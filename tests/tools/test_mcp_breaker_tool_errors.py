"""A tool-level error is a completed round trip, not a transport failure.

The per-server breaker is process-global. Counting a tool's isError replies (an
argument-validation failure, say) toward it let a sandbox script looping over one bad
bridged call open it in under a second, and every user of the server then got
"unreachable ... Do NOT retry" for the full cooldown. Only transport failures count.
"""

import json
from unittest.mock import MagicMock

import pytest

pytest.importorskip("mcp.client.auth.oauth2")

from tests.tools.test_mcp_circuit_breaker import _cleanup, _install_stub_server  # noqa: E402

SERVER = "srv-tool-errors"


def _result(text, *, is_error):
    result = MagicMock()
    result.isError = is_error
    block = MagicMock()
    block.text = text
    result.content = [block]
    result.structuredContent = None
    return result


@pytest.fixture
def mcp(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import mcp_tool

    yield mcp_tool
    _cleanup(mcp_tool, SERVER)


def test_tool_level_errors_do_not_open_the_breaker(mcp):
    replies = iter([True, True, True, False])

    async def call_tool(*a, **kw):
        is_error = next(replies)
        return _result("bad arguments" if is_error else "ok", is_error=is_error)

    _install_stub_server(mcp, SERVER, call_tool)
    mcp._ensure_mcp_loop()
    handler = mcp._make_tool_handler(SERVER, "tool1", 10.0)

    for _ in range(3):
        assert "bad arguments" in json.loads(handler({}))["error"]
    assert mcp._server_error_counts.get(SERVER, 0) == 0

    assert json.loads(handler({})) == {"result": "ok"}


def test_transport_failures_still_open_the_breaker(mcp):
    calls = {"n": 0}

    async def call_tool(*a, **kw):
        calls["n"] += 1
        raise RuntimeError("connection reset")

    _install_stub_server(mcp, SERVER, call_tool)
    mcp._ensure_mcp_loop()
    handler = mcp._make_tool_handler(SERVER, "tool1", 10.0)

    for _ in range(3):
        assert "connection reset" in json.loads(handler({}))["error"]

    fourth = json.loads(handler({}))
    assert "unreachable" in fourth["error"]
    assert calls["n"] == 3, "an open breaker must not reach the session"


def test_a_tool_error_resets_a_count_left_by_transport_failures(mcp):
    async def call_tool(*a, **kw):
        return _result("bad arguments", is_error=True)

    _install_stub_server(mcp, SERVER, call_tool)
    mcp._ensure_mcp_loop()
    mcp._server_error_counts[SERVER] = mcp._CIRCUIT_BREAKER_THRESHOLD - 1

    handler = mcp._make_tool_handler(SERVER, "tool1", 10.0)
    assert "bad arguments" in json.loads(handler({}))["error"]
    assert mcp._server_error_counts[SERVER] == 0
