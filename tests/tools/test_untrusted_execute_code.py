"""A script can print document text; its output must reach the model inside the same
injection boundary MCP results get (trc-backend spec §4.4)."""

from agent.tool_dispatch_helpers import make_tool_result_message


def test_execute_code_output_is_wrapped_as_untrusted():
    message = make_tool_result_message("execute_code", "x" * 64, "call-1")
    assert message["content"].startswith('<untrusted_tool_result source="execute_code">')
