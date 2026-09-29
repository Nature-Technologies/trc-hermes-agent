"""execute_code over the sidecar transport, against an in-process fake runner."""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
from unittest.mock import patch

import pytest

from tools import code_execution_tool as cet

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX")


def _fake_runner(path, script):
    """Serve one connection, playing `script` (a list of messages) back."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    seen = {}

    def serve():
        conn, _ = srv.accept()
        f_in, f_out = conn.makefile("rb"), conn.makefile("wb")
        seen["run"] = json.loads(f_in.readline())
        for msg in script:
            f_out.write(json.dumps(msg).encode() + b"\n")
            f_out.flush()
            if msg["op"] == "call":
                seen.setdefault("results", []).append(json.loads(f_in.readline()))
        conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return seen


def _config(path):
    return {
        "transport": "sidecar",
        "sidecar_socket": path,
        "sandbox_tools": ["mcp__ragnarok__query"],
        "max_tool_calls": 20,
        "record_computed_server": "ragnarok",
    }


def test_a_run_relays_a_call_and_returns_the_output():
    path = os.path.join(tempfile.mkdtemp(), "sock")
    seen = _fake_runner(path, [
        {"op": "call", "id": 1, "tool": "mcp__ragnarok__query", "args": {"query": "q", "session_id": "x"}},
        {"op": "done", "status": "success", "exit_code": 0, "stdout": "growth 41.5%", "stderr": "", "duration_seconds": 0.1},
    ])
    with (
        patch.object(cet, "_load_config", return_value=_config(path)),
        patch("model_tools.handle_function_call", return_value='{"result": "{}"}') as h,
        patch("tools.trc_sandbox_bridge.record_computed") as rec,
        patch("tools.approval.check_execute_code_guard", return_value={"approved": True}),
    ):
        out = json.loads(cet.execute_code("print(1)", task_id="t", enabled_tools=["mcp__ragnarok__query"]))
    assert out["status"] == "success"
    assert out["output"] == "growth 41.5%"
    assert out["tool_calls_made"] == 1
    assert h.call_args.args[1] == {"query": "q"}  # identity arg stripped
    assert "ragnarok = _McpServer" in seen["run"]["stubs"]
    rec.assert_called_once_with("growth 41.5%", "ragnarok")


def test_an_unreachable_sidecar_is_a_readable_error():
    """Review Focus 5."""
    missing = os.path.join(tempfile.mkdtemp(), "nope")
    with (
        patch.object(cet, "_load_config", return_value=_config(missing)),
        patch("tools.approval.check_execute_code_guard", return_value={"approved": True}),
    ):
        out = json.loads(cet.execute_code("print(1)", task_id="t", enabled_tools=[]))
    assert out["status"] == "unavailable"
    assert "unavailable" in out["error"]


def test_busy_is_passed_through_readably():
    """Review Focus 5."""
    path = os.path.join(tempfile.mkdtemp(), "sock")
    _fake_runner(path, [
        {"op": "done", "status": "busy", "exit_code": -1, "stdout": "", "stderr": "", "duration_seconds": 20.0},
    ])
    with (
        patch.object(cet, "_load_config", return_value=_config(path)),
        patch("tools.approval.check_execute_code_guard", return_value={"approved": True}),
    ):
        out = json.loads(cet.execute_code("print(1)", task_id="t", enabled_tools=[]))
    assert out["status"] == "busy"
    assert "busy" in out["error"]


def test_a_failing_script_returns_its_traceback():
    path = os.path.join(tempfile.mkdtemp(), "sock")
    _fake_runner(path, [
        {"op": "done", "status": "error", "exit_code": 1, "stdout": "", "stderr": "Traceback: KeyError", "duration_seconds": 0.1},
    ])
    with (
        patch.object(cet, "_load_config", return_value=_config(path)),
        patch("tools.approval.check_execute_code_guard", return_value={"approved": True}),
    ):
        out = json.loads(cet.execute_code("x", task_id="t", enabled_tools=[]))
    assert out["status"] == "error"
    assert "KeyError" in out["output"]


# ---------------------------------------------------------------------------
# Beyond the plan: the controller's notes on the brief, and run_in_sidecar's
# "never raises" contract.
# ---------------------------------------------------------------------------

_DONE_OK = {"op": "done", "status": "success", "exit_code": 0, "stdout": "", "stderr": "", "duration_seconds": 0.1}


def _sock_path():
    return os.path.join(tempfile.mkdtemp(), "sock")


def _serve_once(path, handler):
    """Accept one connection on `path` and hand it to `handler(conn)`."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        try:
            handler(conn)
        except OSError:
            pass  # the client went away first
        finally:
            conn.close()
            srv.close()

    threading.Thread(target=serve, daemon=True).start()


