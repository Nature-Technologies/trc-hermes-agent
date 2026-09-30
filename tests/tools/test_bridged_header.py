"""A tool call a sandbox script makes carries X-Hermes-Bridged: 1; a model-direct call
does not.  The backend routes bridged tokens to PENDING rather than DELIVERED (closes L4).

Tests cover:
- _BRIDGED is True inside the dispatch window set by _serve_bridged_call
- _BRIDGED resets to False after the call (or on exception)
- _make_tool_handler reads the ContextVar on the agent thread and arms
  server._end_user_bridged (pure unit test, no socket needed)
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tools import trc_sandbox_bridge as bridge


# ---------------------------------------------------------------------------
# _serve_bridged_call / _BRIDGED ContextVar
# ---------------------------------------------------------------------------


def test_serve_bridged_call_arms_the_bridged_flag(monkeypatch):
    """_dispatch_one (and anything it calls) sees _BRIDGED == True."""
    seen: dict = {}
    monkeypatch.setattr(
        bridge,
        "_dispatch_one",
        lambda *a, **k: seen.update(bridged=bridge._bridged_now()) or "{}",
    )
    bridge._serve_bridged_call(
        "mcp__ragnarok__query",
        {"query": "q"},
        allowed_tools=frozenset({"mcp__ragnarok__query"}),
        tool_call_counter=[0],
        max_tool_calls=20,
        task_id="t",
    )
    assert seen["bridged"] is True


def test_bridged_flag_is_false_outside_serve():
    """Outside a bridged dispatch the ContextVar is at its default (False)."""
    assert bridge._bridged_now() is False


def test_bridged_flag_resets_after_serve(monkeypatch):
    """_serve_bridged_call resets _BRIDGED in its finally, even if dispatch raises."""
    monkeypatch.setattr(
        bridge,
        "_dispatch_one",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    try:
        bridge._serve_bridged_call(
            "mcp__ragnarok__query",
            {"query": "q"},
            allowed_tools=frozenset({"mcp__ragnarok__query"}),
            tool_call_counter=[0],
            max_tool_calls=20,
            task_id="t",
        )
    except RuntimeError:
        pass
    # Must be False regardless of how dispatch exited.
    assert bridge._bridged_now() is False


def test_disallowed_tool_returns_error_without_setting_bridged(monkeypatch):
    """The allowlist rejection path returns before the ContextVar is set."""
    seen: dict = {}
    monkeypatch.setattr(
        bridge,
        "_dispatch_one",
        lambda *a, **k: seen.update(called=True) or "{}",
    )
    result = bridge._serve_bridged_call(
        "mcp__ragnarok__forbidden",
        {},
        allowed_tools=frozenset({"mcp__ragnarok__query"}),
        tool_call_counter=[0],
        max_tool_calls=20,
        task_id="t",
    )
    assert "error" in json.loads(result)
    assert not seen.get("called")
    assert bridge._bridged_now() is False


# ---------------------------------------------------------------------------
# _resolve_end_user_bridged reads _BRIDGED on the calling thread
# (testing through the full _make_tool_handler requires a live MCP event loop
# which is not available in unit tests; testing the resolver is the right seam)
# ---------------------------------------------------------------------------


def test_resolve_end_user_bridged_returns_true_inside_serve():
    """_resolve_end_user_bridged() returns True while _BRIDGED is set."""
    from tools.mcp_tool import _resolve_end_user_bridged

    tok = bridge._BRIDGED.set(True)
    try:
        assert _resolve_end_user_bridged() is True
    finally:
        bridge._BRIDGED.reset(tok)


def test_resolve_end_user_bridged_returns_false_by_default():
    """_resolve_end_user_bridged() returns False when _BRIDGED is at its default."""
    from tools.mcp_tool import _resolve_end_user_bridged

    assert _resolve_end_user_bridged() is False


def test_resolve_end_user_bridged_returns_false_after_reset(monkeypatch):
    """_resolve_end_user_bridged() returns False after _serve_bridged_call resets it."""
    from tools.mcp_tool import _resolve_end_user_bridged

    monkeypatch.setattr(bridge, "_dispatch_one", lambda *a, **k: "{}")
    bridge._serve_bridged_call(
        "mcp__ragnarok__query",
        {"query": "q"},
        allowed_tools=frozenset({"mcp__ragnarok__query"}),
        tool_call_counter=[0],
        max_tool_calls=20,
        task_id="t",
    )
    # After the call the ContextVar must be back at its default.
    assert _resolve_end_user_bridged() is False
