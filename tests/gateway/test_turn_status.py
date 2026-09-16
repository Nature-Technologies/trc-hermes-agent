"""The interim status line's state machine — pure, clock injected.

Spec: trc-backend docs/superpowers/specs/2026-09-16-interim-status-while-answering-design.md §4.
"""

from __future__ import annotations

from gateway.platforms.turn_status import (
    CANNED,
    COMPOSING_LINE,
    DONE_LINE,
    OPENERS,
    OTHER_TOOL_LINE,
    READING_LINE,
    STATUS_TEXT_RE,
    TurnStatus,
    data_tool_of,
    status_chunk,
    status_payload,
)

QUERY = "mcp__ragnarok__query"
REPORT = "mcp__ragnarok__generate_report"


class FakeClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _machine(cadence=6.0, max_lines=4, clock=None):
    clock = clock or FakeClock()
    return TurnStatus(clock=clock, cadence_seconds=cadence, max_lines=max_lines), clock


def _descriptions(frames):
    return [f["description"] for f in frames]


# --- naming and shapes -------------------------------------------------------------


def test_data_tool_of_recognises_the_four_ragnarok_tools_only():
    assert data_tool_of("mcp__ragnarok__query") == "query"
    assert data_tool_of("mcp__ragnarok__list_entities") == "list_entities"
    assert data_tool_of("mcp__ragnarok__generate_report") == "generate_report"
    assert data_tool_of("mcp__ragnarok__find_relationships") == "find_relationships"
    assert data_tool_of("mcp__ragnarok__ingest_document") is None
    assert data_tool_of("mcp__other__query") is None
    assert data_tool_of("session_search") is None
    assert data_tool_of("") is None


def test_status_payload_carries_exactly_three_keys():
    assert status_payload("Reading your question…") == {
        "description": "Reading your question…",
        "done": False,
        "hidden": False,
    }
    assert status_payload("", done=True, hidden=True) == {
        "description": "",
        "done": True,
        "hidden": True,
    }


def test_status_chunk_is_a_chat_completion_chunk_with_empty_choices_and_an_event():
    chunk = status_chunk("chatcmpl-1", "hermes", 1700000000, status_payload("Reading your question…"))
    assert chunk == {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "hermes",
        "choices": [],
        "event": {
            "type": "status",
            "data": {"description": "Reading your question…", "done": False, "hidden": False},
        },
    }


def test_every_fixed_line_matches_the_regex():
    lines = [READING_LINE, COMPOSING_LINE, DONE_LINE, OTHER_TOOL_LINE, *OPENERS.values()]
    for canned in CANNED.values():
        lines.extend(canned)
    for line in lines:
        assert STATUS_TEXT_RE.match(line), line


# --- the timeline ------------------------------------------------------------------


def test_a_greeting_shows_the_reading_line_then_hides():
    m, _ = _machine()
    assert _descriptions(m.on_request()) == [READING_LINE]
    assert m.on_content() == []  # ambiguous in `reading`: narration or a direct answer
    assert m.on_end() == [status_payload("", done=True, hidden=True)]
    assert m.state == "hidden"
    assert m.on_end() == []  # terminal is emitted once


def test_narration_then_a_query_call_then_the_answer():
    m, clock = _machine()
    m.on_request()
    assert m.on_content() == []  # pre-tool narration does not end the status
    assert _descriptions(m.on_tool_start(QUERY)) == [OPENERS["query"]]
    assert m.state == "searching"
    assert m.on_hints(["Looking through statements…", "Checking the agreement…"]) == []
    assert m.on_tick() == []  # cadence not reached
    clock.advance(6.0)
    assert _descriptions(m.on_tick()) == ["Looking through statements…"]
    clock.advance(6.0)
    assert _descriptions(m.on_tick()) == ["Checking the agreement…"]
    clock.advance(6.0)
    assert m.on_tick() == []  # exhausted: the last line holds
    assert _descriptions(m.on_tool_complete(QUERY)) == [COMPOSING_LINE]
    assert m.state == "composing"
    assert m.on_content() == [status_payload(DONE_LINE, done=True)]
    assert m.state == "done"
    assert m.on_end() == []


