"""The TRC analysis sandbox runner (trc-backend spec 2026-09-28 §5).

Runs INSIDE the `hermes-sandbox` container: `network_mode: none`, read-only root, no
environment, no Hermes code. Hermes connects over a unix socket on a shared volume and
sends one script per connection; the script's tool calls come back over the same
connection for Hermes to serve. There is no network interface, so the only way out is
the connection Hermes opened.

Stdlib only. Runs as root so it can give each run its own slot user id; the run itself
holds no capability (dropping from uid 0 clears them; no-new-privileges keeps them
gone). After each run the runner clears what that run could leave for the next one in
its slot: every process of the slot's uid, the contents of the slot's work directory,
and the slot uid's entries directly under /dev/shm and /dev/mqueue. It clears nothing
else — SysV IPC included; the container's IPC isolation (compose, validated by
validate_compose.py) closes the rest. A slot whose cleanup fails is quarantined: it is
never handed to another run.
"""

from __future__ import annotations

import collections
import errno
import itertools
import json
import os
import secrets
import signal
import socket
import socketserver
import stat
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
# What the done frame carries. Hermes keeps 50 KB / 10 KB of these; a larger capture,
# JSON-escaped, could outgrow the client's 4 MB line.
CAPTURE_LIMIT_STDOUT = 64_000
CAPTURE_LIMIT_STDERR = 16_000
MAX_LINE = 4_000_000
POLL_SECONDS = 0.2
HERMES_GRACE_SECONDS = 10
IPC_ROOTS = ("/dev/shm", "/dev/mqueue")
# Operations allowed to empty one tree. A slot's tmpfs holds 4096 inodes and an entry
# costs at most two operations, so reaching this is a fault, not a slow cleanup.
MAX_REMOVE_OPS = 200_000
_DIR_FD = os.unlink in os.supports_dir_fd  # False on Windows, where only tests run
DEFAULT_LIMITS = {"wall": 240, "cpu": 60, "mem_mb": 768}
CEILINGS = {"wall": 240, "cpu": 60, "mem_mb": 768}

MALFORMED = b'{"error": "malformed tool call"}\n'
LOST_GATEWAY = "the sandbox lost its connection to the gateway"
TOO_LARGE = "tool call too large"


def clamp_limits(requested) -> dict:
    """Hermes may ask for LESS than a ceiling, never more. Anything unusable — not a
    dict, not a number, infinite — means the default."""
    if not isinstance(requested, dict):
        requested = {}
    out = {}
    for key, default in DEFAULT_LIMITS.items():
        try:
            value = int(requested.get(key, default))
        except (TypeError, ValueError, OverflowError):
            value = default
        out[key] = max(1, min(value, CEILINGS[key]))
    return out


def _frame(obj) -> bytes:
    """One wire line. ASCII-escaped, so no string content — a lone surrogate that
    json.loads let through, say — can make the encoding fail."""
    return json.dumps(obj, ensure_ascii=True).encode("ascii") + b"\n"


def send(fp, obj) -> None:
    fp.write(_frame(obj))
    fp.flush()


def recv(fp):
    line = fp.readline(MAX_LINE)
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


class SlotPool:
    def __init__(self, uids):
        self._free = list(uids)
        self.size = len(self._free)
        self._quarantined = set()
        self._cond = threading.Condition()

    def quarantine(self, uid) -> None:
        """Never hand `uid` out again (until the daemon restarts)."""
        with self._cond:
            self._quarantined.add(uid)

    def quarantined(self) -> int:
        with self._cond:
            return len(self._quarantined)

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

_GONE = (b"Z", b"X")  # zombie, dead: no memory, no files, no code left to run


def _proc_status(path):
    """(state, real uid) from a /proc status file. Read as bytes: a run names its own
    process, and a name that is not UTF-8 must not hide it."""
    state = owner = None
    with open(path, "rb") as fh:
        for line in fh:
            if line.startswith(b"State:"):
                state = line.split()[1]
            elif line.startswith(b"Uid:"):
                owner = int(line.split()[1])
                break
    return state, owner


def _has_live_thread(pid_dir) -> bool:
    """A zombie leader can still have running threads (it called pthread_exit)."""
    try:
        tids = os.listdir(os.path.join(pid_dir, "task"))
    except OSError:
        return False
    for tid in tids:
        try:
            state, _ = _proc_status(os.path.join(pid_dir, "task", tid, "status"))
        except (OSError, ValueError, IndexError):
            continue
        if state not in _GONE:
            return True
    return False


