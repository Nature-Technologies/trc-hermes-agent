"""The TRC analysis sandbox runner (trc-backend spec 2026-09-28 §5).

Runs INSIDE the `hermes-sandbox` container: `network_mode: none`, read-only root, no
environment, no Hermes code. Hermes connects over a unix socket on a shared volume and
sends one script per connection; the script's tool calls come back over the same
connection for Hermes to serve. There is no network interface, so the only way out is
the connection Hermes opened.

Stdlib only. Runs as root so it can give each run its own slot user id; the run itself
holds no capability (dropping from uid 0 clears them; no-new-privileges keeps them
gone). Nothing a run leaves behind survives into the next run in its slot.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time

SOCKET_PATH = "/run/hermes-sandbox/sock"
HERMES_UID = 10000
WORK_ROOT = "/work"
SLOT_UIDS = (20001, 20002, 20003)
LAUNCHER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "launch.py")
BUSY_WAIT_SECONDS = 20.0
CAPTURE_LIMIT = 1_000_000
MAX_LINE = 4_000_000
DEFAULT_LIMITS = {"wall": 240, "cpu": 60, "mem_mb": 768}
CEILINGS = {"wall": 240, "cpu": 60, "mem_mb": 768}


def clamp_limits(requested: dict) -> dict:
    """Hermes may ask for LESS than a ceiling, never more."""
    out = {}
    for key, default in DEFAULT_LIMITS.items():
        try:
            value = int(requested.get(key, default))
        except (TypeError, ValueError):
            value = default
        out[key] = max(1, min(value, CEILINGS[key]))
    return out


def send(fp, obj) -> None:
    fp.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    fp.flush()


def recv(fp):
    line = fp.readline(MAX_LINE)
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


class SlotPool:
    def __init__(self, uids):
        self._free = list(uids)
        self._cond = threading.Condition()

    def acquire(self, timeout):
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._free:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            return self._free.pop(0)

    def release(self, uid) -> None:
        with self._cond:
            self._free.append(uid)
            self._cond.notify()


POOL = SlotPool(SLOT_UIDS)


def uid_pids(uid, proc_root="/proc"):
    pids = []
    for entry in os.listdir(proc_root):
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, entry, "status"), encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("Uid:"):
                        if int(line.split()[1]) == uid:
                            pids.append(int(entry))
                        break
        except (OSError, ValueError, IndexError):
            continue
    return pids


def kill_uid(uid) -> None:
    """Kill EVERY process owned by the slot's uid — a setsid/double-fork leftover too."""
    for _ in range(40):
        pids = uid_pids(uid)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.05)


def prepare_workdir(uid) -> str:
    path = os.path.join(WORK_ROOT, f"slot-{uid}")
    shutil.rmtree(path, ignore_errors=True)
    os.mkdir(path, 0o700)
    os.chown(path, uid, uid)
    return path


class _Capture:
    """Drain a stream to EOF, keeping at most CAPTURE_LIMIT bytes (head and tail)."""

    def __init__(self, stream):
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self._thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
        self._thread.start()

    def _drain(self, stream):
        half = CAPTURE_LIMIT // 2
        for chunk in iter(lambda: stream.read(65536), b""):
            self.total += len(chunk)
            if len(self.head) < half:
                take = half - len(self.head)
                self.head += chunk[:take]
                chunk = chunk[take:]
            if chunk:
                self.tail += chunk
                if len(self.tail) > half:
                    del self.tail[: len(self.tail) - half]

    def text(self) -> str:
        self._thread.join(timeout=3)
        omitted = self.total - len(self.head) - len(self.tail)
        data = bytes(self.head)
        if omitted > 0:
            data += f"\n... [{omitted} bytes omitted] ...\n".encode()
        return (data + bytes(self.tail)).decode("utf-8", errors="replace")


def relay(child, hermes_sock, rfile, wfile, proc, deadline) -> bool:
    """Relay the script's tool calls to Hermes until the script exits.

    Returns False when the wall clock ran out (or Hermes went away)."""
    buf = b""
    seq = 0
    child.settimeout(0.2)
    while True:
        if time.monotonic() > deadline:
            return False
        if proc.poll() is not None and not buf:
            return True
        try:
            chunk = child.recv(65536)
        except socket.timeout:
            continue
        except OSError:
            chunk = b""
        if not chunk:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
                return True
            except subprocess.TimeoutExpired:
                return False
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                request = json.loads(line.decode("utf-8"))
            except ValueError:
                child.sendall(b'{"error": "malformed tool call"}\n')
                continue
            seq += 1
            send(wfile, {
                "op": "call",
                "id": seq,
                "tool": str(request.get("tool", "")),
                "args": request.get("args") if isinstance(request.get("args"), dict) else {},
            })
            hermes_sock.settimeout(max(1.0, deadline - time.monotonic()) + 10)
            reply = recv(rfile)
            if not reply or reply.get("op") != "result":
                return False
            result = reply.get("result")
            if not isinstance(result, str):
                result = json.dumps(result)
            child.sendall(result.encode("utf-8") + b"\n")


def _done(status, started, exit_code=-1, stdout="", stderr=""):
    return {
        "op": "done",
        "status": status,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "duration_seconds": round(time.monotonic() - started, 2),
    }


def run_job(hermes_sock, rfile, wfile, request) -> dict:
    started = time.monotonic()
    limits = clamp_limits(request.get("limits") or {})
    uid = POOL.acquire(BUSY_WAIT_SECONDS)
    if uid is None:
        return _done("busy", started)
    try:
        workdir = prepare_workdir(uid)
        for name, body in (
            ("script.py", request.get("code") or ""),
            ("hermes_tools.py", request.get("stubs") or ""),
        ):
            path = os.path.join(workdir, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.chown(path, uid, uid)
        parent, child = socket.socketpair()
        try:
            proc = subprocess.Popen(
                [
                    sys.executable, "-E", "-s", "-B", LAUNCHER,
                    str(uid), str(limits["cpu"]), str(limits["mem_mb"]),
                    workdir, str(child.fileno()),
                ],
                pass_fds=(child.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={},
                start_new_session=True,
            )
        finally:
            child.close()
        out, err = _Capture(proc.stdout), _Capture(proc.stderr)
        finished = relay(parent, hermes_sock, rfile, wfile, proc, started + limits["wall"])
        kill_uid(uid)  # before reading output: a leftover could hold the pipes open
        proc.wait(timeout=5)
        parent.close()
        if not finished:
            return _done("timeout", started, -1, out.text(), err.text())
        status = "success" if proc.returncode == 0 else "error"
        return _done(status, started, proc.returncode, out.text(), err.text())
    finally:
        kill_uid(uid)
        shutil.rmtree(os.path.join(WORK_ROOT, f"slot-{uid}"), ignore_errors=True)
        POOL.release(uid)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(30)
        try:
            request = recv(self.rfile)
        except (OSError, ValueError):
            return
        if not request or request.get("op") != "run":
            send(self.wfile, _done("error", time.monotonic(), stderr="expected a run request"))
            return
        send(self.wfile, run_job(self.connection, self.rfile, self.wfile, request))


if hasattr(socketserver, "UnixStreamServer"):

    class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True


def serve() -> None:
    os.umask(0o077)
    os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    os.makedirs(WORK_ROOT, exist_ok=True)
    with _Server(SOCKET_PATH, _Handler) as server:
        os.chown(SOCKET_PATH, HERMES_UID, HERMES_UID)
        os.chmod(SOCKET_PATH, 0o600)
        server.serve_forever()


if __name__ == "__main__":
    serve()
