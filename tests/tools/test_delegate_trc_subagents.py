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


# --- C2: foreground on the API server ----------------------------------------------------

_CREDS = {
    "model": "m", "provider": None, "base_url": None, "api_key": None,
    "api_mode": None, "command": None, "args": None,
}


def _bind_api_server_request(sid="raw-sid-7"):
    """What ApiServerAdapter._bind_api_server_session binds for a continuable turn,
    which is exactly the case that upstream dispatches in the background and wakes."""
    from gateway.session_context import set_session_vars

    set_session_vars(
        platform="api_server", chat_id=sid, session_key=sid, session_id=sid,
        async_delivery=False,
    )


def _no_background_dispatch(*args, **kwargs):
    raise AssertionError("delegate_task dispatched in the background on the API server")


def test_api_server_delegation_returns_results_in_the_same_request(monkeypatch):
    import run_agent
    import tools.async_delegation as ad

    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"
    fake_child.tool_progress_callback = None

    def _fast_child(task_index, goal, child=None, parent_agent=None, **kw):
        return {
            "task_index": task_index, "status": "completed",
            "summary": f"findings: {goal}", "api_calls": 1,
            "duration_seconds": 0.1, "model": "m", "exit_reason": "completed",
        }

    monkeypatch.setattr(dt, "_load_config", lambda: {"background": False})
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_run_single_child", _fast_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: _CREDS)
    monkeypatch.setattr(ad, "dispatch_async_delegation_batch", _no_background_dispatch)
    _bind_api_server_request()

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "raw-sid-7"
    parent._active_children = []
    parent._active_children_lock = threading.Lock()

    # The live model path, which asks for background=True on every top-level call.
    single = json.loads(run_agent.AIAgent._dispatch_delegate_task(parent, {"goal": "brief on X"}))
    batch = json.loads(run_agent.AIAgent._dispatch_delegate_task(
        parent, {"tasks": [{"goal": "part 1"}, {"goal": "part 2"}]}
    ))

    assert single.get("status") != "dispatched"
    assert single["results"][0]["summary"] == "findings: brief on X"
    assert [r["summary"] for r in batch["results"]] == ["findings: part 1", "findings: part 2"]
    # No completion event was queued, so gateway/wake.py has nothing to self-POST.
    assert process_registry.completion_queue.empty()


def _bare_agent():
    """AIAgent with only the interrupt fields (tests/run_agent/test_interrupt_propagation.py)."""
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent._interrupt_requested = False
    agent._interrupt_message = None
    agent._execution_thread_id = None
    agent._interrupt_thread_signal_pending = False
    agent._active_children = []
    agent._active_children_lock = threading.Lock()
    agent.quiet_mode = True
    agent.provider = "openrouter"
    agent.model = "test/model"
    agent._base_url = "http://localhost:1234"
    return agent


def test_stop_on_the_parent_interrupts_a_running_foreground_child(monkeypatch):
    parent = _bare_agent()
    parent._delegate_depth = 0
    parent.session_id = "raw-sid-7"
    child = _bare_agent()
    started = threading.Event()

    def _build(**kw):
        # The real _build_child_agent registers the child for interrupt fan-out.
        with parent._active_children_lock:
            parent._active_children.append(child)
        return child

    def _run_until_interrupted(task_index, goal, child=None, parent_agent=None, **kw):
        started.set()
        deadline = time.monotonic() + 5
        while not child._interrupt_requested and time.monotonic() < deadline:
            time.sleep(0.01)
        status = "interrupted" if child._interrupt_requested else "completed"
        return {
            "task_index": task_index, "status": status, "summary": None,
            "api_calls": 0, "duration_seconds": 0.0, "exit_reason": status,
        }

    monkeypatch.setattr(dt, "_load_config", lambda: {"background": False})
    monkeypatch.setattr(dt, "_build_child_agent", _build)
    monkeypatch.setattr(dt, "_run_single_child", _run_until_interrupted)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: _CREDS)

    out = {}

    def _request():
        _bind_api_server_request()
        out["raw"] = dt.delegate_task(goal="long report", background=True, parent_agent=parent)

    t = threading.Thread(target=_request)
    t.start()
    assert started.wait(5)
    parent.interrupt("Stop")  # what the SSE writer does on client disconnect / Stop
    t.join(timeout=10)

    result = json.loads(out["raw"])
    assert result["results"][0]["status"] == "interrupted"
    assert child._interrupt_requested is True


def test_foreground_mode_tells_the_model_the_results_arrive_in_this_call(monkeypatch):
    monkeypatch.setattr(dt, "_load_config", lambda: {"background": False})
    desc = dt._build_top_level_description()
    assert "RUN IN THE BACKGROUND" not in desc
    assert "this same tool call" in desc
    monkeypatch.setattr(dt, "_load_config", lambda: {})
    assert "RUN IN THE BACKGROUND" in dt._build_top_level_description()


