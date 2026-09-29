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
from tools.interrupt import clear_current_thread_interrupt, set_interrupt

af_unix = pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX")

Q = "mcp__ragnarok__query"
TRANSPORT_ENV = "HERMES_CODE_EXECUTION_TRANSPORT"
INTERRUPTED = "\n[execution interrupted — user sent a new message]"


@pytest.fixture(autouse=True)
def _clean_slate(monkeypatch):
    """No transport pinned by the host running the tests, and no interrupt bit left on
    this thread by a test before or by this one."""
    monkeypatch.delenv(TRANSPORT_ENV, raising=False)
    clear_current_thread_interrupt()
    yield
    clear_current_thread_interrupt()


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


@af_unix
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


@af_unix
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


@af_unix
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


@af_unix
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
# Beyond the plan: the controller's notes on the brief, run_in_sidecar's
# "never raises" contract, and fix round 1.
# ---------------------------------------------------------------------------

_DONE_OK = {"op": "done", "status": "success", "exit_code": 0, "stdout": "", "stderr": "", "duration_seconds": 0.1}
_DEADLINE = {"status": "timeout", "error": "overall deadline"}


def _sock_path():
    return os.path.join(tempfile.mkdtemp(), "sock")


def _write(fp, obj) -> None:
    fp.write(json.dumps(obj).encode() + b"\n")
    fp.flush()


def _serve_once(path, handler):
    """Accept one connection on `path` and hand it to `handler(conn)`. Returns the
    serving thread."""
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

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread


def _silent(conn):
    """Read the run request, then say nothing until the client hangs up (or 5 s pass)."""
    conn.settimeout(5.0)
    conn.makefile("rb").readline()
    try:
        conn.recv(1)
    except socket.timeout:
        conn.sendall(json.dumps(_DONE_OK).encode() + b"\n")


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


@af_unix
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


@af_unix
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


@af_unix
@pytest.mark.parametrize(
    "stdout, output",
    [
        ("partial 12.5%", "partial 12.5%\n\n⏰ " + cet._SIDECAR_ERRORS["timeout"]),
        ("", "⏰ " + cet._SIDECAR_ERRORS["timeout"]),
    ],
    ids=["with-output", "no-output"],
)
def test_a_timeout_is_readable_and_its_output_is_still_recorded(stdout, output):
    """The timeout is in the output too, as on the local path (#10807): an empty output
    reads to the model as "nothing happened"."""
    path = _sock_path()
    _fake_runner(path, [{
        "op": "done", "status": "timeout", "exit_code": -1,
        "stdout": stdout, "stderr": "", "duration_seconds": 240.0,
    }])
    record = {}
    out = _run_sidecar(_config(path), record=record)
    assert out["status"] == "timeout"
    assert out["error"] == cet._SIDECAR_ERRORS["timeout"]
    assert out["output"] == output
    record["record_computed"].assert_called_once_with(stdout, "ragnarok")


@af_unix
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


@af_unix
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
            _write(f_out, {"op": "call", "id": seq, "tool": "t", "args": {}})
            if not f_in.readline():
                return
        _write(f_out, _DONE_OK)

    _serve_once(path, endless_calls)
    started = time.monotonic()
    done = run_in_sidecar(path, "x", "", {}, lambda tool, args: "{}", overall_timeout=0.5)
    assert done == _DEADLINE
    assert time.monotonic() - started < 3.0


@af_unix
@pytest.mark.parametrize("should_stop", [None, lambda: False], ids=["no-check", "never-stops"])
def test_a_runner_that_goes_silent_ends_at_the_deadline(should_stop):
    """Fix round 1: the wait now ticks to look at should_stop; a tick that finds no stop
    still ends at the deadline."""
    from tools.sidecar_sandbox import run_in_sidecar

    path = _sock_path()
    _serve_once(path, _silent)
    started = time.monotonic()
    done = run_in_sidecar(path, "x", "", {}, lambda tool, args: "{}",
                          overall_timeout=0.5, should_stop=should_stop)
    assert done == _DEADLINE
    assert time.monotonic() - started < 3.0


