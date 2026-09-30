"""After each run the gateway reports its stdout for the computed-figure tier
(trc-backend spec §4.4). A failure must never break the run's result."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tools import trc_sandbox_bridge as bridge


def test_sends_the_stdout_to_the_configured_server():
    handler = MagicMock(return_value='{"result": "{\\"status\\": \\"ok\\"}"}')
    with (
        patch("tools.mcp_tool._servers", {"ragnarok": SimpleNamespace(tool_timeout=30)}),
        patch("tools.mcp_tool._make_tool_handler", return_value=handler) as make,
    ):
        bridge.record_computed("growth 1,700,000.00", "ragnarok")
    make.assert_called_once_with("ragnarok", "record_computed", 30)
    handler.assert_called_once_with({"texts": ["growth 1,700,000.00"]})


def test_nothing_is_sent_without_a_server_or_output():
    with patch("tools.mcp_tool._make_tool_handler") as make:
        bridge.record_computed("1,000.00", None)
        bridge.record_computed("   ", "ragnarok")
    make.assert_not_called()


def test_a_failure_is_swallowed():
    with (
        patch("tools.mcp_tool._servers", {"ragnarok": SimpleNamespace(tool_timeout=30)}),
        patch("tools.mcp_tool._make_tool_handler", side_effect=RuntimeError("down")),
    ):
        bridge.record_computed("1,000.00", "ragnarok")  # must not raise


def test_an_unconnected_server_is_skipped():
    with (
        patch("tools.mcp_tool._servers", {}),
        patch("tools.mcp_tool._make_tool_handler") as make,
    ):
        bridge.record_computed("1,000.00", "ragnarok")
    make.assert_not_called()


def test_an_error_reply_is_logged_as_a_warning(caplog):
    handler = MagicMock(return_value='{"error": "MCP call failed: boom"}')
    with (
        patch("tools.mcp_tool._servers", {"ragnarok": SimpleNamespace(tool_timeout=30)}),
        patch("tools.mcp_tool._make_tool_handler", return_value=handler),
    ):
        bridge.record_computed("1,000.00", "ragnarok")  # must not raise
    records = [r for r in caplog.records if r.name == "tools.trc_sandbox_bridge"]
    warning_records = [r for r in records if r.levelname == "WARNING"]
    assert len(warning_records) == 1
    assert "record_computed failed" in warning_records[0].message
    for record in records:
        assert "boom" not in record.message


_REFUSAL = {"status": "error", "error": "no verified caller identity (fail-closed)"}


def _warnings_for(reply, caplog, stdout="growth 1,700,000.00"):
    handler = MagicMock(return_value=reply)
    with (
        caplog.at_level("INFO", logger="tools.trc_sandbox_bridge"),
        patch("tools.mcp_tool._servers", {"ragnarok": SimpleNamespace(tool_timeout=30)}),
        patch("tools.mcp_tool._make_tool_handler", return_value=handler),
    ):
        bridge.record_computed(stdout, "ragnarok")
    records = [r for r in caplog.records if r.name == "tools.trc_sandbox_bridge"]
    for record in records:
        assert stdout not in record.getMessage(), "script output must never be logged"
    assert not [r for r in records if r.getMessage().startswith("record_computed: sent")], (
        "a failure logged as sent"
    )
    return [r.getMessage() for r in records if r.levelname == "WARNING"]


def test_a_backend_refusal_in_structured_content_is_a_failure(caplog):
    reply = json.dumps({"result": json.dumps(_REFUSAL), "structuredContent": _REFUSAL})
    (warning,) = _warnings_for(reply, caplog)
    assert "no verified caller identity (fail-closed)" in warning


def test_a_backend_refusal_in_the_result_text_is_a_failure(caplog):
    reply = json.dumps({"result": json.dumps(_REFUSAL)})
    (warning,) = _warnings_for(reply, caplog)
    assert "no verified caller identity (fail-closed)" in warning


def test_a_top_level_error_is_a_failure(caplog):
    (warning,) = _warnings_for(json.dumps({"error": "MCP call failed"}), caplog)
    assert "record_computed failed" in warning


def test_the_backend_reason_is_truncated(caplog):
    long = {"status": "error", "error": "x" * 500}
    (warning,) = _warnings_for(json.dumps({"result": json.dumps(long)}), caplog)
    assert "x" * 200 in warning and "x" * 201 not in warning


def test_a_backend_success_is_logged_as_sent(caplog):
    ok = {"status": "ok", "count": 1, "promoted": 0}
    handler = MagicMock(return_value=json.dumps({"result": json.dumps(ok), "structuredContent": ok}))
    with (
        caplog.at_level("INFO", logger="tools.trc_sandbox_bridge"),
        patch("tools.mcp_tool._servers", {"ragnarok": SimpleNamespace(tool_timeout=30)}),
        patch("tools.mcp_tool._make_tool_handler", return_value=handler),
    ):
        bridge.record_computed("growth 1,700,000.00", "ragnarok")
    messages = [r.getMessage() for r in caplog.records if r.name == "tools.trc_sandbox_bridge"]
    assert any(m.startswith("record_computed: sent") for m in messages)
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


# ---------------------------------------------------------------------------
# Retry behaviour (spec §4.4)
# ---------------------------------------------------------------------------


def test_a_transport_failure_retries_once(caplog):
    """A transport failure (handler raises) triggers exactly one retry, then gives up
    non-fatally.  The well-formed-error path must NOT be triggered."""
    call_count = 0

    def failing_handler(args):
        nonlocal call_count
        call_count += 1
        raise ConnectionError("MCP transport down")

    with (
        caplog.at_level("WARNING", logger="tools.trc_sandbox_bridge"),
        patch("tools.mcp_tool._servers", {"ragnarok": SimpleNamespace(tool_timeout=30)}),
        patch("tools.mcp_tool._make_tool_handler", return_value=failing_handler),
    ):
        bridge.record_computed("1,000.00", "ragnarok")  # must not raise

    assert call_count == 2, "expected initial attempt + exactly one retry"
    warnings = [r.getMessage() for r in caplog.records
                if r.name == "tools.trc_sandbox_bridge" and r.levelname == "WARNING"]
    assert len(warnings) == 2, "one warning per attempt"
    assert all("transport failure" in w for w in warnings)


def test_a_well_formed_error_reply_does_not_retry(caplog):
    """A well-formed error reply from the backend (not an exception) is NOT retried."""
    call_count = 0
    error_reply = json.dumps({"error": "MCP call failed: backend refused"})

    def handler(args):
        nonlocal call_count
        call_count += 1
        return error_reply

    with (
        caplog.at_level("WARNING", logger="tools.trc_sandbox_bridge"),
        patch("tools.mcp_tool._servers", {"ragnarok": SimpleNamespace(tool_timeout=30)}),
        patch("tools.mcp_tool._make_tool_handler", return_value=handler),
    ):
        bridge.record_computed("1,000.00", "ragnarok")

    assert call_count == 1, "a well-formed error must not be retried"
    warnings = [r.getMessage() for r in caplog.records
                if r.name == "tools.trc_sandbox_bridge" and r.levelname == "WARNING"]
    # Exactly one warning for the error reply — no transport-failure warnings.
    assert len(warnings) == 1
    assert "transport failure" not in warnings[0]


def test_transport_failure_then_success(caplog):
    """If the first attempt fails but the retry succeeds, the call is logged as sent."""
    attempts = []
    ok = json.dumps({"status": "ok", "count": 1, "promoted": 0})

    def flaky_handler(args):
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("first attempt down")
        return json.dumps({"result": ok, "structuredContent": {"status": "ok"}})

    with (
        caplog.at_level("INFO", logger="tools.trc_sandbox_bridge"),
        patch("tools.mcp_tool._servers", {"ragnarok": SimpleNamespace(tool_timeout=30)}),
        patch("tools.mcp_tool._make_tool_handler", return_value=flaky_handler),
    ):
        bridge.record_computed("1,000.00", "ragnarok")

    assert len(attempts) == 2
    messages = [r.getMessage() for r in caplog.records if r.name == "tools.trc_sandbox_bridge"]
    assert any(m.startswith("record_computed: sent") for m in messages)
