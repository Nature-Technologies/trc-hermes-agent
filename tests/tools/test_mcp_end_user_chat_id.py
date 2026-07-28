"""Per-request conversation id forwarding onto outbound MCP tool calls.

Why this exists
---------------
RAGnarok masks PII at two points — the Open WebUI filter on the way in, and the
RAG pipeline around retrieval — and both must allocate placeholder tokens from
ONE session-scoped namespace, or ``<PERSON_1>`` means a different person at each
end and the answer never un-masks. That namespace is ``<user_id>:<chat_id>``.

The user id survives the hop as a signed JWT (see
``test_mcp_end_user_identity.py``). The chat id did NOT: Open WebUI sends it as
``X-OpenWebUI-Chat-Id``, but Hermes read only the identity header and stamped
only ``X-Hermes-End-User-Jwt`` outbound, so RAGnarok fell back to a bare user id
for its half of the namespace. Live symptom: the assistant's answer rendered
raw ``<ORGANIZATION_1>`` tokens to the user, because the tokens the RAG minted
lived in a mapping the un-mask call never looked at.

The reasoning model cannot be asked to carry the id instead — it invents ids
when told to pass one, and a wrong namespace fails silently.

Forwarding rides the SAME machinery as the identity: armed on the server object
for the duration of one ``tools/call`` under ``_rpc_lock``, stamped per-POST by
the httpx request hook. It is gated on the same ``forward_user_identity``
opt-in, deliberately — a deployment with one and not the other reproduces
exactly the bug this closes.

These tests assert the headers of a REAL ``httpx`` round trip driven through the
REAL production request hook, not a mock's call args.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

from tests.tools.test_mcp_end_user_identity import (  # noqa: E402
    MCP_URL,
    TOKEN_ALICE,
    _connect_http_server,
    _wire_outbound,
)

# Open WebUI chat ids are UUIDs.
CHAT_ALICE = "0f7c1a2e-9b3d-4c5f-8a1b-2d3e4f5a6b7c"
CHAT_BOB = "3a9d5e71-2c4b-4f8a-9e0d-1b2c3d4e5f60"

OUTBOUND_HEADER = "x-hermes-session-id"  # httpx.Headers lookups are casefolded
IDENTITY_HEADER = "x-hermes-end-user-jwt"


@pytest.fixture
def ragnarok(request):
    """A connected, opt-in MCP server plus its captured outbound headers."""
    from tools import mcp_tool

    config = {
        "url": MCP_URL,
        "forward_user_identity": True,
    }
    config.update(getattr(request, "param", None) or {})

    mcp_tool._ensure_mcp_loop()
    server, client_kwargs = _connect_http_server(mcp_tool, "ragnarok", config)

    outbound: list = []
    _wire_outbound(server, client_kwargs, outbound)
    mcp_tool._servers["ragnarok"] = server
    mcp_tool._server_error_counts.pop("ragnarok", None)

    handler = mcp_tool._make_tool_handler("ragnarok", "query", 15.0)
    try:
        yield handler, outbound, server
    finally:
        mcp_tool._servers.pop("ragnarok", None)
        mcp_tool._server_error_counts.pop("ragnarok", None)


@pytest.fixture
def chat_id():
    """Bind/unbind the per-request conversation id ContextVar."""
    from gateway.session_context import (
        reset_end_user_chat_id,
        set_end_user_chat_id,
    )

    tokens = []

    def _bind(value):
        tokens.append(set_end_user_chat_id(value))

    try:
        yield _bind
    finally:
        for tok in reversed(tokens):
            reset_end_user_chat_id(tok)


# ---------------------------------------------------------------------------
# The namespace half that was being dropped
# ---------------------------------------------------------------------------


def test_outbound_mcp_call_carries_this_requests_chat_id(ragnarok, chat_id):
    handler, outbound, _server = ragnarok

    chat_id(CHAT_ALICE)
    result = handler({"q": "list the companies"})

    assert json.loads(result) == {"result": "ok"}
    assert len(outbound) == 1
    assert outbound[0][OUTBOUND_HEADER] == CHAT_ALICE


def test_identity_and_chat_id_travel_together(ragnarok, chat_id):
    """Both halves of ``<user_id>:<chat_id>`` on one call, under one opt-in.

    This is the whole point: either half alone yields a namespace that does not
    match the Open WebUI filter's, and placeholders stop round-tripping.
    """
    from gateway.session_context import (
        reset_end_user_identity,
        set_end_user_identity,
    )

    handler, outbound, _server = ragnarok

    chat_id(CHAT_ALICE)
    tok = set_end_user_identity(TOKEN_ALICE)
    try:
        handler({"q": "x"})
    finally:
        reset_end_user_identity(tok)

    assert outbound[0][IDENTITY_HEADER] == TOKEN_ALICE
    assert outbound[0][OUTBOUND_HEADER] == CHAT_ALICE


# ---------------------------------------------------------------------------
# No stickiness — a shared process must not bleed one chat into another
# ---------------------------------------------------------------------------


def test_sequential_requests_each_carry_their_own_chat_id(ragnarok):
    """A sticky chat id would merge two conversations' mapping namespaces."""
    from gateway.session_context import (
        reset_end_user_chat_id,
        set_end_user_chat_id,
    )

    handler, outbound, _server = ragnarok

    tok = set_end_user_chat_id(CHAT_ALICE)
    handler({"q": "first chat"})
    reset_end_user_chat_id(tok)

    tok = set_end_user_chat_id(CHAT_BOB)
    handler({"q": "second chat"})
    reset_end_user_chat_id(tok)

    assert [h[OUTBOUND_HEADER] for h in outbound] == [CHAT_ALICE, CHAT_BOB]


