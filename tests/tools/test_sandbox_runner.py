"""Pure parts of the sandbox runner (deploy/trc/sandbox/runner.py). The process,
privilege and isolation behaviour is verified against the real container by
deploy/trc/sandbox/adversarial_check.py (Task 8)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

_SANDBOX = Path(__file__).resolve().parents[2] / "deploy" / "trc" / "sandbox"
_spec = importlib.util.spec_from_file_location("sandbox_runner", _SANDBOX / "runner.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

_needs_ownership = pytest.mark.skipif(
    sys.platform == "win32", reason="no file ownership on Windows")
_needs_dir_fd = pytest.mark.skipif(not runner._DIR_FD, reason="no dir_fd on Windows")


def test_limits_are_clamped_to_the_ceiling():
    """Review Focus 3."""
    got = runner.clamp_limits({"wall": 9999, "cpu": 9999, "mem_mb": 99999})
    assert got == runner.CEILINGS


def test_smaller_limits_are_honoured_and_junk_falls_back():
    assert runner.clamp_limits({"wall": 30, "cpu": "x"})["wall"] == 30
    assert runner.clamp_limits({"cpu": "x"})["cpu"] == runner.DEFAULT_LIMITS["cpu"]


@pytest.mark.parametrize("requested", [{"wall": float("inf")}, {"cpu": "nan"}, [], None, "x"])
def test_unusable_limits_mean_the_defaults(requested):
    """M5: JSON Infinity, NaN and a non-dict must not raise."""
    assert runner.clamp_limits(requested) == runner.DEFAULT_LIMITS


def test_framing_round_trip():
    buf = io.BytesIO()
    runner.send(buf, {"op": "done", "text": "é"})
    buf.seek(0)
    assert runner.recv(buf) == {"op": "done", "text": "é"}
    assert runner.recv(buf) is None


def test_a_lone_surrogate_is_escaped_not_raised():
    """I2: json.loads lets '\\ud800' through; the frame to Hermes must still encode."""
    buf = io.BytesIO()
    runner.send(buf, {"op": "done", "stdout": json.loads('"\\ud800"')})
    assert b"\\ud800" in buf.getvalue()


def test_slot_pool_answers_busy_when_exhausted():
    pool = runner.SlotPool((1, 2))
    assert {pool.acquire(0.01), pool.acquire(0.01)} == {1, 2}
    assert pool.acquire(0.01) is None
    pool.release(1)
    assert pool.acquire(0.01) == 1


# --- /proc and kill_uid -------------------------------------------------------------


def _proc(root: Path, pid: int, uid: int, state: str = "S", name: bytes = b"python",
          threads: dict | None = None) -> Path:
    d = root / str(pid)
    d.mkdir()
    (d / "status").write_bytes(_status(name, state, uid))
    for tid, tstate in (threads or {}).items():
        t = d / "task" / str(tid)
        t.mkdir(parents=True)
        (t / "status").write_bytes(_status(name, tstate, uid))
    return d


def _status(name: bytes, state: str, uid: int) -> bytes:
    return (b"Name:\t" + name + f"\nState:\t{state} (x)\nTgid:\t1\n"
            f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n".encode())


def test_uid_pids_reads_a_proc_tree(tmp_path):
    for pid, uid in ((10, 20001), (11, 20002), (12, 20001)):
        _proc(tmp_path, pid, uid)
    (tmp_path / "self").mkdir()
    assert sorted(runner.uid_pids(20001, proc_root=str(tmp_path))) == [10, 12]


def test_uid_pids_skips_zombies_and_dead_processes(tmp_path):
    """I1: a PID-1 daemon may leave a killed orphan as a zombie that still shows the uid."""
    _proc(tmp_path, 10, 20001, "Z", threads={10: "Z"})
    _proc(tmp_path, 11, 20001, "X")
    _proc(tmp_path, 12, 20001, "R")
    assert runner.uid_pids(20001, proc_root=str(tmp_path)) == [12]


def test_a_zombie_leader_with_a_running_thread_is_live(tmp_path):
    """The leader can pthread_exit and leave its other threads running."""
    _proc(tmp_path, 10, 20001, "Z", threads={10: "Z", 11: "R"})
    assert runner.uid_pids(20001, proc_root=str(tmp_path)) == [10]


def test_a_name_that_is_not_utf8_does_not_hide_a_process(tmp_path):
    """A run sets its own name (prctl, /proc/self/comm)."""
    _proc(tmp_path, 10, 20001, name=b"\xff\xfe")
    assert runner.uid_pids(20001, proc_root=str(tmp_path)) == [10]


@pytest.fixture
def fake_kill(monkeypatch):
    calls = []
    monkeypatch.setattr(runner.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(runner.os, "kill", lambda pid, sig: calls.append(pid))
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    return calls


def test_kill_uid_is_satisfied_by_zombies(tmp_path, fake_kill):
    _proc(tmp_path, 10, 20001, "Z", threads={10: "Z"})
    assert runner.kill_uid(20001, proc_root=str(tmp_path)) is True
    assert fake_kill == []


def test_kill_uid_reports_a_process_that_survives(tmp_path, fake_kill):
    """I1: True only when no LIVE process remains; a survivor is reported, not ignored."""
    _proc(tmp_path, 10, 20001, "D")
    assert runner.kill_uid(20001, proc_root=str(tmp_path)) is False
    assert fake_kill == [10] * 40


def test_kill_uid_succeeds_once_the_process_is_gone(tmp_path, monkeypatch, fake_kill):
    d = _proc(tmp_path, 10, 20001, "R")
    monkeypatch.setattr(runner.os, "kill", lambda pid, sig: (d / "status").write_bytes(
        _status(b"python", "Z", 20001)))
    assert runner.kill_uid(20001, proc_root=str(tmp_path)) is True


# --- work directory and IPC leftovers ------------------------------------------------


def _symlink(link: Path, target: Path) -> bool:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        return False
    return True


@pytest.fixture
def mount_point(tmp_path, monkeypatch):
    """tmp_path/slot-20001, which -- like the per-slot tmpfs -- must never be removed."""
    slot = tmp_path / "slot-20001"
    real_rmtree, real_rmdir = shutil.rmtree, runner.os.rmdir

    def rmtree(path, *a, **kw):
        assert Path(path) != slot, "the slot directory itself must never be removed"
        return real_rmtree(path, *a, **kw)

    def rmdir(path, *a, **kw):
        assert Path(path) != slot, "the slot directory itself must never be removed"
        return real_rmdir(path, *a, **kw)

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    monkeypatch.setattr(runner.os, "rmdir", rmdir)
    return slot


def test_clear_dir_empties_the_directory_but_keeps_it(tmp_path, mount_point):
    """A slot's directory is a tmpfs mount point: its contents go, never the directory."""
    slot = mount_point
    (slot / "sub" / "deep").mkdir(parents=True)
    (slot / "sub" / "deep" / "f.txt").write_text("x", encoding="utf-8")
    (slot / "a.txt").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    _symlink(slot / "link", outside / "keep.txt")
    _symlink(slot / "dirlink", outside)
    runner.clear_dir(str(slot))
    assert slot.is_dir() and list(slot.iterdir()) == []
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_clear_dir_of_a_missing_directory_is_a_no_op(tmp_path):
    runner.clear_dir(str(tmp_path / "absent"))