def uid_pids(uid, proc_root="/proc"):
    """The LIVE processes of `uid`. A zombie is skipped — the daemon may be PID 1 and
    reap an orphan late — unless one of its threads still runs."""
    pids = []
    for entry in os.listdir(proc_root):
        if not entry.isdigit():
            continue
        pid_dir = os.path.join(proc_root, entry)
        try:
            state, owner = _proc_status(os.path.join(pid_dir, "status"))
        except (OSError, ValueError, IndexError):
            continue
        if owner != uid:
            continue
        if state in _GONE and not _has_live_thread(pid_dir):
            continue
        pids.append(int(entry))
    return pids


def kill_uid(uid, proc_root="/proc") -> bool:
    """Kill EVERY process owned by the slot's uid — a setsid/double-fork leftover too.

    True only when no live process of the uid remains; the caller quarantines the slot
    otherwise."""
    for _ in range(40):
        pids = uid_pids(uid, proc_root)
        if not pids:
            return True
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.05)
    return not uid_pids(uid, proc_root)


def slot_dir(uid) -> str:
    return os.path.join(WORK_ROOT, f"slot-{uid}")


class CleanupTooLarge(Exception):
    """Emptying a tree needed more than MAX_REMOVE_OPS operations."""


class _Tree:
    """Empty one directory, whatever the depth of what is in it, and keep the directory.

    Flat, not recursive (shutil.rmtree recurses, and a run can nest deeper than the
    interpreter's recursion limit): a subdirectory that is not empty has its children
    moved up into the root under fresh names and is then removed, until the root is
    empty. Each entry moves at most once, so the work is linear in the entry count.
    At most two directory fds are held — the root and one subdirectory — and every
    operation is relative to one of them, opened O_NOFOLLOW, so no path a run planted
    is ever resolved. Past MAX_REMOVE_OPS it raises, and the caller quarantines the
    slot: fail closed, never loop forever. Where dir_fd is unsupported (Windows, only
    in tests) the same loop runs on paths."""

    def __init__(self, path):
        self.path = path
        self.fd = None
        self._ops = 0
        self._prefix = f".trc-rm-{secrets.token_hex(8)}-"
        self._fresh = itertools.count()
        if _DIR_FD:
            self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        else:
            os.lstat(path)  # a missing root raises FileNotFoundError, as os.open does

    def empty(self) -> None:
        try:
            while True:
                names = os.listdir(self.path if self.fd is None else self.fd)
                if not names:
                    return
                for name in names:
                    self._spend()
                    try:
                        self._remove(name)
                    except FileNotFoundError:
                        continue
        finally:
            if self.fd is not None:
                os.close(self.fd)

    def _spend(self) -> None:
        self._ops += 1
        if self._ops > MAX_REMOVE_OPS:
            raise CleanupTooLarge(self.path)

    def _remove(self, name) -> None:
        if not stat.S_ISDIR(self._lstat(name).st_mode):
            self._unlink(name)
            return
        try:
            self._rmdir(name)
            return
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                raise
        self._hoist(name)
        self._rmdir(name)

    def _hoist(self, name) -> None:
        """Move every child of the subdirectory `name` up into the root."""
        if self.fd is None:
            sub = os.path.join(self.path, name)
            for child in os.listdir(sub):
                self._spend()
                os.rename(os.path.join(sub, child), os.path.join(self.path, self._new_name()))
            return
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        sub_fd = os.open(name, flags, dir_fd=self.fd)
        try:
            for child in os.listdir(sub_fd):
                self._spend()
                os.rename(child, self._new_name(), src_dir_fd=sub_fd, dst_dir_fd=self.fd)
        finally:
            os.close(sub_fd)

    def _new_name(self) -> str:
        return f"{self._prefix}{next(self._fresh)}"

    def _lstat(self, name):
        if self.fd is None:
            return os.lstat(os.path.join(self.path, name))
        return os.stat(name, dir_fd=self.fd, follow_symlinks=False)

    def _unlink(self, name) -> None:
        if self.fd is None:
            os.unlink(os.path.join(self.path, name))
        else:
            os.unlink(name, dir_fd=self.fd)

    def _rmdir(self, name) -> None:
        if self.fd is None:
            os.rmdir(os.path.join(self.path, name))
        else:
            os.rmdir(name, dir_fd=self.fd)