# --- C3: TRC rules in children -----------------------------------------------------------

from pathlib import Path as _Path

_TRC_SOUL = _Path(__file__).resolve().parents[2] / "deploy" / "trc" / "SOUL.md"


def test_a_configured_result_contract_replaces_the_generic_summary_instruction():
    prompt = dt._build_child_system_prompt(
        "Brief on <ORGANIZATION_3>",
        result_instructions="Return findings with every [Sn] marker.",
    )
    assert "YOUR TASK:\nBrief on <ORGANIZATION_3>" in prompt
    assert "Return findings with every [Sn] marker." in prompt
    assert "Any files you created or modified" not in prompt
    assert "/workspace" not in prompt


def test_without_a_contract_the_generic_instruction_is_unchanged():
    prompt = dt._build_child_system_prompt("Fix the tests")
    assert "Any files you created or modified" in prompt


def _build_one_child(parent=None):
    with patch("run_agent.AIAgent") as MockAgent:
        MockAgent.return_value = MagicMock()
        dt._build_child_agent(
            task_index=0, goal="g", context=None, toolsets=None, model=None,
            max_iterations=6, task_count=1, parent_agent=parent or _make_mock_parent(),
        )
    return MockAgent.call_args.kwargs


def test_children_are_built_with_soul_and_the_configured_contract(monkeypatch):
    monkeypatch.setattr(
        dt, "_load_config", lambda: {"result_instructions": "CONTRACT: copy tokens verbatim."}
    )
    kwargs = _build_one_child()
    assert kwargs["load_soul_identity"] is True
    assert kwargs["skip_context_files"] is True  # cwd project files stay out
    assert "CONTRACT: copy tokens verbatim." in kwargs["ephemeral_system_prompt"]


