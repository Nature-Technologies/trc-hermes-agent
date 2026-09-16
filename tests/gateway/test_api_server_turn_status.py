"""`_start_turn_status`: when the status line applies, what it puts on the queue, and
how the hints side-call is bounded and fed in.

Spec: trc-backend docs/superpowers/specs/2026-09-16-interim-status-while-answering-design.md S4-5.
"""

from __future__ import annotations

import asyncio
import queue
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.platforms import api_server as srv
from gateway.platforms.turn_status import READING_LINE, TurnStatus
from gateway.session_context import clear_session_vars, set_end_user_chat_id

CHAT = "0f7c1a2e-9b3d-4c5f-8a1b-2d3e4f5a6b7c"


def _adapter():
    from gateway.config import PlatformConfig

    return srv.APIServerAdapter(PlatformConfig(enabled=True, token="test-key"))


@pytest.fixture(autouse=True)
def _clean_context():
    clear_session_vars([])
    yield
    clear_session_vars([])


def _drain(q):
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            return items


async def _settle():
    # Let the hints task run to completion.
    if srv._STATUS_HINT_TASKS:
        await asyncio.gather(*list(srv._STATUS_HINT_TASKS), return_exceptions=True)


# --- helpers ----------------------------------------------------------------------


def test_question_text_takes_a_string_or_the_text_parts_of_a_list():
    assert srv._question_text("who is the client") == "who is the client"
    parts = [
        {"type": "text", "text": "first"},
        {"type": "image_url", "image_url": {"url": "data:..."}},
        {"type": "text", "text": "second"},
    ]
    assert srv._question_text(parts) == "first\nsecond"
    assert srv._question_text(None) == ""
    assert srv._question_text(42) == ""


def test_question_text_is_truncated_to_two_thousand_characters():
    assert len(srv._question_text("x" * 5000)) == 2000


def test_status_hints_enabled_reads_the_flag_with_a_true_default():
    assert srv._status_hints_enabled({}) is True
    assert srv._status_hints_enabled({"enabled": True}) is True
    assert srv._status_hints_enabled({"enabled": False}) is False
    assert srv._status_hints_enabled({"enabled": "false"}) is False


def test_cfg_number_falls_back_on_garbage():
    assert srv._cfg_number({"timeout": 3}, "timeout", 5.0) == 3.0
    assert srv._cfg_number({"timeout": "x"}, "timeout", 5.0) == 5.0
    assert srv._cfg_number({}, "timeout", 5.0) == 5.0


def test_generate_status_hints_sanitizes_and_strips_think_blocks():
    reply = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content='<think>The user asks about 3 things</think>["Looking through recent statements", "Found 3 files"]'
                )
            )
        ]
    )
    with patch.object(srv, "_call_status_hints_model", return_value=reply) as call:
        lines = srv._generate_status_hints("who is <PERSON_1>", max_lines=4, timeout=5.0)
    assert lines == ["Looking through recent statements\u2026"]
    messages = call.call_args.kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "who is <PERSON_1>"}
    assert call.call_args.kwargs["max_tokens"] == 160
    assert call.call_args.kwargs["timeout"] == 5.0


def test_generate_status_hints_returns_nothing_on_any_failure():
    with patch.object(srv, "_call_status_hints_model", side_effect=RuntimeError("no provider")):
        assert srv._generate_status_hints("q", max_lines=4, timeout=5.0) == []


# --- _start_turn_status -----------------------------------------------------------


def test_no_chat_id_means_no_status_line():
    q = queue.Queue()
    with patch.object(srv, "_status_hints_config", return_value={"enabled": True}):
        assert _adapter()._start_turn_status(q, "who is the client") is None
    assert _drain(q) == []


def test_disabled_task_means_no_status_line():
    set_end_user_chat_id(CHAT)
    q = queue.Queue()
    with patch.object(srv, "_status_hints_config", return_value={"enabled": False}):
        assert _adapter()._start_turn_status(q, "who is the client") is None
    assert _drain(q) == []


def test_a_turn_with_a_chat_id_gets_the_reading_frame_and_the_hints():
    set_end_user_chat_id(CHAT)
    q = queue.Queue()

    async def run():
        with patch.object(srv, "_status_hints_config", return_value={"enabled": True, "timeout": 5}), \
             patch.object(srv, "_generate_status_hints", return_value=["Looking through statements\u2026"]) as gen:
            ts = _adapter()._start_turn_status(q, "who is the client")
            assert isinstance(ts, TurnStatus)
            await _settle()
            gen.assert_called_once_with("who is the client", 4, 5.0)
        return ts

    ts = asyncio.run(run())
    items = _drain(q)
    assert items[0] == ("__status__", {"description": READING_LINE, "done": False, "hidden": False})
    assert ("__hints__", ["Looking through statements\u2026"]) in items
    assert ts.cadence_seconds == 6.0 and ts.max_lines == 4


def test_config_knobs_reach_the_machine_and_the_call():
    set_end_user_chat_id(CHAT)
    q = queue.Queue()

    async def run():
        cfg = {"enabled": True, "timeout": 2, "max_lines": 2, "cadence_seconds": 3}
        with patch.object(srv, "_status_hints_config", return_value=cfg), \
             patch.object(srv, "_generate_status_hints", return_value=[]) as gen:
            ts = _adapter()._start_turn_status(q, "who is the client")
            await _settle()
            gen.assert_called_once_with("who is the client", 2, 2.0)
        return ts

    ts = asyncio.run(run())
    assert ts.cadence_seconds == 3.0 and ts.max_lines == 2
    assert ("__hints__", []) in _drain(q)


def test_an_empty_question_starts_the_line_but_makes_no_side_call():
    set_end_user_chat_id(CHAT)
    q = queue.Queue()

    async def run():
        with patch.object(srv, "_status_hints_config", return_value={"enabled": True}), \
             patch.object(srv, "_generate_status_hints") as gen:
            ts = _adapter()._start_turn_status(q, [{"type": "image_url", "image_url": {"url": "d"}}])
            await _settle()
            gen.assert_not_called()
        return ts

    assert asyncio.run(run()) is not None
    assert [tag for tag, _ in _drain(q)] == ["__status__"]


def test_a_hung_side_call_still_delivers_empty_hints_within_the_bound():
    set_end_user_chat_id(CHAT)
    q = queue.Queue()
    release = __import__("threading").Event()

    def hang(*_a, **_k):
        release.wait(5)  # longer than the 0.2 s timeout below
        return ["Never\u2026"]

    async def run():
        cfg = {"enabled": True, "timeout": 0.2}
        with patch.object(srv, "_status_hints_config", return_value=cfg), \
             patch.object(srv, "_generate_status_hints", side_effect=hang):
            _adapter()._start_turn_status(q, "who is the client")
            await _settle()  # returns after the 1.2 s bound, not after the hang
        release.set()  # free the executor thread BEFORE asyncio.run shuts the executor down

    asyncio.run(run())
    assert ("__hints__", []) in _drain(q)