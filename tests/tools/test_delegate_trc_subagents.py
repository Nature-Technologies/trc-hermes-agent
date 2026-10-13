"""Subagents on the TRC API server (trc-orchestrator spec 2026-10-13 §5, Phase C).

delegate_task becomes a TRC tool, so every guarantee the parent's own RAGnarok calls carry must
hold for a child's calls too. The end user's identity, chat id and request id reach the child's
tool calls (C1). The user's request stays open until the children return (C2). Children carry
SOUL.md and a findings contract (C3). Placeholder tokens and [Sn] markers survive the hand-off
(C4). Nothing is written to disk outside the session DB (C5). The Open WebUI status line moves
while children work (C7).
"""

from __future__ import annotations

import json
import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import tools.delegate_tool as dt
from gateway.session_context import (
    get_end_user_chat_id,
    get_end_user_identity,
    get_end_user_request_id,
    reset_end_user_chat_id,
    reset_end_user_identity,
    reset_end_user_request_id,
    set_end_user_chat_id,
    set_end_user_identity,
    set_end_user_request_id,
)
from tools.process_registry import process_registry


def _drain_all():
    while not process_registry.completion_queue.empty():
        try:
            process_registry.completion_queue.get_nowait()
        except Exception:
            break


def _drain_one(timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not process_registry.completion_queue.empty():
            return process_registry.completion_queue.get_nowait()
        time.sleep(0.02)
    return None


@pytest.fixture(autouse=True)
def _clean_queue_and_session_vars(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    _drain_all()
    yield
    import gateway.session_context as sc

    for var in sc._VAR_MAP.values():
        var.set(sc._UNSET)
    sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)
    os.environ.pop("HERMES_SESSION_ID", None)
    _drain_all()


def _make_mock_parent(depth=0):
    """Same shape as tests/tools/test_delegate.py::_make_mock_parent."""
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "***"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent.session_id = "parent-sess"
    return parent


def _recording_child_factory(seen, lock):
    """An AIAgent stand-in whose run_conversation records what its OWN thread sees."""

    def _factory(*args, **kwargs):
        child = MagicMock()
        child.model = "m"
        child.session_prompt_tokens = 0
        child.session_completion_tokens = 0
        child.session_estimated_cost_usd = 0.0
        child._credential_pool = None

        def _run(user_message, task_id=None, stream_callback=None):
            from tools.terminal_tool import _get_approval_callback

            with lock:
                seen[user_message] = {
                    "identity": get_end_user_identity(),
                    "chat_id": get_end_user_chat_id(),
                    "request_id": get_end_user_request_id(),
                    "approval_cb": _get_approval_callback(),
                }
            return {
                "final_response": f"findings for {user_message}",
                "completed": True,
                "interrupted": False,
                "api_calls": 1,
                "messages": [],
            }

        child.run_conversation.side_effect = _run
        return child

    return _factory


def _as_user(identity, chat_id, request_id, fn):
    """Run fn with this request's end-user bindings, as api_server's _run() does."""
    t_id = set_end_user_identity(identity)
    t_chat = set_end_user_chat_id(chat_id)
    t_req = set_end_user_request_id(request_id)
    try:
        return fn()
    finally:
        reset_end_user_request_id(t_req)
        reset_end_user_chat_id(t_chat)
        reset_end_user_identity(t_id)


# --- C1: identity reaches the child's tool-calling thread ---------------------------------


def test_children_carry_their_own_users_identity_with_two_users_interleaved():
    """Batch (outer executor) and single task (inner executor), two users at once."""
    seen, lock = {}, threading.Lock()
    barrier = threading.Barrier(2)
    errors = []

    def _user(tag):
        def _go():
            parent = _make_mock_parent()
            barrier.wait(timeout=5)
            dt.delegate_task(
                tasks=[{"goal": f"{tag}-1"}, {"goal": f"{tag}-2"}], parent_agent=parent
            )
            dt.delegate_task(goal=f"{tag}-solo", parent_agent=parent)

        try:
            _as_user(f"jwt-{tag}", f"chat-{tag}", f"req-{tag}", _go)
        except Exception as exc:  # surfaced below, not swallowed
            errors.append(exc)

    with patch("run_agent.AIAgent", side_effect=_recording_child_factory(seen, lock)):
        threads = [threading.Thread(target=_user, args=(tag,)) for tag in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

    assert not errors, errors
    assert set(seen) == {"A-1", "A-2", "A-solo", "B-1", "B-2", "B-solo"}
    for goal, rec in seen.items():
        tag = goal.split("-")[0]
        assert rec["identity"] == f"jwt-{tag}", (goal, rec)
        assert rec["chat_id"] == f"chat-{tag}", (goal, rec)
        assert rec["request_id"] == f"req-{tag}", (goal, rec)


def test_child_worker_keeps_the_subagent_approval_callback():
    """Guard for deviation 1: carrying context must not install the parent's
    interactive CLI approval callback in place of the subagent auto-deny."""
    from tools import terminal_tool

    seen, lock = {}, threading.Lock()

    def _cli_prompt(*args, **kwargs):
        return "once"

    terminal_tool.set_approval_callback(_cli_prompt)
    try:
        with patch("run_agent.AIAgent", side_effect=_recording_child_factory(seen, lock)):
            dt.delegate_task(goal="solo", parent_agent=_make_mock_parent())
            dt.delegate_task(
                tasks=[{"goal": "b-1"}, {"goal": "b-2"}], parent_agent=_make_mock_parent()
            )
    finally:
        terminal_tool.set_approval_callback(None)

    for goal in ("solo", "b-1", "b-2"):
        assert seen[goal]["approval_cb"] is dt._subagent_auto_deny, goal


def test_background_children_keep_the_identity_of_the_request_that_spawned_them():
    seen, lock = {}, threading.Lock()
    with patch("run_agent.AIAgent", side_effect=_recording_child_factory(seen, lock)):
        out = _as_user(
            "jwt-bg",
            "chat-bg",
            "req-bg",
            lambda: dt.delegate_task(
                goal="bg-1", background=True, parent_agent=_make_mock_parent()
            ),
        )
        assert json.loads(out)["status"] == "dispatched"
        assert _drain_one() is not None
    assert seen["bg-1"]["identity"] == "jwt-bg"
    assert seen["bg-1"]["chat_id"] == "chat-bg"
    assert seen["bg-1"]["request_id"] == "req-bg"
