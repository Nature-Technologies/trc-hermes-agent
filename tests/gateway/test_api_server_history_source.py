"""H3 — which history a turn runs on: ours, or the client's.

The staging failure this closes: turn 3's request body carried none of turn 2's
`mcp__ragnarok__query` calls or their results, because Open WebUI only ever saw the
rendered assistant paragraph. The model was asked to "dig into" documents it could not
see it had ever retrieved, and its only visible move was to re-run the same search.
Hermes' own session file held all four tool calls the whole time.
"""

from __future__ import annotations

from gateway.platforms.api_server import choose_history


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _assistant(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _tool_call() -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]}


def test_the_first_turn_uses_the_body():
    body = [_user("hello")]
    assert choose_history(body, []) == (body, False)


def test_our_stored_history_wins_because_it_has_the_tool_calls():
    body = [_user("q1"), _assistant("rendered answer")]
    stored = [_user("q1"), _tool_call(), {"role": "tool", "content": "{...}"},
              _assistant("rendered answer")]
    chosen, diverged = choose_history(body, stored)
    assert chosen is stored
    assert diverged is False


def test_a_shorter_body_wins_and_reports_divergence():
    """A deleted or regenerated message in Open WebUI. Replaying our longer record
    would resurrect text the user removed on purpose."""
    body = [_user("q1")]
    stored = [_user("q1"), _assistant("a1"), _user("q2"), _assistant("a2")]
    chosen, diverged = choose_history(body, stored)
    assert chosen is body
    assert diverged is True


def test_equal_turn_counts_prefer_the_stored_history():
    body = [_user("q1"), _assistant("a1"), _user("q2"), _assistant("a2")]
    stored = [_user("q1"), _tool_call(), _assistant("a1"), _user("q2"), _assistant("a2")]
    assert choose_history(body, stored)[0] is stored