@af_unix
def test_a_runner_that_stops_reading_ends_at_the_deadline():
    """A send that cannot complete (the runner never reads) is the deadline too."""
    from tools.sidecar_sandbox import run_in_sidecar

    path = _sock_path()
    _serve_once(path, lambda conn: time.sleep(3.0))
    started = time.monotonic()
    done = run_in_sidecar(path, "x" * 8_000_000, "", {}, lambda tool, args: "{}",
                          overall_timeout=0.5)
    assert done == _DEADLINE
    assert time.monotonic() - started < 2.5


@af_unix
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


@af_unix
def test_tool_calls_are_served_on_the_calling_thread():
    """The end-user identity and chat id live in ContextVars on the execute_code thread;
    a call served on any other thread would be made without them."""
    from tools.sidecar_sandbox import run_in_sidecar

    path = _sock_path()
    _fake_runner(path, [{"op": "call", "id": 1, "tool": Q, "args": {}}, _DONE_OK])
    served_on = []

    def on_call(tool, args):
        served_on.append(threading.get_ident())
        return "{}"

    done = run_in_sidecar(path, "x", "", {}, on_call, overall_timeout=10.0)
    assert done["status"] == "success"
    assert served_on == [threading.get_ident()]


@af_unix
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


@af_unix
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


@af_unix
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


# --- Fix round 1, I1: a stopped turn's run stops -----------------------------------


@af_unix
def test_a_stop_during_a_call_ends_the_run_before_the_next_one():
    """The orphaned run of a stopped turn must not go on making bridged calls: the
    backend keys turn scope by conversation, so a later call would land in the NEXT
    turn's scope."""
    from tools.sidecar_sandbox import run_in_sidecar

    path = _sock_path()
    seen = {}

    def two_calls(conn):
        f_in, f_out = conn.makefile("rb"), conn.makefile("wb")
        f_in.readline()
        _write(f_out, {"op": "call", "id": 1, "tool": Q, "args": {}})
        seen["first"] = json.loads(f_in.readline())
        try:
            _write(f_out, {"op": "call", "id": 2, "tool": Q, "args": {}})
            seen["after"] = f_in.readline()
        except OSError:  # EPIPE, or ECONNRESET for a close with call 2 unread
            seen["after"] = b""

    thread = _serve_once(path, two_calls)
    stop = threading.Event()

    def on_call(tool, args):
        stop.set()  # the user pressed stop while this call ran
        return "{}"

    started = time.monotonic()
    done = run_in_sidecar(path, "x", "", {}, on_call, should_stop=stop.is_set,
                          overall_timeout=30.0)
    elapsed = time.monotonic() - started
    thread.join(5)
    assert done == {"status": "interrupted"}
    assert elapsed < 1.5
    assert seen["first"]["id"] == 1
    assert seen["after"] == b""  # the connection closed: no second result


@af_unix
def test_a_call_already_received_is_not_served_once_stopped():
    """The stop is checked before each call is served, not only while waiting: a call
    frame that arrived with the stop must not reach the backend."""
    from tools.sidecar_sandbox import run_in_sidecar

    path = _sock_path()

    def two_calls_at_once(conn):
        conn.makefile("rb").readline()
        conn.sendall(b"".join(
            json.dumps({"op": "call", "id": seq, "tool": Q, "args": {}}).encode() + b"\n"
            for seq in (1, 2)
        ))
        conn.settimeout(5.0)
        while conn.recv(65536):
            pass

    _serve_once(path, two_calls_at_once)
    stop = threading.Event()
    served = []

    def on_call(tool, args):
        served.append(tool)
        stop.set()
        return "{}"

    done = run_in_sidecar(path, "x", "", {}, on_call, should_stop=stop.is_set,
                          overall_timeout=10.0)
    assert done == {"status": "interrupted"}
    assert served == [Q]


@af_unix
def test_a_turn_stopped_before_the_run_never_starts_it():
    from tools.sidecar_sandbox import run_in_sidecar

    path = _sock_path()
    received = {}

    def record(conn):
        conn.settimeout(5.0)
        received["run"] = conn.makefile("rb").readline()

    thread = _serve_once(path, record)
    done = run_in_sidecar(path, "x", "", {}, lambda tool, args: "{}",
                          should_stop=lambda: True, overall_timeout=10.0)
    thread.join(5)
    assert done == {"status": "interrupted"}
    assert received["run"] == b""  # no run request was sent