def _deep_tree(root: Path, depth: int) -> None:
    """`depth` nested directories under `root`, a file at the top and at the bottom:
    what `for _ in range(depth): os.mkdir("d"); os.chdir("d")` leaves behind."""
    (root / "top.txt").write_bytes(b"x")
    if not runner._DIR_FD:
        here = root
        for _ in range(depth):
            here = here / "d"
            here.mkdir()
        (here / "leaf").write_bytes(b"x")
        return
    fd = os.open(root, os.O_RDONLY)
    try:
        for _ in range(depth):
            os.mkdir("d", dir_fd=fd)
            nested = os.open("d", os.O_RDONLY, dir_fd=fd)
            os.close(fd)
            fd = nested
        os.close(os.open("leaf", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd))
    finally:
        os.close(fd)


@_needs_dir_fd
def test_clear_dir_empties_a_tree_deeper_than_the_recursion_limit(mount_point):
    """N1: shutil.rmtree recurses; 1,100 levels raised RecursionError and quarantined
    the slot for good, so three such runs took the sandbox down."""
    mount_point.mkdir()
    _deep_tree(mount_point, 1100)
    runner.clear_dir(str(mount_point))
    assert mount_point.is_dir() and list(mount_point.iterdir()) == []


def test_the_path_fallback_is_iterative_too(mount_point, monkeypatch):
    """Where dir_fd is unsupported the same flat loop runs on paths. On Linux this
    forces the fallback at full depth; Windows paths cap the depth at 40."""
    depth = 40 if sys.platform == "win32" else 1100
    mount_point.mkdir()
    _deep_tree(mount_point, depth)
    monkeypatch.setattr(runner, "_DIR_FD", False)
    runner.clear_dir(str(mount_point))
    assert mount_point.is_dir() and list(mount_point.iterdir()) == []