def _run_sidecar(config, code="print(1)", enabled_tools=(), record=None):
    with (
        patch.object(cet, "_load_config", return_value=config),
        patch("model_tools.handle_function_call", return_value='{"result": "{}"}') as h,
        patch("tools.trc_sandbox_bridge.record_computed") as rec,
        patch("tools.approval.check_execute_code_guard", return_value={"approved": True}),
    ):
        out = json.loads(cet.execute_code(code, task_id="t", enabled_tools=list(enabled_tools)))
    if record is not None:
        record["handle_function_call"] = h
        record["record_computed"] = rec
    return out


def test_the_run_request_carries_the_script_pipe_stubs_and_limits():
    path = _sock_path()
    seen = _fake_runner(path, [_DONE_OK])
    config = {**_config(path), "sidecar_limits": {"wall": 100}}
    _run_sidecar(config, code="print(2)", enabled_tools=["mcp__ragnarok__query"])
    run = seen["run"]
    assert run["op"] == "run"
    assert run["code"] == "print(2)"
    assert run["limits"] == {"wall": 100}
    assert 'os.environ["HERMES_RPC_FD"]' in run["stubs"]
    assert "HERMES_RPC_TOKEN" not in run["stubs"]  # nothing else can reach the pair


def test_a_refused_call_is_answered_with_json_the_stub_can_parse():
    """Task 6 re-review N6: the runner relays a one-line result verbatim, and the stub
    json.loads it — a reply that is not JSON would raise inside the script."""
    path = _sock_path()
    seen = _fake_runner(path, [
        {"op": "call", "id": 1, "tool": "terminal", "args": {"command": "id"}},
        _DONE_OK,
    ])
    record = {}
    out = _run_sidecar(
        _config(path), enabled_tools=["mcp__ragnarok__query", "terminal"], record=record,
    )
    reply = seen["results"][0]
    assert reply["op"] == "result"
    assert reply["id"] == 1
    assert "not available" in json.loads(reply["result"])["error"]
    record["handle_function_call"].assert_not_called()
    assert out["tool_calls_made"] == 0


def test_a_timeout_is_readable_and_its_output_is_still_recorded():
    path = _sock_path()
    _fake_runner(path, [{
        "op": "done", "status": "timeout", "exit_code": -1,
        "stdout": "partial 12.5%", "stderr": "", "duration_seconds": 240.0,
    }])
    record = {}
    out = _run_sidecar(_config(path), record=record)
    assert out["status"] == "timeout"
    assert out["error"] == cet._SIDECAR_ERRORS["timeout"]
    assert out["output"] == "partial 12.5%"
    record["record_computed"].assert_called_once_with("partial 12.5%", "ragnarok")


@pytest.mark.parametrize(
    "stderr, suffix",
    [
        ("", ""),
        ("1 of 3 sandbox slots quarantined", " (1 of 3 sandbox slots quarantined)"),
    ],
    ids=["no-reason", "quarantined"],
)
def test_busy_says_why_when_the_runner_does(stderr, suffix):
    path = _sock_path()
    _fake_runner(path, [{
        "op": "done", "status": "busy", "exit_code": -1,
        "stdout": "", "stderr": stderr, "duration_seconds": 20.0,
    }])
    out = _run_sidecar(_config(path))
    assert out["status"] == "busy"
    assert out["error"] == cet._SIDECAR_ERRORS["busy"] + suffix


def test_the_overall_timeout_is_a_deadline_not_a_per_read_timeout():
    """A runner that keeps sending calls must not hold Hermes past the deadline."""
    from tools.sidecar_sandbox import run_in_sidecar

    path = _sock_path()

    def endless_calls(conn):
        f_in, f_out = conn.makefile("rb"), conn.makefile("wb")
        f_in.readline()
        give_up = time.monotonic() + 5.0
        seq = 0
        while time.monotonic() < give_up:
            seq += 1
            f_out.write(json.dumps({"op": "call", "id": seq, "tool": "t", "args": {}}).encode() + b"\n")
            f_out.flush()
            if not f_in.readline():
                return
        f_out.write(json.dumps(_DONE_OK).encode() + b"\n")
        f_out.flush()

    _serve_once(path, endless_calls)
    started = time.monotonic()
    done = run_in_sidecar(path, "x", "", {}, lambda tool, args: "{}", overall_timeout=0.5)
    assert done == {"status": "timeout", "error": "overall deadline"}
    assert time.monotonic() - started < 3.0


