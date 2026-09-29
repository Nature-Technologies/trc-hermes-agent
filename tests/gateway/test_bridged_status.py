"""Bridged calls drive the interim status line (trc-backend spec §4.3)."""

import inspect

from gateway.platforms import api_server
from gateway.platforms.turn_status import STATUS_TEXT_RE, TurnStatus


def test_execute_code_shows_calculating():
    status = TurnStatus(clock=lambda: 0.0)
    payloads = status.on_tool_start("execute_code")
    assert payloads[-1]["description"] == "Calculating…"
    assert STATUS_TEXT_RE.match("Calculating…")


def test_a_bridged_data_tool_marks_the_turn_as_searched():
    status = TurnStatus(clock=lambda: 0.0)
    status.on_tool_start("execute_code")
    status.on_tool_start("mcp__ragnarok__query")
    status.on_tool_complete("mcp__ragnarok__query")
    assert status.on_end()[-1]["description"] == "Searched TRC records"


def test_the_request_binds_its_callbacks_for_bridged_calls():
    src = inspect.getsource(api_server.APIServerAdapter._run_agent)
    assert "set_progress_callbacks(tool_start_callback, tool_complete_callback)" in src
    assert "reset_progress_callbacks(" in src
    assert "discard_turn_cache()" in src
    assert src.index("discard_turn_cache()") < src.index("reset_end_user_identity(")
