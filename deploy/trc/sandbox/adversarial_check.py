"""Adversarial checks against the LIVE sandbox (trc-backend spec 2026-09-28 §10).

Run inside the hermes-agent container (it mounts the runner's socket):
    python deploy/trc/sandbox/adversarial_check.py --quick   # every deploy
    python deploy/trc/sandbox/adversarial_check.py --full    # the enable gate
Exits 1 if any check fails. An unrun check is not coverage: run --full on staging
before enabling, and read the output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.sidecar_sandbox import run_in_sidecar  # noqa: E402

SOCKET = "/run/hermes-sandbox/sock"


def _run(code: str) -> dict:
    return run_in_sidecar(
        SOCKET, code, "", {}, lambda tool, args: json.dumps({"error": "no tools"}),
        overall_timeout=300,
    )


# libc on the sandbox image (python:3.12-slim-bookworm, glibc 2.36: mq_open is in libc).
_LIBC = "import ctypes\nlibc = ctypes.CDLL('libc.so.6', use_errno=True)\n"
_SHMGET = "libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]\n"
SHM_KEY = 0x54524301
IPC_CREAT = 0o1000

QUICK = {
    "no network (app:8000)": (
        "import socket\n"
        "try:\n socket.create_connection(('app', 8000), timeout=3); print('OPEN')\n"
        "except OSError: print('BLOCKED')\n",
        "BLOCKED",
    ),
    "no network (public IP)": (
        "import socket\n"
        "try:\n socket.create_connection(('1.1.1.1', 443), timeout=3); print('OPEN')\n"
        "except OSError: print('BLOCKED')\n",
        "BLOCKED",
    ),
    "no DNS": (
        "import socket\n"
        "try:\n socket.getaddrinfo('example.com', 443); print('OPEN')\n"
        "except OSError: print('BLOCKED')\n",
        "BLOCKED",
    ),
    "control socket refused": (
        "import socket\n"
        "s = socket.socket(socket.AF_UNIX)\n"
        "try:\n s.connect('/run/hermes-sandbox/sock'); print('OPEN')\n"
        "except OSError: print('BLOCKED')\n",
        "BLOCKED",
    ),
    "nothing writable outside the run": (
        "import os\n"
        "bad = []\n"
        "for d in ('/', '/opt', '/opt/sandbox', '/tmp', '/work', '/run/hermes-sandbox',\n"
        "          '/dev/shm', '/dev/mqueue'):\n"
        " try:\n  open(os.path.join(d, 'x'), 'w').close(); bad.append(d)\n"
        " except OSError: pass\n"
        "open('ok.txt', 'w').close()\n"
        "print('BAD ' + ','.join(bad) if bad else 'CONTAINED')\n",
        "CONTAINED",
    ),
    # Absent, not merely unwritable: a present, world-writable /dev/shm lets two
    # concurrent runs share a nested directory the runner's per-uid wipe never judges.
    "no /dev/shm": (
        "import os\nprint('PRESENT' if os.path.exists('/dev/shm') else 'ABSENT')\n",
        "ABSENT",
    ),
    "no message queues": (
        _LIBC
        + "libc.msgget.argtypes = [ctypes.c_int, ctypes.c_int]\n"
        f"sysv = libc.msgget(0, {IPC_CREAT} | 0o600)\n"  # IPC_PRIVATE
        "libc.mq_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]\n"
        "posix = libc.mq_open(b'/trc-sandbox-probe', 0o100 | 2, 0o600, None)\n"  # O_CREAT|O_RDWR
        "opened = [n for n, r in (('msgget', sysv), ('mq_open', posix)) if r != -1]\n"
        "print('OPEN ' + ','.join(opened) if opened else 'BLOCKED')\n",
        "BLOCKED",
    ),
    "the work dir is noexec": (
        "import os, subprocess\n"
        "with open('probe.sh', 'w') as fh:\n fh.write('#!/bin/sh\\necho RAN\\n')\n"
        "os.chmod('probe.sh', 0o700)\n"
        "try:\n"
        " r = subprocess.run(['./probe.sh'], capture_output=True, text=True, timeout=10)\n"
        " print('EXECUTED ' + r.stdout.strip())\n"
        # PermissionError from a noexec mount; a run that cannot spawn at all also
        # proves nothing written here executes.
        "except OSError:\n print('NOEXEC')\n",
        "NOEXEC",
    ),
    "no secrets in the environment": (
        "import os\n"
        "keys = [k for k in os.environ if any(s in k.upper() for s in ('KEY', 'TOKEN', 'SECRET', 'PASS'))]\n"
        "print('LEAK ' + ','.join(keys) if keys else 'CLEAN')\n",
        "CLEAN",
    ),
    "leftover process is killed": (
        "import os, time\n"
        "if os.fork() == 0:\n"
        " os.setsid()\n"
        " if os.fork() == 0:\n  time.sleep(600)\n"
        " os._exit(0)\n"
        "print('SPAWNED')\n",
        "SPAWNED",
    ),
    # A zombie is the reaper's lag, not a survivor -- the runner's own rule.
    "no process of any slot survives a run": (
        "import os\n"
        "me = os.getpid()\n"
        "left = []\n"
        "for p in os.listdir('/proc'):\n"
        " if p.isdigit() and int(p) != me:\n"
        "  try:\n"
        "   st = open(f'/proc/{p}/status').read()\n"
        "   uid = int(st.split('Uid:')[1].split()[0])\n"
        "   state = st.split('State:')[1].split()[0]\n"
        "  except (OSError, IndexError, ValueError):\n   continue\n"
        "  if 20001 <= uid <= 20003 and state != 'Z': left.append(p)\n"
        "print('LEFT ' + ','.join(left) if left else 'NONE')\n",
        "NONE",
    ),
    "each run starts in an empty directory": (
        "import os\nprint(sorted(os.listdir('.')))\n",
        "['hermes_tools.py', 'script.py']",
    ),
}

# Two runs: the first leaves a SysV segment behind if it can (CREATED) or is refused
# (REFUSED, the better outcome); the second, in whatever slot it lands, must not find it.
SHM_LEAVE = (
    _LIBC + _SHMGET
    + "libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]\n"
    "libc.shmat.restype = ctypes.c_void_p\n"
    f"sid = libc.shmget({SHM_KEY}, 4096, {IPC_CREAT} | 0o666)\n"
    "if sid == -1:\n print('REFUSED')\n"
    "else:\n"
    " addr = libc.shmat(sid, None, 0)\n"
    " if addr not in (None, ctypes.c_void_p(-1).value):\n"
    "  ctypes.memmove(addr, b'TRC-SANDBOX-MARKER', 18)\n"
    " print('CREATED')\n"
)
SHM_FIND = (
    _LIBC + _SHMGET
    + f"print('GONE' if libc.shmget({SHM_KEY}, 0, 0) == -1 else 'PRESENT')\n"
)


def _check_shm_between_runs() -> tuple[bool, str, str]:
    first = _run(SHM_LEAVE)
    second = _run(SHM_FIND)
    out = (second.get("stdout") or "").strip()
    first_out = (first.get("stdout") or "").strip()
    ok = second.get("status") == "success" and out == "GONE"
    return ok, f"first={first_out[:20]!r}, status={second.get('status')}", out


FULL = {
    "fork bomb is contained": ("import os\nwhile True:\n os.fork()\n", None),
    "memory hog is contained": ("x = bytearray(4 * 1024**3)\nprint('ALLOCATED')\n", None),
    "cpu spin is stopped": ("while True:\n pass\n", None),
}


def main(argv: list[str]) -> int:
    cases = dict(QUICK)
    if "--full" in argv:
        cases.update(FULL)
    failed = 0
    for name, (code, expected) in cases.items():
        done = _run(code)
        out = (done.get("stdout") or "").strip()
        if expected is None:
            ok = done.get("status") in ("error", "timeout") and "ALLOCATED" not in out
            ok = ok and _run("print('ALIVE')").get("stdout", "").strip() == "ALIVE"
        else:
            ok = done.get("status") == "success" and out == expected
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}  (status={done.get('status')}, out={out[:60]!r})")
    ok, detail, out = _check_shm_between_runs()
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  no SysV shared memory between runs  ({detail}, out={out[:60]!r})")
    print("all sandbox checks passed" if not failed else f"{failed} sandbox check(s) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