def test_chat_id_is_released_after_each_call(ragnarok, chat_id):
    """Nothing may stay armed on the server once the call returns."""
    handler, _outbound, server = ragnarok

    chat_id(CHAT_ALICE)
    handler({"q": "x"})

    assert server._end_user_chat_id is None


def test_a_later_call_without_a_chat_id_does_not_inherit_the_previous_one(ragnarok):
    from gateway.session_context import (
        reset_end_user_chat_id,
        set_end_user_chat_id,
    )

    handler, outbound, _server = ragnarok

    tok = set_end_user_chat_id(CHAT_ALICE)
    handler({"q": "first chat"})
    reset_end_user_chat_id(tok)

    handler({"q": "no chat id"})

    assert outbound[0][OUTBOUND_HEADER] == CHAT_ALICE
    assert OUTBOUND_HEADER not in outbound[1]


# ---------------------------------------------------------------------------
# Absent chat id behaves exactly as before the feature existed
# ---------------------------------------------------------------------------


def test_no_chat_id_means_no_header_and_no_error(ragnarok):
    handler, outbound, _server = ragnarok

    result = handler({"q": "public doc"})

    assert json.loads(result) == {"result": "ok"}
    assert len(outbound) == 1
    assert OUTBOUND_HEADER not in outbound[0]


def test_blank_chat_id_is_treated_as_absent(ragnarok, chat_id):
    handler, outbound, _server = ragnarok

    chat_id("   ")
    result = handler({"q": "public doc"})

    assert json.loads(result) == {"result": "ok"}
    assert OUTBOUND_HEADER not in outbound[0]


# ---------------------------------------------------------------------------
# Opt-in gate — the conversation id is user data, not broadcast material
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ragnarok", [{"forward_user_identity": False}], indirect=True)
def test_server_without_optin_never_receives_the_chat_id(ragnarok, chat_id):
    """One opt-in covers both values; an un-opted server sees neither."""
    handler, outbound, server = ragnarok

    chat_id(CHAT_ALICE)
    result = handler({"q": "x"})

    assert json.loads(result) == {"result": "ok"}
    assert OUTBOUND_HEADER not in outbound[0]
    assert server._end_user_chat_id is None


@pytest.mark.parametrize("ragnarok", [{
    "session_id_header": "X-RAGnarok-Conversation",
}], indirect=True)
def test_outbound_header_name_is_configurable(ragnarok, chat_id):
    """So the MCP server end can be aligned without a Hermes code change."""
    handler, outbound, _server = ragnarok

    chat_id(CHAT_ALICE)
    handler({"q": "x"})

    assert outbound[0]["x-ragnarok-conversation"] == CHAT_ALICE
    assert OUTBOUND_HEADER not in outbound[0]


# ---------------------------------------------------------------------------
# Hermes forwards only — it never mints a conversation id
# ---------------------------------------------------------------------------


def test_hermes_does_not_synthesize_a_chat_id_from_session_vars(ragnarok):
    """Hermes' OWN session chat id is not Open WebUI's conversation id.

    ``_bind_api_server_session`` binds ``HERMES_SESSION_CHAT_ID`` to Hermes'
    gateway session id. Forwarding that as the mask namespace would key the RAG
    side by a value the filter has never seen — the same silent mismatch, just
    with a more convincing-looking id.
    """
    from gateway.session_context import clear_session_vars, set_session_vars

    handler, outbound, _server = ragnarok

    tokens = set_session_vars(
        platform="api_server", chat_id="hermes-session-abc", user_id="alice",
    )
    try:
        handler({"q": "x"})
    finally:
        clear_session_vars(tokens)

    assert OUTBOUND_HEADER not in outbound[0]