@af_unix
@pytest.mark.parametrize(
    "data",
    [b"x" * 5000, json.dumps({**_DONE_OK, "stdout": "x" * 2000}).encode() + b"\n"],
    ids=["no-newline", "long-valid-frame"],
)
def test_a_frame_longer_than_the_limit_is_unavailable(data, monkeypatch):
    """The client's own line buffer is bounded, as readline(MAX_LINE) bounded it."""
    from tools import sidecar_sandbox

    monkeypatch.setattr(sidecar_sandbox, "MAX_LINE", 1000)
    path = _sock_path()

    def long_frame(conn):
        conn.makefile("rb").readline()
        conn.sendall(data)
        conn.settimeout(5.0)
        conn.recv(1)

    _serve_once(path, long_frame)
    started = time.monotonic()
    done = sidecar_sandbox.run_in_sidecar(path, "x", "", {}, lambda tool, args: "{}",
                                          overall_timeout=5.0)
    assert done["status"] == "unavailable"
    assert time.monotonic() - started < 2.5


@af_unix
def test_a_should_stop_that_raises_is_a_stop():
    """Fail closed: a stop check that cannot answer ends the run."""
    from tools.sidecar_sandbox import run_in_sidecar

    path = _sock_path()
    _serve_once(path, _silent)

    def broken():
        raise RuntimeError("no answer")

    started = time.monotonic()
    done = run_in_sidecar(path, "x", "", {}, lambda tool, args: "{}",
                          should_stop=broken, overall_timeout=10.0)
    assert done == {"status": "interrupted"}
    assert time.monotonic() - started < 1.5


@af_unix
def test_an_interrupt_stops_the_run_and_nothing_is_recorded(caplog):
    path = _sock_path()
    seen = {"results": []}

    def calls(conn):
        f_in, f_out = conn.makefile("rb"), conn.makefile("wb")
        f_in.readline()
        for seq in (1, 2):
            try:
                _write(f_out, {"op": "call", "id": seq, "tool": Q, "args": {"query": "q"}})
                line = f_in.readline()
            except OSError:  # EPIPE, or ECONNRESET for a close with a call unread
                line = b""
            if not line:
                seen["eof"] = True
                return
            seen["results"].append(json.loads(line))
        _write(f_out, {**_DONE_OK, "stdout": "should never be recorded"})

    thread = _serve_once(path, calls)

    def handle(tool_name, tool_args, task_id=None):
        set_interrupt(True)  # agent.interrupt() flags this, the execute_code thread
        return '{"result": "{}"}'

    caplog.set_level(logging.WARNING, logger="tools.code_execution_tool")
    with (
        patch.object(cet, "_load_config", return_value=_config(path)),
        patch("model_tools.handle_function_call", side_effect=handle) as h,
        patch("tools.trc_sandbox_bridge.record_computed") as rec,
        patch("tools.approval.check_execute_code_guard", return_value={"approved": True}),
    ):
        out = json.loads(cet.execute_code("print(1)", task_id="t", enabled_tools=[Q]))
    thread.join(5)
    assert out["status"] == "interrupted"
    assert out["output"] == INTERRUPTED
    assert out["tool_calls_made"] == 1
    assert h.call_count == 1
    assert len(seen["results"]) == 1 and seen.get("eof")
    rec.assert_not_called()
    assert "execute_code sidecar: interrupted" in caplog.text


@af_unix
def test_client_side_failures_are_logged_by_status_and_class(caplog):
    missing = os.path.join(tempfile.mkdtemp(), "nope")
    caplog.set_level(logging.WARNING, logger="tools.code_execution_tool")
    out = _run_sidecar(_config(missing))
    assert out["status"] == "unavailable"
    assert "execute_code sidecar: unavailable (FileNotFoundError)" in caplog.text