def clear_dir(path) -> None:
    """Empty `path` but keep it: each slot's directory is its own tmpfs mount point,
    which cannot be removed. Nothing a run planted is followed — a symlink is removed,
    never its target. A missing directory is already clear."""
    try:
        tree = _Tree(path)
    except FileNotFoundError:
        return
    tree.empty()


def _remove_dir(path) -> None:
    """Remove the directory `path` and everything in it, whatever the depth."""
    _Tree(path).empty()
    os.rmdir(path)


def _create_file(path, body, uid) -> None:
    """A new file owned by `uid`, mode 0600. It must not exist yet: a name a run left
    behind — a symlink included — fails instead of being followed."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if hasattr(os, "fchown"):
            os.fchown(fd, uid, uid)
        fh = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    with fh:
        fh.write(body)


def prepare_workdir(uid, files) -> str:
    """The slot's work directory, emptied, with `files` ({name: text}) written into it.

    Root owns the directory while the files are written, and only then hands it to the
    slot's uid, so nothing is written through anything a run could plant."""
    path = slot_dir(uid)
    clear_dir(path)
    os.makedirs(path, exist_ok=True)
    if hasattr(os, "chown"):
        os.chown(path, 0, 0)
    os.chmod(path, 0o700)
    for name, body in files.items():
        _create_file(os.path.join(path, name), body, uid)
    if hasattr(os, "chown"):
        os.chown(path, uid, uid)
    os.chmod(path, 0o700)
    return path


def wipe_ipc_leftovers(uid, roots=IPC_ROOTS) -> None:
    """Remove every entry directly under /dev/shm and /dev/mqueue that the slot's uid
    owns: both outlive the run's processes and its work directory. Judged by lstat, so a
    symlink is removed as itself and its target is never touched; root holds FOWNER,
    so the sticky bit does not stand in the way. A missing root is not an error."""
    for root in roots:
        try:
            with os.scandir(root) as it:
                entries = list(it)
        except FileNotFoundError:
            continue
        for entry in entries:
            try:
                info = os.lstat(entry.path)
                if info.st_uid != uid:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    _remove_dir(entry.path)
                else:
                    os.unlink(entry.path)
            except FileNotFoundError:
                continue


class _Capture:
    """Drain a stream to EOF, keeping at most `limit` bytes: half from the head, half
    from the tail. The tail is a queue of whole chunks, so nothing is moved per chunk."""

    def __init__(self, stream, limit):
        self._head_cap = limit // 2
        self._tail_cap = limit - self._head_cap
        self.head = bytearray()
        self._tail = collections.deque()
        self._tail_len = 0
        self.total = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
        self._thread.start()

    def _drain(self, stream):
        try:
            for chunk in iter(lambda: stream.read(65536), b""):
                with self._lock:
                    self._keep(chunk)
        except (OSError, ValueError):
            pass

    def _keep(self, chunk):
        self.total += len(chunk)
        take = self._head_cap - len(self.head)
        if take > 0:
            self.head += chunk[:take]
            chunk = chunk[take:]
        if chunk:
            self._tail.append(chunk)
            self._tail_len += len(chunk)
            while self._tail and self._tail_len - len(self._tail[0]) >= self._tail_cap:
                self._tail_len -= len(self._tail.popleft())

    def text(self) -> str:
        self._thread.join(timeout=3)
        with self._lock:
            head = bytes(self.head)
            tail = b"".join(self._tail)
            total = self.total
        tail = tail[len(tail) - self._tail_cap:] if len(tail) > self._tail_cap else tail
        omitted = total - len(head) - len(tail)
        data = head
        if omitted > 0:
            data += f"\n... [{omitted} bytes omitted] ...\n".encode()
        return (data + tail).decode("utf-8", errors="replace")


def _call_frame(line, seq):
    """The call frame for one script line, or None when the line is not a tool call:
    not JSON, not an object, or nested deeper than the codec allows."""
    try:
        request = json.loads(line.decode("utf-8"))
        if not isinstance(request, dict):
            return None
        args = request.get("args")
        return _frame({
            "op": "call",
            "id": seq,
            "tool": str(request.get("tool", "")),
            "args": args if isinstance(args, dict) else {},
        })
    except (ValueError, RecursionError):
        return None