@_needs_dir_fd
def test_tree_removal_holds_at_most_two_directory_fds(tmp_path, monkeypatch):
    root = tmp_path / "slot"
    root.mkdir()
    _deep_tree(root, 200)
    for i in range(20):
        (root / f"wide{i}" / "inner").mkdir(parents=True)
    held, peak = set(), [0]
    real_open, real_close = os.open, os.close

    def opener(*a, **kw):
        fd = real_open(*a, **kw)
        held.add(fd)
        peak[0] = max(peak[0], len(held))
        return fd

    def closer(fd):
        held.discard(fd)
        return real_close(fd)

    monkeypatch.setattr(runner.os, "open", opener)
    monkeypatch.setattr(runner.os, "close", closer)
    runner.clear_dir(str(root))
    assert list(root.iterdir()) == []
    assert peak[0] == 2 and not held


@_needs_dir_fd
def test_a_symlinked_root_is_refused_not_followed(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        runner._remove_dir(str(link))
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_the_operation_cap_fails_closed(tmp_path, monkeypatch):
    """N1: past MAX_REMOVE_OPS the removal raises instead of looping on."""
    monkeypatch.setattr(runner, "MAX_REMOVE_OPS", 5)
    for i in range(20):
        (tmp_path / f"f{i}").write_bytes(b"x")
    with pytest.raises(runner.CleanupTooLarge):
        runner.clear_dir(str(tmp_path))


def test_prepare_workdir_hands_the_directory_over_only_after_writing(
        tmp_path, monkeypatch, mount_point):
    """M3: root owns the directory while the files are written; the uid gets it last."""
    monkeypatch.setattr(runner, "WORK_ROOT", str(tmp_path))
    slot = mount_point
    (slot / "old").mkdir(parents=True)
    (slot / "leftover.txt").write_text("old", encoding="utf-8")
    events = []

    def chown(path, uid, gid):
        events.append(("chown", uid, sorted(os.listdir(path))))

    monkeypatch.setattr(runner.os, "chown", chown, raising=False)
    monkeypatch.setattr(runner.os, "fchown", lambda fd, uid, gid: events.append(("fchown", uid)),
                        raising=False)
    path = runner.prepare_workdir(20001, {"script.py": "print(1)", "hermes_tools.py": "x = 1"})
    assert path == str(slot)
    assert events == [
        ("chown", 0, []),
        ("fchown", 20001),
        ("fchown", 20001),
        ("chown", 20001, ["hermes_tools.py", "script.py"]),
    ]
    assert (slot / "script.py").read_text(encoding="utf-8") == "print(1)"


def test_a_file_is_never_written_through_an_existing_name(tmp_path, monkeypatch):
    """M3: O_EXCL (plus O_NOFOLLOW): an existing file or a planted symlink fails."""
    monkeypatch.setattr(runner.os, "fchown", lambda fd, uid, gid: None, raising=False)
    existing = tmp_path / "script.py"
    existing.write_text("theirs", encoding="utf-8")
    with pytest.raises(FileExistsError):
        runner._create_file(str(existing), "ours", 20001)
    assert existing.read_text(encoding="utf-8") == "theirs"
    target = tmp_path / "target.txt"
    target.write_text("keep", encoding="utf-8")
    if not _symlink(tmp_path / "planted.py", target):
        pytest.skip("symlinks not available here")
    with pytest.raises(OSError):
        runner._create_file(str(tmp_path / "planted.py"), "ours", 20001)
    assert target.read_text(encoding="utf-8") == "keep"


@_needs_ownership
def test_ipc_leftovers_of_the_slot_uid_are_removed(tmp_path):
    """C1: /dev/shm and /dev/mqueue survive the kill and the work-dir wipe."""
    shm, mq = tmp_path / "shm", tmp_path / "mqueue"
    shm.mkdir()
    mq.mkdir()
    (shm / "seg").write_bytes(b"data")
    (shm / "dir").mkdir()
    (shm / "dir" / "inner").write_bytes(b"data")
    (mq / "queue").write_bytes(b"")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    # Each link is ours, and judged as itself: one to root's file, one to a directory.
    (shm / "rootlink").symlink_to("/etc/passwd")
    (shm / "dirlink").symlink_to(outside, target_is_directory=True)
    ours = os.lstat(shm / "seg").st_uid
    assert os.stat("/etc/passwd").st_uid != ours
    runner.wipe_ipc_leftovers(ours + 1, roots=(str(shm), str(mq)))
    assert sorted(os.listdir(shm)) == ["dir", "dirlink", "rootlink", "seg"]
    assert os.listdir(mq) == ["queue"]
    runner.wipe_ipc_leftovers(ours, roots=(str(shm), str(mq), str(tmp_path / "absent")))
    assert os.listdir(shm) == [] and os.listdir(mq) == []
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert os.path.exists("/etc/passwd")


@_needs_ownership
@_needs_dir_fd
def test_a_deep_tree_under_an_ipc_root_is_removed(tmp_path):
    shm = tmp_path / "shm"
    (shm / "deep").mkdir(parents=True)
    _deep_tree(shm / "deep", 1100)
    runner.wipe_ipc_leftovers(os.lstat(shm / "deep").st_uid, roots=(str(shm),))
    assert os.listdir(shm) == []


def test_missing_ipc_roots_are_not_an_error(tmp_path):
    runner.wipe_ipc_leftovers(20001, roots=(str(tmp_path / "shm"), str(tmp_path / "mqueue")))


# --- capture -------------------------------------------------------------------------


def test_capture_keeps_head_and_tail():
    cap = runner._Capture(io.BytesIO(b"A" * 10 + b"B" * 100 + b"C" * 10), 20)
    text = cap.text()
    assert text.startswith("A" * 10) and text.endswith("C" * 10) and "omitted" in text


def test_capture_holds_at_most_its_limit():
    """M1: a stream far larger than the limit keeps the limit, not the stream."""
    cap = runner._Capture(io.BytesIO(bytes(range(256)) * 4000), 1000)
    cap.text()
    assert len(cap.head) == 500
    assert cap._tail_len < 500 + 65536


def test_capture_limits_are_below_the_hermes_line():
    assert runner.CAPTURE_LIMIT_STDOUT == 64_000 and runner.CAPTURE_LIMIT_STDERR == 16_000


# --- relay ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self):
        self.done = False

    def poll(self):
        return 0 if self.done else None

    def wait(self, timeout=None):
        return 0


def test_relay_forwards_a_call_and_returns_the_result():
    child_side, script_side = socket.socketpair()
    hermes_side, runner_side = socket.socketpair()
    proc = _FakeProc()
    rfile, wfile = runner_side.makefile("rb"), runner_side.makefile("wb")

    def hermes():
        f_in, f_out = hermes_side.makefile("rb"), hermes_side.makefile("wb")
        msg = json.loads(f_in.readline())
        assert msg["op"] == "call" and msg["tool"] == "mcp__ragnarok__query"
        f_out.write(json.dumps({"op": "result", "id": msg["id"], "result": '{"ok": 1}'}).encode() + b"\n")
        f_out.flush()

    threading.Thread(target=hermes, daemon=True).start()
    script_side.sendall(json.dumps({"tool": "mcp__ragnarok__query", "args": {}}).encode() + b"\n")
    result = {}

    def run():
        result["finished"] = runner.relay(child_side, runner_side, rfile, wfile, proc, time.monotonic() + 5)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    script_side.settimeout(5)
    assert script_side.recv(1024).strip() == b'{"ok": 1}'
    proc.done = True
    t.join(timeout=5)
    assert result["finished"] == "exited"


def test_relay_reports_a_timeout():
    child_side, _script_side = socket.socketpair()
    _hermes_side, runner_side = socket.socketpair()
    finished = runner.relay(
        child_side, runner_side, runner_side.makefile("rb"), runner_side.makefile("wb"),
        _FakeProc(), time.monotonic() + 0.3,
    )
    assert finished == "timeout"


def _read_all(sock) -> bytes:
    sock.settimeout(5)
    data = b""
    while chunk := sock.recv(65536):
        data += chunk
    return data


def _relay(script_sends: bytes, hermes_replies: bytes = b"", *, wall: float = 5.0):
    """Run the relay to the end over two socket pairs. The script half-closes after
    sending, so the relay sees EOF and the fake process then 'exits'. Returns the
    outcome, what the script received and the frames Hermes received."""
    child, script = socket.socketpair()
    hermes, runner_side = socket.socketpair()
    script.sendall(script_sends)
    script.shutdown(socket.SHUT_WR)
    if hermes_replies:
        hermes.sendall(hermes_replies)
    hermes.shutdown(socket.SHUT_WR)
    rfile, wfile = runner_side.makefile("rb"), runner_side.makefile("wb")
    outcome = runner.relay(child, runner_side, rfile, wfile, _FakeProc(), time.monotonic() + wall)
    for obj in (child, rfile, wfile, runner_side):
        obj.close()
    frames = [json.loads(line) for line in _read_all(hermes).splitlines()]
    return outcome, _read_all(script), frames


def _reply(seq: int, result) -> bytes:
    return json.dumps({"op": "result", "id": seq, "result": result}).encode() + b"\n"


def test_a_line_that_is_not_an_object_is_a_malformed_call():
    """I2: [1,2], 1 and "x" parse, but are not tool calls; the relay carries on."""
    call = json.dumps({"tool": "t", "args": {}}).encode() + b"\n"
    outcome, got, frames = _relay(b'[1,2]\n1\n"x"\nnot json\n' + call, _reply(1, "ok"))
    assert outcome == "exited"
    assert got == runner.MALFORMED * 4 + b"ok\n"
    assert [f["id"] for f in frames] == [1]


def test_a_lone_surrogate_in_a_call_reaches_hermes_escaped():
    outcome, got, frames = _relay(b'{"tool": "t", "args": {"q": "\\ud800"}}\n', _reply(1, "ok"))
    assert outcome == "exited" and got == b"ok\n"
    assert frames[0]["args"] == {"q": "\ud800"}


def test_a_result_with_a_newline_is_sent_json_encoded():
    """M2: a raw newline would end the script's line early; the stub unwraps a JSON string."""
    call = b'{"tool": "t", "args": {}}\n'
    replies = _reply(1, "line one\nline two") + _reply(2, {"k": 1}) + _reply(3, '{"ok": 1}')
    outcome, got, _ = _relay(call * 3, replies)
    assert outcome == "exited"
    assert got.splitlines() == [b'"line one\\nline two"', b'{"k": 1}', b'{"ok": 1}']


@pytest.mark.parametrize(
    "reply",
    [
        _reply(2, "ok"),                 # answers another call
        b"[1]\n",                        # not an object
        b'{"op": "call", "id": 1}\n',    # not a result
        b"not json\n",                   # malformed
        b"",                             # Hermes went away
    ],
)
def test_a_broken_reply_from_hermes_is_a_protocol_outcome(reply):
    """I2/M4: never an exception out of the relay."""
    outcome, got, _ = _relay(b'{"tool": "t", "args": {}}\n', reply)
    assert outcome == "protocol" and got == b""


def test_a_reply_that_never_comes_ends_at_the_wall_clock(monkeypatch):
    monkeypatch.setattr(runner, "HERMES_GRACE_SECONDS", 0)
    child, script = socket.socketpair()
    _hermes, runner_side = socket.socketpair()
    script.sendall(b'{"tool": "t", "args": {}}\n')
    started = time.monotonic()
    outcome = runner.relay(child, runner_side, runner_side.makefile("rb"),
                           runner_side.makefile("wb"), _FakeProc(), started + 0.3)
    assert outcome == "timeout" and time.monotonic() - started < 5


def test_an_endless_line_is_too_large(monkeypatch):
    """I3: bytes with no newline must not grow the root daemon's buffer unbounded."""
    monkeypatch.setattr(runner, "MAX_LINE", 1000)
    child, script = socket.socketpair()
    _hermes, runner_side = socket.socketpair()
    script.sendall(b"x" * 5000)
    started = time.monotonic()
    outcome = runner.relay(child, runner_side, runner_side.makefile("rb"),
                           runner_side.makefile("wb"), _FakeProc(), started + 5)
    assert outcome == "too_large" and time.monotonic() - started < 2


def test_a_complete_line_longer_than_the_limit_is_too_large(monkeypatch):
    monkeypatch.setattr(runner, "MAX_LINE", 1000)
    outcome, got, frames = _relay(b"x" * 2000 + b"\n")
    assert outcome == "too_large" and got == b"" and frames == []


def test_a_call_whose_frame_outgrows_the_hermes_line_is_too_large(monkeypatch):
    """The line fits, but escaping it for Hermes (one e-acute is six bytes) would not."""
    monkeypatch.setattr(runner, "MAX_LINE", 1000)
    line = json.dumps({"tool": "t", "args": {"q": "\u00e9" * 300}}, ensure_ascii=False)
    assert len(line.encode()) < 1000
    outcome, _, frames = _relay(line.encode() + b"\n")
    assert outcome == "too_large" and frames == []


def test_buffered_calls_stop_at_the_wall_clock(monkeypatch):
    """N3: once the deadline passes, a call already buffered is not served."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(runner, "time",
                        types.SimpleNamespace(monotonic=lambda: clock["now"], sleep=time.sleep))
    child, script = socket.socketpair()
    hermes, runner_side = socket.socketpair()
    script.sendall(b'{"tool": "t", "args": {}}\n' * 2)
    hermes.sendall(_reply(1, "ok") + _reply(2, "ok"))
    rfile, wfile = runner_side.makefile("rb"), runner_side.makefile("wb")

    class _SlowHermes:
        """Each call takes Hermes a minute: the first one outlasts the wall clock."""

        def write(self, data):
            clock["now"] += 60
            return wfile.write(data)

        def flush(self):
            wfile.flush()

    outcome = runner.relay(child, runner_side, rfile, _SlowHermes(), _FakeProc(),
                           clock["now"] + 30)
    for obj in (child, rfile, wfile, runner_side):
        obj.close()
    frames = [json.loads(line) for line in _read_all(hermes).splitlines()]
    assert outcome == "timeout"
    assert [f["id"] for f in frames] == [1]
    assert _read_all(script) == b"ok\n"


class _FakeChild:
    """The runner's end of the script's socket, where sendall can be made to fail."""

    def __init__(self, lines: bytes, send_error: Exception | None):
        self._chunks = [lines]
        self._send_error = send_error
        self.timeout = None
        self.sent_with_timeout = []
        self.recvs_after_send = 0

    def settimeout(self, value):
        self.timeout = value

    def recv(self, n):
        if self.sent_with_timeout:
            self.recvs_after_send += 1
        if self._chunks:
            return self._chunks.pop(0)
        time.sleep(0.01)
        raise socket.timeout

    def sendall(self, data):
        self.sent_with_timeout.append(self.timeout)
        if self._send_error:
            raise self._send_error


@pytest.mark.parametrize(
    ("error", "expected"),
    [(None, "timeout"), (socket.timeout(), "timeout"), (BrokenPipeError(), "exited")],
)
def test_replies_to_the_script_get_the_wall_clock_not_the_poll_timeout(error, expected):
    """I2: a send to the script runs under the remaining wall time (at least 1 s), and
    the poll timeout comes back afterwards. A timed-out send ends the run; a broken
    pipe means the script went away, and its exit decides the status."""
    child = _FakeChild(b"[1]\n", error)
    outcome = runner.relay(child, None, None, None, _FakeProc(), time.monotonic() + 0.5)
    assert outcome == expected
    assert len(child.sent_with_timeout) == 1 and child.sent_with_timeout[0] >= 1.0
    assert child.timeout == runner.POLL_SECONDS
    if isinstance(error, BrokenPipeError):
        assert child.recvs_after_send == 0


@pytest.mark.parametrize("hang_up", ["close", "unsolicited"])
def test_the_relay_notices_hermes_hanging_up_mid_run(hang_up):
    """Task 7 fix round 1: a run whose Hermes went away (a stopped turn) ends at the next
    poll tick, not at its next bridged call — a pure-compute orphan would otherwise hold
    its slot for the whole wall clock. Hermes sends nothing unasked mid-run, so data is a
    hang-up too."""
    child, _script = socket.socketpair()
    hermes, runner_side = socket.socketpair()
    result = {}

    def run():
        result["outcome"] = runner.relay(
            child, runner_side, runner_side.makefile("rb"), runner_side.makefile("wb"),
            _FakeProc(), time.monotonic() + 10,
        )
        result["at"] = time.monotonic()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.3)  # the relay is ticking, with nothing from the script
    hung_up_at = time.monotonic()
    if hang_up == "close":
        hermes.close()
    else:
        hermes.sendall(b"x")
    thread.join(5)
    assert result["outcome"] == "protocol"
    assert result["at"] - hung_up_at < 1.0


# --- run_job -------------------------------------------------------------------------


class _Launched:
    """What Popen would return: a process that has already exited, or runs until killed."""

    def __init__(self, returncode=0, stdout=b"", stderr=b"", running=False):
        self.returncode = None if running else returncode
        self._exit = returncode
        self.stdout, self.stderr = io.BytesIO(stdout), io.BytesIO(stderr)
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class _Unreapable(_Launched):
    def __init__(self):
        super().__init__(running=True)

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("launch.py", timeout)


@pytest.fixture
def job(tmp_path, monkeypatch, mount_point):
    """run_job with the slot machinery faked: one slot, no real processes."""
    (mount_point / "left" / "behind").mkdir(parents=True)
    state = {"kills": [], "kill_results": [], "wiped": [], "proc": _Launched(),
             "slot": mount_point}
    pool = runner.SlotPool((20001,))
    monkeypatch.setattr(runner, "POOL", pool)
    monkeypatch.setattr(runner, "WORK_ROOT", str(tmp_path))
    monkeypatch.setattr(runner, "prepare_workdir", lambda uid, files: str(tmp_path))

    def kill_uid(uid, proc_root="/proc"):
        state["kills"].append(uid)
        return state["kill_results"].pop(0) if state["kill_results"] else True

    monkeypatch.setattr(runner, "kill_uid", kill_uid)
    monkeypatch.setattr(runner, "wipe_ipc_leftovers",
                        lambda uid, roots=None: state["wiped"].append(uid))
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **kw: state["proc"])
    state["pool"] = pool

    def run(request=None):
        request = {"op": "run", "code": "print(1)", "stubs": ""} if request is None else request
        return runner.run_job(None, None, None, request)

    state["run"] = run
    return state


def test_a_clean_run_returns_the_slot(job):
    job["proc"] = _Launched(0, b"hello", b"")
    frame = job["run"]()
    assert frame["status"] == "success" and frame["stdout"] == "hello"
    assert job["wiped"] == [20001]
    assert job["slot"].is_dir() and list(job["slot"].iterdir()) == []
    assert job["pool"].acquire(0.01) == 20001


def test_a_slot_whose_processes_survive_is_quarantined(job, capsys):
    """I1: fail closed — the pool does not get the uid back."""
    job["kill_results"] = [False] * 10
    job["run"]()
    assert job["pool"].acquire(0.01) is None
    assert job["wiped"] == []
    assert capsys.readouterr().err.strip() == (
        "sandbox: slot 20001 quarantined: processes survived cleanup")


def test_a_survivor_at_the_pre_read_kill_quarantines_the_slot(job, capsys):
    job["kill_results"] = [False, True]
    job["run"]()
    assert job["kills"] == [20001, 20001]
    assert job["pool"].acquire(0.01) is None
    assert "quarantined" in capsys.readouterr().err


def test_the_ipc_wipe_still_runs_when_the_clear_faults(job, monkeypatch, capsys):
    """N1: a run must not keep its /dev/shm entries by making its own clear fail."""
    def fail(path):
        raise RecursionError

    monkeypatch.setattr(runner, "clear_dir", fail)
    job["run"]()
    assert job["wiped"] == [20001]
    assert job["pool"].acquire(0.01) is None
    assert capsys.readouterr().err.strip() == (
        "sandbox: slot 20001 quarantined: its work directory could not be cleared")


def test_a_tree_past_the_operation_cap_quarantines_the_slot(job, monkeypatch):
    monkeypatch.setattr(runner, "MAX_REMOVE_OPS", 5)
    for i in range(20):
        (job["slot"] / f"f{i}").write_bytes(b"x")
    job["run"]()
    assert job["wiped"] == [20001]
    assert job["pool"].acquire(0.01) is None


def test_busy_says_how_many_slots_are_quarantined(job, monkeypatch):
    monkeypatch.setattr(runner, "BUSY_WAIT_SECONDS", 0.01)
    held = job["pool"].acquire(0.01)
    frame = job["run"]()
    assert (frame["status"], frame["stderr"]) == ("busy", "")
    job["pool"].release(held)
    job["kill_results"] = [False, False]
    job["run"]()
    frame = job["run"]()
    assert (frame["status"], frame["stderr"]) == ("busy", "1 of 1 sandbox slots quarantined")


def test_an_unreaped_process_reports_exit_code_minus_one(job, monkeypatch):
    """N5: a reap that times out leaves returncode None; the frame says -1."""
    monkeypatch.setattr(runner, "relay", lambda *a: "exited")
    job["proc"] = _Unreapable()
    frame = job["run"]()
    assert frame["exit_code"] == -1 and frame["status"] == "error"
    assert job["pool"].acquire(0.01) is None
    assert runner._finish("exited", time.monotonic(), None, "", "")["exit_code"] == -1


def test_a_runner_fault_still_ends_in_a_done_frame(job, monkeypatch):
    """I2: only the exception's class reaches the frame, and cleanup still runs."""
    def boom(uid, files):
        raise RuntimeError("/work/slot-20001/secret: detail")

    monkeypatch.setattr(runner, "prepare_workdir", boom)
    frame = job["run"]()
    assert frame["op"] == "done" and frame["status"] == "error"
    assert frame["stderr"] == "sandbox runner error: RuntimeError"
    assert job["pool"].acquire(0.01) == 20001


def test_a_relay_fault_closes_the_socket_and_reaps_the_process(job, monkeypatch):
    seen = {}

    def relay(child, *a):
        seen["parent"] = child
        raise ValueError("boom")

    monkeypatch.setattr(runner, "relay", relay)
    job["proc"] = _Launched(running=True)
    frame = job["run"]()
    assert frame["status"] == "error" and frame["stderr"] == "sandbox runner error: ValueError"
    assert seen["parent"].fileno() == -1
    assert job["proc"].killed


@pytest.mark.parametrize(
    ("outcome", "returncode", "status", "stderr"),
    [
        ("exited", 0, "success", "trace"),
        ("exited", 2, "error", "trace"),
        ("timeout", 0, "timeout", "trace"),
        ("protocol", 0, "error", "trace\n" + runner.LOST_GATEWAY),
        ("too_large", 0, "error", "trace\n" + runner.TOO_LARGE),
    ],
)
def test_relay_outcomes_map_to_done_statuses(job, monkeypatch, outcome, returncode, status, stderr):
    """M4."""
    monkeypatch.setattr(runner, "relay", lambda *a: outcome)
    job["proc"] = _Launched(returncode, b"", b"trace")
    frame = job["run"]()
    assert (frame["status"], frame["stderr"]) == (status, stderr)
    assert frame["exit_code"] == (returncode if outcome == "exited" else -1)


def test_the_done_frame_carries_capped_output(job):
    """M1: 64 KB of stdout and 16 KB of stderr, head and tail."""
    job["proc"] = _Launched(0, b"o" * 300_000, b"e" * 100_000)
    frame = job["run"]()
    marker = len("\n... [000000 bytes omitted] ...\n")
    out, err = runner.CAPTURE_LIMIT_STDOUT, runner.CAPTURE_LIMIT_STDERR
    assert out < len(frame["stdout"]) <= out + marker
    assert err < len(frame["stderr"]) <= err + marker


def test_run_job_refuses_a_request_that_is_not_an_object(job):
    assert job["run"]([1, 2])["stderr"] == "expected a run request"
    assert job["pool"].acquire(0.01) == 20001


# --- the connection handler -----------------------------------------------------------


@pytest.mark.parametrize(
    "request_line",
    [b"[1, 2]\n", b'"run"\n', b'{"op": "run", "limits": [1]}\n', b'{"op": "run", "code": [1]}\n',
     b'{"op": "nope"}\n'],
)
def test_the_handler_answers_a_bad_request_with_a_done_frame(request_line):
    """I2: a non-dict request or non-dict limits must not raise in the handler."""
    client, server_side = socket.socketpair()
    client.sendall(request_line)
    runner._Handler(server_side, ("", 0), None)
    client.settimeout(5)
    frame = json.loads(client.makefile("rb").readline())
    assert frame["op"] == "done" and frame["status"] == "error"
    assert frame["stderr"] == "expected a run request"


# --- the launcher --------------------------------------------------------------------


@pytest.fixture
def launch(monkeypatch):
    pytest.importorskip("resource")
    spec = importlib.util.spec_from_file_location("sandbox_launch", _SANDBOX / "launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events = []

    class _Exec(Exception):
        pass

    def execve(*a):
        raise _Exec

    monkeypatch.setattr(module.resource, "setrlimit",
                        lambda which, lim: events.append((which, lim)))
    monkeypatch.setattr(module.os, "setgroups", lambda groups: events.append("setgroups"))
    monkeypatch.setattr(module.os, "setgid", lambda gid: events.append("setgid"))
    monkeypatch.setattr(module.os, "chdir", lambda path: None)
    monkeypatch.setattr(module.os, "execve", execve)
    module.events, module.Exec = events, _Exec
    return module


def test_launcher_limits_processes_and_core_dumps(launch, monkeypatch, tmp_path):
    """I4: NPROC 8, no core file."""
    monkeypatch.setattr(launch, "OOM_SCORE_ADJ", str(tmp_path / "oom"))
    monkeypatch.setattr(launch.os, "setuid", lambda uid: launch.events.append("setuid"))
    with pytest.raises(launch.Exec):
        launch.main(["20001", "60", "768", str(tmp_path), "5"])
    limits = dict(e for e in launch.events if isinstance(e, tuple))
    assert limits[launch.resource.RLIMIT_NPROC] == (8, 8)
    assert limits[launch.resource.RLIMIT_CORE] == (0, 0)


def test_launcher_raises_the_oom_score_before_dropping_privileges(launch, monkeypatch, tmp_path):
    """I4: a run, not the daemon, is the OOM killer's first choice."""
    oom = tmp_path / "oom"
    monkeypatch.setattr(launch, "OOM_SCORE_ADJ", str(oom))
    at_setuid = []
    monkeypatch.setattr(launch.os, "setuid",
                        lambda uid: at_setuid.append(oom.read_text(encoding="ascii")))
    with pytest.raises(launch.Exec):
        launch.main(["20001", "60", "768", str(tmp_path), "5"])
    assert at_setuid == ["1000"]


def test_launcher_carries_on_when_the_oom_score_cannot_be_written(launch, monkeypatch, tmp_path):
    monkeypatch.setattr(launch, "OOM_SCORE_ADJ", str(tmp_path / "absent" / "oom"))
    monkeypatch.setattr(launch.os, "setuid", lambda uid: launch.events.append("setuid"))
    with pytest.raises(launch.Exec):
        launch.main(["20001", "60", "768", str(tmp_path), "5"])
    assert "setuid" in launch.events
