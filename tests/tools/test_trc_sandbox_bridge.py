"""Rules every bridged (script-issued) tool call follows (trc-backend spec §4.3)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tools import code_execution_tool as cet
from tools import trc_sandbox_bridge as bridge

Q = "mcp__ragnarok__query"


@pytest.fixture(autouse=True)
def _clear_cache():
    """Isolate each test: clear the process-global LRU before it runs."""
    bridge._cache.clear()
    yield
    bridge._cache.clear()


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
        patch("gateway.session_context.get_end_user_identity", return_value="tok-a"),
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
            patch("gateway.session_context.get_end_user_identity", return_value="tok-a"),
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
        patch("gateway.session_context.get_end_user_identity", return_value="tok-a"),
        patch("gateway.session_context.get_end_user_chat_id", return_value="chat-1"),
        patch("gateway.session_context.get_end_user_request_id", return_value="msg-e"),
    ):
        bridge.cached_bridged_call(Q, {"query": "q"}, call)
        bridge.cached_bridged_call(Q, {"query": "q"}, call)
    assert len(calls) == 2


def test_different_identity_same_turn_ids_refetches():
    """I1: same chat+message ids but a different identity must NOT share a cached result."""
    calls = []

    def call():
        calls.append(1)
        return json.dumps({"status": "ok"})

    for identity in ("user-alice-jwt", "user-bob-jwt"):
        with (
            patch("gateway.session_context.get_end_user_identity", return_value=identity),
            patch("gateway.session_context.get_end_user_chat_id", return_value="chat-1"),
            patch("gateway.session_context.get_end_user_request_id", return_value="msg-1"),
        ):
            bridge.cached_bridged_call(Q, {"query": "q"}, call)
    assert len(calls) == 2


def test_missing_identity_is_not_cached():
    """I1: when identity is missing the turn key is None → no caching, always dispatch."""
    calls = []

    def call():
        calls.append(1)
        return json.dumps({"status": "ok"})

    with (
        patch("gateway.session_context.get_end_user_identity", return_value=None),
        patch("gateway.session_context.get_end_user_chat_id", return_value="chat-1"),
        patch("gateway.session_context.get_end_user_request_id", return_value="msg-1"),
    ):
        bridge.cached_bridged_call(Q, {"query": "q"}, call)
        bridge.cached_bridged_call(Q, {"query": "q"}, call)
    assert len(calls) == 2


def test_discard_turn_cache_drops_current_turn_and_leaves_others():
    """I2: discard_turn_cache removes the current turn's entries; other turns survive."""
    # Populate two turns.
    for identity, msg, result_val in [
        ("id-a", "msg-1", "ok-a"),
        ("id-b", "msg-2", "ok-b"),
    ]:
        with (
            patch("gateway.session_context.get_end_user_identity", return_value=identity),
            patch("gateway.session_context.get_end_user_chat_id", return_value="chat-1"),
            patch("gateway.session_context.get_end_user_request_id", return_value=msg),
        ):
            bridge.cached_bridged_call(Q, {"query": "q"}, lambda v=result_val: json.dumps({"r": v}))

    assert len(bridge._cache) == 2

    # Discard from id-a's turn.
    with (
        patch("gateway.session_context.get_end_user_identity", return_value="id-a"),
        patch("gateway.session_context.get_end_user_chat_id", return_value="chat-1"),
        patch("gateway.session_context.get_end_user_request_id", return_value="msg-1"),
    ):
        dropped = bridge.discard_turn_cache()

    assert dropped == 1
    assert len(bridge._cache) == 1  # id-b's entry survives


def test_discard_turn_cache_no_op_without_turn_key():
    """discard_turn_cache returns 0 and does nothing when there is no current turn."""
    with (
        patch("gateway.session_context.get_end_user_identity", return_value=None),
        patch("gateway.session_context.get_end_user_chat_id", return_value="chat-1"),
        patch("gateway.session_context.get_end_user_request_id", return_value="msg-1"),
    ):
        assert bridge.discard_turn_cache() == 0


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