def test_a_runner_that_goes_silent_ends_at_the_deadline():
    from tools.sidecar_sandbox import run_in_sidecar

    path = _sock_path()

    def silent(conn):
        conn.settimeout(5.0)
        conn.makefile("rb").readline()
        try:
            conn.recv(1)  # until the client hangs up, or the fake gives up
        except socket.timeout:
            conn.sendall(json.dumps(_DONE_OK).encode() + b"\n")

    _serve_once(path, silent)
    started = time.monotonic()
    done = run_in_sidecar(path, "x", "", {}, lambda tool, args: "{}", overall_timeout=0.5)
    assert done == {"status": "timeout", "error": "overall deadline"}
    assert time.monotonic() - started < 3.0


def test_a_tool_call_that_raises_is_answered_and_the_run_goes_on(caplog):
    from tools.sidecar_sandbox import run_in_sidecar

    path = _sock_path()
    seen = _fake_runner(path, [
        {"op": "call", "id": 7, "tool": "mcp__ragnarok__query", "args": {}},
        {**_DONE_OK, "stdout": "carried on"},
    ])

    def boom(tool, args):
        raise RuntimeError("detail that may carry tool data")

    caplog.set_level(logging.WARNING, logger="tools.sidecar_sandbox")
    done = run_in_sidecar(path, "x", "", {}, boom, overall_timeout=10.0)
    assert done["status"] == "success"
    assert done["stdout"] == "carried on"
    reply = seen["results"][0]
    assert reply["id"] == 7
    assert json.loads(reply["result"]) == {"error": "tool call failed: RuntimeError"}
    assert "RuntimeError" in caplog.text  # an operator sees it failed …
    assert "may carry tool data" not in caplog.text  # … never what it held


@pytest.mark.parametrize(
    "line",
    [
        b"[1]\n",
        b'"done"\n',
        b"[" * 100_000 + b"]" * 100_000 + b"\n",
    ],
    ids=["array", "string", "too-deep"],
)
def test_a_frame_that_is_not_an_object_is_unavailable_not_a_crash(line):
    from tools.sidecar_sandbox import run_in_sidecar

    path = _sock_path()

    def send_line(conn):
        conn.makefile("rb").readline()
        conn.sendall(line)

    _serve_once(path, send_line)
    done = run_in_sidecar(path, "x", "", {}, lambda tool, args: "{}", overall_timeout=10.0)
    assert done["status"] == "unavailable"


def test_frames_are_ascii_escaped_so_no_string_can_break_the_encoding():
    """A lone surrogate (json.loads lets one through) must not end the run."""
    from tools.sidecar_sandbox import run_in_sidecar

    path = _sock_path()
    seen = _fake_runner(path, [
        {"op": "call", "id": 1, "tool": "mcp__ragnarok__query", "args": {}},
        _DONE_OK,
    ])
    done = run_in_sidecar(path, "x", "", {}, lambda tool, args: "\ud800 row", overall_timeout=10.0)
    assert done["status"] == "success"
    assert seen["results"][0]["result"] == "\ud800 row"


def test_the_pipe_stub_calls_through_the_inherited_socket(monkeypatch):
    """The stub a sidecar script imports: one line out on HERMES_RPC_FD, one line back."""
    with patch.object(cet, "_load_config", return_value=_config("unused")):
        src = cet.generate_hermes_tools_module(["mcp__ragnarok__query"], transport="pipe")
    ours, theirs = socket.socketpair()
    monkeypatch.setenv("HERMES_RPC_FD", str(theirs.detach()))
    requests = []

    def runner_side():
        requests.append(json.loads(ours.makefile("rb").readline()))
        ours.sendall(json.dumps({"result": json.dumps({"total": 3})}).encode() + b"\n")

    thread = threading.Thread(target=runner_side, daemon=True)
    thread.start()
    namespace: dict = {}
    exec(compile(src, "hermes_tools.py", "exec"), namespace)
    try:
        out = namespace["ragnarok"].query(query="q")
    finally:
        thread.join(5)
        if namespace.get("_sock") is not None:
            namespace["_sock"].close()
        ours.close()
    assert requests == [{"tool": "mcp__ragnarok__query", "args": {"query": "q"}}]
    assert out == {"total": 3}