def test_a_child_system_prompt_carries_the_trc_soul_rules(tmp_path, monkeypatch):
    """The real prompt builder, given the flags the child is built with, picks up the TRC
    SOUL.md and not Hermes' stock identity."""
    import shutil
    from types import SimpleNamespace

    from agent.prompt_builder import DEFAULT_AGENT_IDENTITY
    from agent.system_prompt import build_system_prompt_parts

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    shutil.copy(_TRC_SOUL, home / "SOUL.md")
    monkeypatch.setattr(dt, "_load_config", lambda: {})

    kwargs = _build_one_child()
    child_like = SimpleNamespace(
        load_soul_identity=kwargs["load_soul_identity"],
        skip_context_files=kwargs["skip_context_files"],
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform=kwargs["platform"],
        pass_session_id=False,
        session_id="",
    )
    with (
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        stable = build_system_prompt_parts(child_like)["stable"]
    assert stable.startswith("RULE ZERO")
    assert DEFAULT_AGENT_IDENTITY not in stable


# --- C4: token/marker-safe truncation ----------------------------------------------------

_SEP = "\n\n[... middle omitted — see footer ...]\n\n"
_UNIT = (
    "Revenue at <ORGANIZATION_12> rose to <MONEY_4> [S3]; holders "
    "[[list:lst_" + "A" * 22 + "]] and <PERSON_105> [S27]. "
)


def _head_and_tail(model_text):
    head, rest = model_text.split(_SEP, 1)
    return head, rest.split("\n" + "─" * 8, 1)[0]


def _has_partial_span(part):
    """Any '<', '>', '[' or ']' left after removing every whole token/marker is a fragment."""
    rest = dt._PROTECTED_SPAN_RE.sub("", part)
    return any(ch in rest for ch in "<>[]")


def test_truncation_never_splits_a_token_or_marker():
    text = _UNIT * 300  # no newlines: the line snap cannot rescue a bad cut
    for cap in range(2000, 2400, 7):
        model_text, _ = dt._trim_summary_with_footer(text, cap, 0, spill=False)
        head, tail = _head_and_tail(model_text)
        assert text.startswith(head) and text.endswith(tail), cap
        assert not _has_partial_span(head), (cap, head[-40:])
        assert not _has_partial_span(tail), (cap, tail[:40])


def test_truncation_without_spill_names_no_path_and_no_read_file(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    model_text, path = dt._trim_summary_with_footer(_UNIT * 300, 2000, 0, spill=False)
    assert path is None
    assert "[SUMMARY TRUNCATED]" in model_text
    for leak in ("read_file", "saved to", "disk", str(home)):
        assert leak not in model_text, leak
    assert not (home / "cache" / "delegation").exists()


def test_summary_budget_honours_spill_summaries_false(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        dt, "_load_config", lambda: {"max_summary_chars": 2000, "spill_summaries": False}
    )

    class _Bare:
        pass

    results = [{"task_index": 0, "summary": _UNIT * 300, "status": "completed"}]
    dt._apply_summary_budget(results, _Bare())
    entry = results[0]
    assert entry["summary_truncated"] is True
    assert "summary_full_path" not in entry
    assert "read_file" not in entry["summary"]
    head, tail = _head_and_tail(entry["summary"])
    assert not _has_partial_span(head) and not _has_partial_span(tail)
    assert not (home / "cache" / "delegation").exists()


def test_a_result_with_tokens_and_markers_reaches_the_parent_byte_for_byte():
    findings = (
        "<ORGANIZATION_3> NAV was $4.2m at 30 June 2026 [S1]; <PERSON_12> holds 40% [S2]. "
        "Holders: [[list:lst_" + "B" * 22 + "]]"
    )

    def _factory(*args, **kwargs):
        child = MagicMock()
        child.model = "m"
        child.session_prompt_tokens = 0
        child.session_completion_tokens = 0
        child.session_estimated_cost_usd = 0.0
        child._credential_pool = None
        child.run_conversation.return_value = {
            "final_response": findings, "completed": True, "interrupted": False,
            "api_calls": 1, "messages": [],
        }
        return child

    with patch("run_agent.AIAgent", side_effect=_factory):
        out = json.loads(dt.delegate_task(goal="brief", parent_agent=_make_mock_parent()))
    assert out["results"][0]["summary"] == findings


@pytest.mark.parametrize("offset", range(1, len("<ORGANIZATION_12>")))
def test_protected_safe_cut_inside_a_token_moves_to_its_edges(offset):
    text = "abc <ORGANIZATION_12> xyz"
    start = text.index("<")
    end = start + len("<ORGANIZATION_12>")
    pos = start + offset
    assert dt._protected_safe_cut(text, pos, toward_end=False) == start
    assert dt._protected_safe_cut(text, pos, toward_end=True) == end
    # A cut exactly on a boundary is kept.
    assert dt._protected_safe_cut(text, start, toward_end=False) == start
    assert dt._protected_safe_cut(text, end, toward_end=True) == end


def test_protected_safe_cut_between_adjacent_tokens_is_kept():
    text = "<PERSON_1><MONEY_2>[S3][[list:lst_" + "C" * 22 + "]]"
    for boundary in (0, 10, 19, 23, len(text)):
        assert dt._protected_safe_cut(text, boundary, toward_end=False) == boundary
        assert dt._protected_safe_cut(text, boundary, toward_end=True) == boundary
    # Inside the second token of an adjacent pair: snaps to that token, not the first.
    assert dt._protected_safe_cut(text, 13, toward_end=False) == 10
    assert dt._protected_safe_cut(text, 13, toward_end=True) == 19


def test_truncation_of_a_summary_that_is_nearly_all_tokens():
    text = ("<PERSON_105><ORGANIZATION_12>[S27][[list:lst_" + "D" * 22 + "]]") * 200 + "end"
    for cap in range(2000, 2100, 3):
        model_text, _ = dt._trim_summary_with_footer(text, cap, 0, spill=False)
        head, tail = _head_and_tail(model_text)
        assert text.startswith(head) and text.endswith(tail), cap
        assert not _has_partial_span(head), (cap, head[-40:])
        assert not _has_partial_span(tail), (cap, tail[:40])


def test_spill_summaries_defaults_on():
    assert dt._spill_summaries_enabled({}) is True
    assert dt._spill_summaries_enabled({"spill_summaries": False}) is False
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["delegation"]["spill_summaries"] is True


# --- C5: nothing on disk outside the session DB ------------------------------------------


def _delegation_files(home):
    root = home / "cache"
    return [p for p in root.rglob("*") if "delegation" in p.parts] if root.exists() else []


def test_with_both_switches_off_no_delegation_files_are_written(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        "delegation:\n  live_log: false\n  spill_summaries: false\n  max_summary_chars: 2000\n",
        encoding="utf-8",
    )

    def _factory(*args, **kwargs):
        child = MagicMock()
        child.model = "m"
        child.session_prompt_tokens = 0
        child.session_completion_tokens = 0
        child.session_estimated_cost_usd = 0.0
        child._credential_pool = None
        child.run_conversation.return_value = {
            "final_response": _UNIT * 300, "completed": True, "interrupted": False,
            "api_calls": 1, "messages": [],
        }
        return child

    with patch("run_agent.AIAgent", side_effect=_factory):
        out = json.loads(dt.delegate_task(
            tasks=[{"goal": "a"}, {"goal": "b"}], parent_agent=_make_mock_parent()
        ))

    assert [r["summary_truncated"] for r in out["results"]] == [True, True]
    assert "live_transcripts" not in out
    for r in out["results"]:
        assert "live_transcript" not in r and "summary_full_path" not in r
    assert _delegation_files(home) == []


class _HangingChild:
    """The minimal subagent stand-in from test_delegate_subagent_timeout_diagnostic.py."""

    def __init__(self):
        self._subagent_id = "sa-0-trc0001"
        self._delegate_depth = 1
        self._delegate_role = "leaf"
        self.model = "test/model"
        self.provider = "testprov"
        self.api_mode = "chat_completions"
        self.base_url = "https://example.test/v1"
        self.max_iterations = 6
        self.quiet_mode = True
        self.skip_memory = True
        self.skip_context_files = True
        self.platform = "subagent"
        self.ephemeral_system_prompt = "sys prompt"
        self.enabled_toolsets = ["ragnarok"]
        self.valid_tool_names = {"mcp__ragnarok__query"}
        self.tools = []
        self._hang = threading.Event()

    def get_activity_summary(self):
        return {"api_call_count": 0, "max_iterations": 6, "current_tool": None,
                "seconds_since_activity": 60}

    def run_conversation(self, user_message, task_id=None, stream_callback=None):
        self._hang.wait(10.0)
        return {"final_response": "", "completed": False, "api_calls": 0}

    def interrupt(self, *args):
        self._hang.set()


def test_live_log_off_also_skips_the_timeout_diagnostic(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(dt, "_load_config", lambda: {"live_log": False})
    monkeypatch.setattr(dt, "_get_child_timeout", lambda: 0.3)
    parent = MagicMock()
    parent._touch_activity = MagicMock()
    parent._current_task_id = None

    result = dt._run_single_child(
        task_index=0, goal="brief on <PERSON_1>", child=_HangingChild(), parent_agent=parent
    )

    assert result["status"] == "timeout"
    assert result.get("diagnostic_path") is None
    logs = home / "logs"
    assert not (logs.is_dir() and list(logs.glob("subagent-timeout-*.log")))


def test_live_log_off_drops_the_transcripts_paragraph(monkeypatch):
    monkeypatch.setattr(dt, "_load_config", lambda: {"live_log": False})
    assert "LIVE TRANSCRIPTS" not in dt._build_top_level_description()
    monkeypatch.setattr(dt, "_load_config", lambda: {})
    assert "LIVE TRANSCRIPTS" in dt._build_top_level_description()


def test_child_sessions_go_to_the_parents_session_db():
    parent = _make_mock_parent()
    parent._session_db = MagicMock(name="session_db")
    kwargs = _build_one_child(parent)
    assert kwargs["session_db"] is parent._session_db
    assert kwargs["parent_session_id"] == "parent-sess"


# --- C5 (controller ruling): no background wording when delegation.background=false -----

_BG_PARAM_DEFAULT = (
    "DEPRECATED / IGNORED. Top-level single and batch "
    "delegations run in the background automatically — you do "
    "not need to (and cannot) opt in or out. A single result or "
    "consolidated batch result re-enters the conversation when "
    "the work finishes; just continue working in the meantime. "
    "Setting this has no effect; the parameter remains only for "
    "backward compatibility."
)
_WHEN_NOT_DURABLE_DEFAULT = (
    "- Durable long-running work that must outlive the current turn -> "
    "use cronjob (action='create') or terminal(background=True, "
    "notify_on_complete=True) instead. Background delegations are NOT "
    "durable: if the parent session is closed (/new) or the process exits "
    "before a subagent finishes, that subagent's work is discarded, and "
    "/stop cancels every running background subagent.\n\n"
)


def test_background_off_schema_has_no_background_or_re_enter_wording(monkeypatch):
    monkeypatch.setattr(dt, "_load_config", lambda: {"background": False})
    overrides = dt._build_dynamic_schema_overrides()
    bg_param = overrides["parameters"]["properties"]["background"]["description"]
    text = overrides["description"] + "\n" + bg_param
    for phrase in (
        "IN THE BACKGROUND", "in the background", "re-enter", "Background delegations",
        "background subagent", "continue working in the meantime",
    ):
        assert phrase not in text, phrase
    assert "this same tool call" in bg_param
    assert "Stop cancels every running subagent" in overrides["description"]
    # The static schema is never mutated by the per-call override.
    assert dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]["background"][
        "description"
    ] == _BG_PARAM_DEFAULT


def test_background_on_by_default_keeps_the_upstream_text(monkeypatch):
    monkeypatch.setattr(dt, "_load_config", lambda: {})
    overrides = dt._build_dynamic_schema_overrides()
    assert overrides["parameters"]["properties"]["background"]["description"] == _BG_PARAM_DEFAULT
    assert _WHEN_NOT_DURABLE_DEFAULT in overrides["description"]
    assert dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]["background"][
        "description"
    ] == _BG_PARAM_DEFAULT


def test_live_log_defaults_on():
    assert dt._live_log_enabled({}) is True
    assert dt._live_log_enabled({"live_log": False}) is False
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["delegation"]["live_log"] is True
