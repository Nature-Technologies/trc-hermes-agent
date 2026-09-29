"""MCP tools reachable from a sandbox script as `ragnarok.<tool>(**kwargs)`
(trc-backend spec 2026-09-28 §4.2), and the TRC-shaped description (§4.6)."""

from __future__ import annotations

import json
import types
from unittest.mock import patch

from tools import code_execution_tool as cet

_ALLOW = {
    "sandbox_tools": [
        "mcp__ragnarok__query",
        "mcp__ragnarok__list_entities",
    ]
}


def _module(tools, calls):
    """Exec the generated stub module with `_call` replaced by a recorder."""
    with patch.object(cet, "_load_config", return_value=_ALLOW):
        src = cet.generate_hermes_tools_module(tools)
    ns: dict = {}
    exec(compile(src, "hermes_tools", "exec"), ns)  # noqa: S102 - generated test code

    def fake_call(tool_name, args):
        calls.append((tool_name, args))
        return {"result": json.dumps({"status": "ok", "count": 3})}

    ns["_call"] = fake_call
    return types.SimpleNamespace(**ns)


def test_a_script_calls_an_mcp_tool_with_keyword_arguments():
    calls: list = []
    mod = _module(["mcp__ragnarok__query", "mcp__ragnarok__list_entities"], calls)
    out = mod.ragnarok.list_entities(category="CLIENT", page=2)
    assert calls == [("mcp__ragnarok__list_entities", {"category": "CLIENT", "page": 2})]
    assert out == {"status": "ok", "count": 3}


def test_a_tool_outside_the_session_is_not_an_attribute():
    calls: list = []
    mod = _module(["mcp__ragnarok__query"], calls)
    try:
        mod.ragnarok.lookup_live(entity="x")
    except AttributeError as exc:
        assert "not available" in str(exc)
    else:
        raise AssertionError("lookup_live must not be callable")
    assert calls == []


def test_structured_content_is_unwrapped_to_the_tools_own_dict():
    unwrap = _module(["mcp__ragnarok__query"], [])._unwrap_mcp
    assert unwrap({"result": "text", "structuredContent": {"status": "ok"}}) == {
        "status": "ok"
    }
    assert unwrap({"structuredContent": {"result": {"status": "ok"}}}) == {"status": "ok"}
    assert unwrap({"result": '{"status": "ok"}'}) == {"status": "ok"}
    assert unwrap({"error": "MCP call failed"}) == {
        "status": "error",
        "error": "MCP call failed",
    }


def test_the_description_lists_mcp_stubs_and_honours_the_intro_and_limits():
    cfg = dict(_ALLOW, description_intro="TRC INTRO.", timeout=270, max_tool_calls=20)
    schema_props = {
        "query": {"type": "string"},
        "requesting_user": {"type": "string"},
        "session_id": {"type": "string"},
        "answer": {"type": "boolean"},
    }
    fake_schema = {"description": "Answer a question.\nMore.", "parameters": {"properties": schema_props}}
    with (
        patch.object(cet, "_load_config", return_value=cfg),
        patch("tools.registry.registry.get_schema", return_value=fake_schema),
    ):
        schema = cet.build_execute_code_schema({"mcp__ragnarok__query"}, mode="strict")
    text = schema["description"]
    assert text.startswith("TRC INTRO.")
    assert "ragnarok.query(query, answer)" in text  # identity args hidden
    assert "Answer a question." in text
    assert "270-second timeout" in text and "max 20 tool calls" in text
    # No built-in tool's doc line (the helper line may still mention terminal()).
    assert "terminal(command" not in text and "web_search(query" not in text
    assert "from hermes_tools import ragnarok" in schema["parameters"]["properties"]["code"]["description"]


# ---------------------------------------------------------------------------
# Fix round 1: digit-leading and keyword server names → valid identifiers
# ---------------------------------------------------------------------------

_ALLOW_1PW = {
    "sandbox_tools": ["mcp__1password__lookup"],
}


def _module_cfg(tools, calls, cfg):
    """Like _module but with an explicit config dict."""
    with patch.object(cet, "_load_config", return_value=cfg):
        src = cet.generate_hermes_tools_module(tools)
    ns: dict = {}
    exec(compile(src, "hermes_tools", "exec"), ns)  # noqa: S102 - generated test code

    def fake_call(tool_name, args):
        calls.append((tool_name, args))
        return {"result": json.dumps({"status": "ok"})}

    ns["_call"] = fake_call
    return types.SimpleNamespace(**ns)


def test_digit_leading_server_module_compiles_and_dispatches():
    """mcp__1password__lookup → variable `mcp_1password`; dispatch uses real name."""
    calls: list = []
    mod = _module_cfg(["mcp__1password__lookup"], calls, _ALLOW_1PW)
    # The module must compile (no SyntaxError) and expose mcp_1password
    out = mod.mcp_1password.lookup(x=1)
    assert calls == [("mcp__1password__lookup", {"x": 1})]
    assert out == {"status": "ok"}


def test_keyword_server_module_compiles():
    """A server named 'class' (a Python keyword) → variable `mcp_class`."""
    cfg = {"sandbox_tools": ["mcp__class__search"]}
    calls: list = []
    mod = _module_cfg(["mcp__class__search"], calls, cfg)
    mod.mcp_class.search(q="test")
    assert calls == [("mcp__class__search", {"q": "test"})]


def test_digit_leading_server_description_uses_identifier():
    """Description line and import example use `mcp_1password`, not `1password`."""
    cfg = dict(_ALLOW_1PW, description_intro="")
    fake_schema = {"description": "Look up a secret.", "parameters": {"properties": {"key": {"type": "string"}}}}
    with (
        patch.object(cet, "_load_config", return_value=cfg),
        patch("tools.registry.registry.get_schema", return_value=fake_schema),
    ):
        schema = cet.build_execute_code_schema({"mcp__1password__lookup"}, mode="strict")
    text = schema["description"]
    assert "mcp_1password.lookup(" in text
    # The bare digit-leading form must not appear as the identifier (though it
    # appears as a substring of "mcp_1password").  Check for the indented doc line.
    assert "  1password.lookup(" not in text
    code_desc = schema["parameters"]["properties"]["code"]["description"]
    assert "mcp_1password" in code_desc