def test_a_denied_script_never_reaches_the_sidecar():
    with (
        patch.object(cet, "_load_config", return_value=_config("/nonexistent/sock")),
        patch("tools.sidecar_sandbox.run_in_sidecar") as run,
        patch("tools.approval.check_execute_code_guard",
              return_value={"approved": False, "message": "denied by policy"}),
    ):
        out = json.loads(cet.execute_code("print(1)", task_id="t", enabled_tools=[Q]))
    assert out["status"] == "error"
    assert out["error"] == "denied by policy"
    run.assert_not_called()


# --- Fix round 1, I2: the transport is pinned and fails closed -----------------------

REFUSED = "execute_code is misconfigured (unknown transport); refusing to run."


class _LocalPathReached(Exception):
    """Raised where upstream's local path starts, standing in for running the script."""


@pytest.mark.parametrize(
    "env, config, expected",
    [
        ("sidecar", {}, "sidecar"),  # config.yaml unreadable: read_raw_config gives {}
        ("  SIDECAR ", {"transport": "bogus"}, "sidecar"),  # the env var wins
        (None, {"transport": "sidecar"}, "sidecar"),
        (None, {"transport": " Sidecar"}, "sidecar"),
        ("sidecr", {"transport": "sidecar"}, "refused"),  # the env var wins, even wrong
        (None, {"transport": "sidecr"}, "refused"),
        (None, {"transport": True}, "refused"),
        ("", {}, "local"),
        (None, {}, "local"),
        (None, {"transport": None}, "local"),
        (None, {"transport": ""}, "local"),
    ],
    ids=[
        "env-only", "env-wins", "config", "config-normalised", "env-typo-refused",
        "config-typo-refused", "config-non-string-refused", "env-empty-is-unset",
        "unset", "config-null-is-unset", "config-empty-is-unset",
    ],
)
def test_the_transport_is_pinned_and_an_unknown_one_is_refused(
    env, config, expected, monkeypatch, caplog,
):
    if env is not None:
        monkeypatch.setenv(TRANSPORT_ENV, env)
    caplog.set_level(logging.ERROR, logger="tools.code_execution_tool")
    with (
        patch.object(cet, "_load_config", return_value=config),
        patch.object(cet, "_execute_sidecar", return_value='{"status": "via-sidecar"}') as sidecar,
        patch.object(cet, "_execute_remote", return_value='{"status": "via-remote"}') as remote,
        patch.object(cet, "resolve_sandbox_tools", side_effect=_LocalPathReached) as local,
        patch.object(cet.subprocess, "Popen") as popen,
        patch("tools.sidecar_sandbox.run_in_sidecar") as socket_run,
        patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local"}),
        patch("tools.terminal_tool._docker_has_host_access", return_value=False),
        patch("tools.approval.check_execute_code_guard", return_value={"approved": True}),
    ):
        try:
            out = json.loads(cet.execute_code("print(1)", task_id="t", enabled_tools=[Q]))
        except _LocalPathReached:
            out = None

    remote.assert_not_called()
    popen.assert_not_called()
    socket_run.assert_not_called()
    if expected == "sidecar":
        assert out == {"status": "via-sidecar"}
        sidecar.assert_called_once_with("print(1)", "t", [Q])
        local.assert_not_called()
    elif expected == "refused":
        assert out == {
            "status": "error", "error": REFUSED, "tool_calls_made": 0, "duration_seconds": 0,
        }
        sidecar.assert_not_called()
        local.assert_not_called()
        assert any(r.levelno == logging.ERROR for r in caplog.records)
    else:
        assert out is None  # upstream's local path, unchanged
        sidecar.assert_not_called()


def test_the_description_follows_the_pinned_transport(monkeypatch):
    """The model is told scripts run in the sandbox whenever they do — the env var pins
    the transport even with no config."""
    monkeypatch.setenv(TRANSPORT_ENV, "sidecar")
    with patch.object(cet, "_load_config", return_value={"sandbox_tools": [Q]}):
        schema = cet.build_execute_code_schema(enabled_sandbox_tools={Q}, mode="project")
    assert "isolated sandbox with no network" in schema["description"]
    assert "session's working directory" not in schema["description"]
