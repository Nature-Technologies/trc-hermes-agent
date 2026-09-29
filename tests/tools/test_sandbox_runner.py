"""Pure parts of the sandbox runner (deploy/trc/sandbox/runner.py). The process,
privilege and isolation behaviour is verified against the real container by
deploy/trc/sandbox/adversarial_check.py (Task 8)."""

from __future__ import annotations

import importlib.util
import io
import json
import socket
import threading
import time
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "deploy" / "trc" / "sandbox" / "runner.py"
_spec = importlib.util.spec_from_file_location("sandbox_runner", _PATH)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


def test_limits_are_clamped_to_the_ceiling():
    """Review Focus 3."""
    got = runner.clamp_limits({"wall": 9999, "cpu": 9999, "mem_mb": 99999})
    assert got == runner.CEILINGS


def test_smaller_limits_are_honoured_and_junk_falls_back():
    assert runner.clamp_limits({"wall": 30, "cpu": "x"})["wall"] == 30
    assert runner.clamp_limits({"cpu": "x"})["cpu"] == runner.DEFAULT_LIMITS["cpu"]


def test_framing_round_trip():
    buf = io.BytesIO()
    runner.send(buf, {"op": "done", "text": "é"})
    buf.seek(0)
    assert runner.recv(buf) == {"op": "done", "text": "é"}
    assert runner.recv(buf) is None


def test_slot_pool_answers_busy_when_exhausted():
    pool = runner.SlotPool((1, 2))
    assert {pool.acquire(0.01), pool.acquire(0.01)} == {1, 2}
    assert pool.acquire(0.01) is None
    pool.release(1)
    assert pool.acquire(0.01) == 1


def test_uid_pids_reads_a_proc_tree(tmp_path):
    for pid, uid in ((10, 20001), (11, 20002), (12, 20001)):
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "status").write_text(f"Name:\tpython\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n", encoding="utf-8")
    (tmp_path / "self").mkdir()
    assert sorted(runner.uid_pids(20001, proc_root=str(tmp_path))) == [10, 12]


def test_capture_keeps_head_and_tail(monkeypatch):
    monkeypatch.setattr(runner, "CAPTURE_LIMIT", 20)
    cap = runner._Capture(io.BytesIO(b"A" * 10 + b"B" * 100 + b"C" * 10))
    text = cap.text()
    assert text.startswith("A" * 10) and text.endswith("C" * 10) and "omitted" in text


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
    assert result["finished"] is True


def test_relay_reports_a_timeout():
    child_side, _script_side = socket.socketpair()
    _hermes_side, runner_side = socket.socketpair()
    finished = runner.relay(
        child_side, runner_side, runner_side.makefile("rb"), runner_side.makefile("wb"),
        _FakeProc(), time.monotonic() + 0.3,
    )
    assert finished is False
