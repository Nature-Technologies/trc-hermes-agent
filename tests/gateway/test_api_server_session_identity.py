"""One Open WebUI chat is one Hermes session -- even for identical openers.

The session id used to be a hash of (system prompt + first user message), with
no user id and no chat id in the seed. Two chats that open with the same words
collided onto one session record and sandbox directory. The Open WebUI filter
masks that first message, so a PII-free opener like "List the companies that you
have" is byte-identical across users, making the collision most likely for
exactly the questions people ask first.

The chat id alone is the right seed: Open WebUI chat ids are UUIDs, unique
across users, so they separate both threads and users. Identity is deliberately
NOT in the seed -- Hermes does not verify the identity JWT, and that JWT carries
a ~300s expiry, so anything seeded on it would rotate mid-conversation and split
one thread into many.
"""

from __future__ import annotations

from gateway.platforms.api_server import _derive_chat_session_id

CHAT_A = "0f7c1a2e-9b3d-4c5f-8a1b-2d3e4f5a6b7c"
CHAT_B = "3a9d5e71-2c4b-4f8a-9e0d-1b2c3d4e5f60"
SYSTEM = "You are TRC's assistant."
OPENER = "List the companies that you have"


def test_same_opener_in_different_chats_yields_different_sessions():
    """THE regression. Identical text, two chats, two sessions."""
    a = _derive_chat_session_id(SYSTEM, OPENER, chat_id=CHAT_A)
    b = _derive_chat_session_id(SYSTEM, OPENER, chat_id=CHAT_B)
    assert a != b


def test_same_chat_yields_a_stable_session_across_turns():
    """Turn 1 and turn 8 of one chat must map to one session."""
    first = _derive_chat_session_id(SYSTEM, OPENER, chat_id=CHAT_A)
    later = _derive_chat_session_id(SYSTEM, "and their bank details?", chat_id=CHAT_A)
    assert first == later


def test_message_content_does_not_affect_the_id_when_a_chat_id_is_present():
    """A masked opener changes between turns as tokens are allocated; the
    session id must not move with it."""
    a = _derive_chat_session_id(SYSTEM, "<PERSON_1> holdings", chat_id=CHAT_A)
    b = _derive_chat_session_id("a different system prompt", "", chat_id=CHAT_A)
    assert a == b


def test_shape_is_unchanged():
    """The id is interpolated into on-disk paths and passes a path-safety guard,
    so the api-<16 hex> shape must not change."""
    sid = _derive_chat_session_id(SYSTEM, OPENER, chat_id=CHAT_A)
    assert sid.startswith("api-")
    assert len(sid) == len("api-") + 16
    assert all(c in "0123456789abcdef" for c in sid[4:])


def test_without_a_chat_id_it_falls_back_to_the_content_hash():
    """Non-Open-WebUI OpenAI-compatible clients keep today's behaviour."""
    a = _derive_chat_session_id(SYSTEM, OPENER, chat_id=None)
    b = _derive_chat_session_id(SYSTEM, OPENER, chat_id=None)
    c = _derive_chat_session_id(SYSTEM, "a different opener", chat_id=None)
    assert a == b
    assert a != c
