"""After each run the gateway reports its stdout for the computed-figure tier
(trc-backend spec §4.4). A failure must never break the run's result."""

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