def _script_reply(result) -> bytes:
    """The line the script reads back: a result string verbatim when it is one line,
    anything else JSON-encoded — which the stub unwraps."""
    if isinstance(result, str) and "\n" not in result:
        try:
            return result.encode("utf-8") + b"\n"
        except UnicodeEncodeError:
            pass
    return _frame(result)


def _await_exit(proc, deadline) -> str:
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return "timeout"
    return "exited"


class _Relay:
    def __init__(self, child, hermes_sock, rfile, wfile, proc, deadline):
        self.child, self.hermes_sock = child, hermes_sock
        self.rfile, self.wfile = rfile, wfile
        self.proc, self.deadline = proc, deadline
        self.seq = 0

    def run(self) -> str:
        buf = bytearray()
        scanned = 0  # bytes of buf already searched for a newline
        self.child.settimeout(POLL_SECONDS)
        while True:
            if time.monotonic() > self.deadline:
                return "timeout"
            if self.proc.poll() is not None and not buf:
                return "exited"
            try:
                chunk = self.child.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                chunk = b""
            if not chunk:
                return _await_exit(self.proc, self.deadline)
            buf += chunk
            start = 0
            while True:
                if time.monotonic() > self.deadline:
                    return "timeout"  # not one more buffered call past the wall clock
                end = buf.find(b"\n", scanned)
                if end < 0:
                    break
                if end - start > MAX_LINE:
                    return "too_large"
                line = bytes(buf[start:end])
                start = scanned = end + 1
                outcome = self._serve(line)
                if outcome == "gone":
                    return _await_exit(self.proc, self.deadline)
                if outcome is not None:
                    return outcome
            del buf[:start]
            scanned = len(buf)
            if len(buf) > MAX_LINE:
                return "too_large"

    def _serve(self, line):
        frame = _call_frame(line, self.seq + 1)
        if frame is None:
            return self._to_script(MALFORMED)
        if len(frame) > MAX_LINE:
            return "too_large"
        self.seq += 1
        grace = max(1.0, self.deadline - time.monotonic()) + HERMES_GRACE_SECONDS
        try:
            self.hermes_sock.settimeout(grace)
            self.wfile.write(frame)
            self.wfile.flush()
            reply = recv(self.rfile)
        except socket.timeout:
            return "timeout"  # the read allows the rest of the wall clock and then some
        except (OSError, ValueError):
            return "protocol"
        if (
            not isinstance(reply, dict)
            or reply.get("op") != "result"
            or type(reply.get("id")) is not int
            or reply["id"] != self.seq
        ):
            return "protocol"
        return self._to_script(_script_reply(reply.get("result")))

    def _to_script(self, data):
        """Send to the script with the rest of the wall clock, not the poll interval.
        None when sent; "timeout" when the clock ran out; "gone" when the script
        closed its end."""
        self.child.settimeout(max(1.0, self.deadline - time.monotonic()))
        try:
            self.child.sendall(data)
        except socket.timeout:
            return "timeout"
        except OSError:
            return "gone"
        finally:
            self.child.settimeout(POLL_SECONDS)
        return None


def relay(child, hermes_sock, rfile, wfile, proc, deadline) -> str:
    """Relay the script's tool calls to Hermes until the script exits.

    Returns "exited" (the script ended), "timeout" (the wall clock ran out), "protocol"
    (Hermes went away, or its reply was malformed or answered another call) or
    "too_large" (a tool call longer than MAX_LINE). Nothing the script sends raises."""
    return _Relay(child, hermes_sock, rfile, wfile, proc, deadline).run()


def _done(status, started, exit_code=-1, stdout="", stderr=""):
    return {
        "op": "done",
        "status": status,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "duration_seconds": round(time.monotonic() - started, 2),
    }


_RUNNER_LINES = {"protocol": LOST_GATEWAY, "too_large": TOO_LARGE}


def _finish(outcome, started, returncode, stdout, stderr) -> dict:
    if returncode is None:  # never reaped: there is no exit code to report
        returncode = -1
    if outcome == "exited":
        status = "success" if returncode == 0 else "error"
        return _done(status, started, returncode, stdout, stderr)
    if outcome == "timeout":
        return _done("timeout", started, -1, stdout, stderr)
    line = _RUNNER_LINES.get(outcome, "sandbox runner error")
    if stderr and not stderr.endswith("\n"):
        stderr += "\n"
    return _done("error", started, -1, stdout, stderr + line)