def test_hints_arriving_late_join_at_the_next_tick():
    m, clock = _machine()
    m.on_request()
    m.on_tool_start(QUERY)
    clock.advance(6.0)
    assert _descriptions(m.on_tick()) == [CANNED["query"][0]]  # no hints yet: canned
    m.on_hints(["Looking through statements…"])
    clock.advance(6.0)
    assert _descriptions(m.on_tick()) == ["Looking through statements…"]


def test_without_hints_the_canned_lines_rotate_then_hold():
    m, clock = _machine()
    m.on_request()
    m.on_tool_start(QUERY)
    m.on_hints([])  # zero survivors reads as "never arrived"
    clock.advance(6.0)
    assert _descriptions(m.on_tick()) == [CANNED["query"][0]]
    clock.advance(6.0)
    assert _descriptions(m.on_tick()) == [CANNED["query"][1]]
    clock.advance(60.0)
    assert m.on_tick() == []


def test_two_tool_calls_continue_the_hint_list_and_end_with_one_trace():
    m, clock = _machine()
    m.on_request()
    m.on_tool_start(QUERY)
    m.on_hints(["First…", "Second…", "Third…"])
    clock.advance(6.0)
    assert _descriptions(m.on_tick()) == ["First…"]
    m.on_tool_complete(QUERY)
    assert _descriptions(m.on_tool_start(REPORT)) == [OPENERS["generate_report"]]
    clock.advance(6.0)
    assert _descriptions(m.on_tick()) == ["Second…"]  # continues, does not restart
    m.on_tool_complete(REPORT)
    assert m.on_content() == [status_payload(DONE_LINE, done=True)]
    assert m.on_end() == []


def test_an_unexpected_tool_shows_working_and_ends_hidden():
    m, clock = _machine()
    m.on_request()
    assert _descriptions(m.on_tool_start("session_search")) == [OTHER_TOOL_LINE]
    m.on_hints(["Never shown…"])
    clock.advance(30.0)
    assert m.on_tick() == []  # no hints for a tool that is not a search of TRC records
    assert _descriptions(m.on_tool_complete("session_search")) == [COMPOSING_LINE]
    assert m.on_content() == [status_payload("", done=True, hidden=True)]
    assert m.state == "hidden"


def test_an_error_end_while_searching_leaves_no_shimmer():
    m, _ = _machine()
    m.on_request()
    m.on_tool_start(QUERY)
    assert m.on_end() == [status_payload(DONE_LINE, done=True)]
    assert m.state == "done"


def test_max_lines_caps_the_hints_kept():
    m, clock = _machine(max_lines=2)
    m.on_request()
    m.on_tool_start(QUERY)
    m.on_hints(["One…", "Two…", "Three…"])
    seen = []
    for _ in range(5):
        clock.advance(6.0)
        seen += _descriptions(m.on_tick())
    assert seen == ["One…", "Two…"]


def test_cadence_is_measured_from_the_last_frame_of_any_kind():
    m, clock = _machine()
    m.on_request()
    clock.advance(5.0)
    m.on_tool_start(QUERY)  # a frame: resets the cadence clock
    m.on_hints(["One…"])
    clock.advance(5.0)
    assert m.on_tick() == []  # only 5 s since the opener
    clock.advance(1.0)
    assert _descriptions(m.on_tick()) == ["One…"]


def test_nothing_rotates_outside_searching():
    m, clock = _machine()
    m.on_request()
    m.on_hints(["One…"])
    clock.advance(60.0)
    assert m.on_tick() == []  # reading
    m.on_tool_start(QUERY)
    m.on_tool_complete(QUERY)
    clock.advance(60.0)
    assert m.on_tick() == []  # composing
