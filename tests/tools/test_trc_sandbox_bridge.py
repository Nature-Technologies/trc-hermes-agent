"""Rules every bridged (script-issued) tool call follows (trc-backend spec §4.3)."""

from __future__ import annotations

import json
from unittest.mock import patch

from tools import code_execution_tool as cet
from tools import trc_sandbox_bridge as bridge

Q = "mcp__ragnarok__query"


def test_identity_arguments_are_stripped_from_bridged_calls():
    """Review Focus 1."""
    args = {"query": "q", "requesting_user": "someone", "session_id": "other:chat"}
    assert bridge.prepare_bridged_args(Q, args) == {"query": "q"}


def test_non_mcp_tools_are_left_alone():
    assert bridge.prepare_bridged_args("terminal", {"session_id": "x"}) == {"session_id": "x"}


def test_answer_is_stripped_only_from_model_direct_calls():
    assert bridge.strip_script_only_args(Q, {"query": "q", "answer": False}) == {"query": "q"}
    assert bridge.prepare_bridged_args(Q, {"query": "q", "answer": False}) == {
        "query": "q",
        "answer": False,
    }


def test_the_turn_cache_reuses_a_result_within_one_turn():
    calls = []

    def call():
        calls.append(1)
        return json.dumps({"status": "ok"})

    with (
        patch("gateway.session_context.get_end_user_chat_id", return_value="chat-1"),
        patch("gateway.session_context.get_end_user_request_id", return_value="msg-1"),
    ):
        bridge.cached_bridged_call(Q, {"query": "q"}, call)
        bridge.cached_bridged_call(Q, {"query": "q"}, call)
    assert len(calls) == 1


def test_a_new_turn_refetches():
    calls = []

    def call():
        calls.append(1)
        return json.dumps({"status": "ok"})

    for msg in ("msg-a", "msg-b"):
        with (
            patch("gateway.session_context.get_end_user_chat_id", return_value="chat-1"),
            patch("gateway.session_context.get_end_user_request_id", return_value=msg),
        ):
            bridge.cached_bridged_call(Q, {"query": "same"}, call)
    assert len(calls) == 2


def test_errors_are_not_cached():
    calls = []

    def call():
        calls.append(1)
        return json.dumps({"error": "MCP call failed"})

    with (
        patch("gateway.session_context.get_end_user_chat_id", return_value="chat-1"),
        patch("gateway.session_context.get_end_user_request_id", return_value="msg-e"),
    ):
        bridge.cached_bridged_call(Q, {"query": "q"}, call)
        bridge.cached_bridged_call(Q, {"query": "q"}, call)
    assert len(calls) == 2


def test_progress_callbacks_fire_with_one_id_per_call():
    seen = []
    token = bridge.set_progress_callbacks(
        lambda cid, name, args: seen.append(("start", cid, name)),
        lambda cid, name, args, result: seen.append(("done", cid, name)),
    )
    try:
        cid = bridge.notify_bridged_start(Q, {"query": "q"})
        bridge.notify_bridged_complete(cid, Q, {"query": "q"}, "{}")
    finally:
        bridge.reset_progress_callbacks(token)
    assert [s[0] for s in seen] == ["start", "done"]
    assert seen[0][1] == seen[1][1] and seen[0][1].startswith("bridged-")


def _serve(tool, args, allowed=frozenset({Q}), counter=None, max_calls=20):
    counter = counter if counter is not None else [0]
    with patch("model_tools.handle_function_call", return_value=json.dumps({"ok": 1})) as h:
        out = cet._serve_bridged_call(
            tool,
            args,
            allowed_tools=allowed,
            tool_call_counter=counter,
            max_tool_calls=max_calls,
            task_id="t",
        )
    return json.loads(out), h, counter


def test_a_tool_outside_the_allowlist_is_refused_without_dispatch():
    """Review Focus 2."""
    for tool in ("mcp__ragnarok__lookup_live", "mcp__ragnarok__record_computed", "terminal"):
        out, handler, counter = _serve(tool, {})
        assert "not available" in out["error"]
        handler.assert_not_called()
        assert counter == [0]


def test_the_call_cap_is_enforced():
    out, handler, _ = _serve(Q, {"query": "q"}, counter=[20], max_calls=20)
    assert "limit" in out["error"].lower()
    handler.assert_not_called()


def test_a_bridged_call_is_dispatched_without_identity_args():
    _, handler, counter = _serve(Q, {"query": "q", "session_id": "evil"})
    assert handler.call_args.args[1] == {"query": "q"}
    assert counter == [1]


def test_model_direct_query_loses_answer_in_the_agent_hook():
    from types import SimpleNamespace

    from agent.tool_executor import _apply_tool_request_middleware_for_agent

    with patch(
        "hermes_cli.middleware.apply_tool_request_middleware",
        side_effect=lambda name, args, **kw: SimpleNamespace(payload=args, trace=[]),
    ):
        payload, _ = _apply_tool_request_middleware_for_agent(
            SimpleNamespace(session_id="s"),
            function_name=Q,
            function_args={"query": "q", "answer": False},
            effective_task_id="t",
            tool_call_id="call-1",
        )
    assert payload == {"query": "q"}