class _Slot:
    """A slot uid held by one run. It goes back to the pool only when every cleanup
    step succeeded; otherwise it is quarantined — never reused — and said so once on
    stderr. Fail closed: a lost slot costs capacity, a reused dirty one leaks."""

    def __init__(self, uid):
        self.uid = uid
        self.fault = None

    def _fail(self, reason) -> None:
        if self.fault is None:
            self.fault = reason

    def kill(self) -> bool:
        try:
            if kill_uid(self.uid):
                return True
        except Exception:
            pass
        self._fail("processes survived cleanup")
        return False

    def reap(self, proc) -> None:
        """The launcher's own pid, which may still be root (before its uid drop), where
        kill_uid cannot see it."""
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self._fail("processes survived cleanup")

    def recycle(self, pool) -> None:
        killed = self.kill()
        try:
            clear_dir(slot_dir(self.uid))
        except Exception:
            self._fail("its work directory could not be cleared")
        # Once nothing of the uid is alive, the wipe runs whatever else faulted: a run
        # must not be able to keep its /dev/shm entries by making its own clear fail.
        if killed:
            try:
                wipe_ipc_leftovers(self.uid)
            except Exception:
                self._fail("its /dev/shm or /dev/mqueue entries could not be removed")
        if self.fault is None:
            pool.release(self.uid)
            return
        pool.quarantine(self.uid)
        try:
            print(f"sandbox: slot {self.uid} quarantined: {self.fault}", file=sys.stderr,
                  flush=True)
        except (OSError, ValueError):
            pass


def _run_in_slot(slot, limits, started, hermes_sock, rfile, wfile, request) -> dict:
    workdir = prepare_workdir(slot.uid, {
        "script.py": request.get("code") or "",
        "hermes_tools.py": request.get("stubs") or "",
    })
    parent, child = socket.socketpair()
    proc = None
    try:
        try:
            proc = subprocess.Popen(
                [
                    sys.executable, "-E", "-s", "-B", LAUNCHER,
                    str(slot.uid), str(limits["cpu"]), str(limits["mem_mb"]),
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
        out = _Capture(proc.stdout, CAPTURE_LIMIT_STDOUT)
        err = _Capture(proc.stderr, CAPTURE_LIMIT_STDERR)
        outcome = relay(parent, hermes_sock, rfile, wfile, proc, started + limits["wall"])
        slot.kill()  # before reading output: a leftover could hold the pipes open
        slot.reap(proc)
        returncode = -1 if proc.returncode is None else proc.returncode
        return _finish(outcome, started, returncode, out.text(), err.text())
    finally:
        parent.close()
        if proc is not None:
            slot.reap(proc)


def run_job(hermes_sock, rfile, wfile, request) -> dict:
    """One run in a free slot. Always a done frame, never an exception."""
    started = time.monotonic()
    if not isinstance(request, dict):
        return _done("error", started, stderr="expected a run request")
    limits = clamp_limits(request.get("limits"))
    uid = POOL.acquire(BUSY_WAIT_SECONDS)
    if uid is None:
        held = POOL.quarantined()
        note = f"{held} of {POOL.size} sandbox slots quarantined" if held else ""
        return _done("busy", started, stderr=note)
    slot = _Slot(uid)
    try:
        return _run_in_slot(slot, limits, started, hermes_sock, rfile, wfile, request)
    except Exception as exc:
        return _done("error", started, stderr=f"sandbox runner error: {type(exc).__name__}")
    finally:
        slot.recycle(POOL)


def _is_run_request(request) -> bool:
    if not isinstance(request, dict) or request.get("op") != "run":
        return False
    limits = request.get("limits")
    if limits is not None and not isinstance(limits, dict):
        return False
    return all(isinstance(request.get(key) or "", str) for key in ("code", "stubs"))


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(30)
        try:
            request = recv(self.rfile)
        except (OSError, ValueError, RecursionError):
            return
        if _is_run_request(request):
            reply = run_job(self.connection, self.rfile, self.wfile, request)
        else:
            reply = _done("error", time.monotonic(), stderr="expected a run request")
        try:
            send(self.wfile, reply)
        except OSError:
            pass  # Hermes went away; the slot was cleaned up before the reply


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
