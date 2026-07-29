"""Per-request conversation id forwarding onto outbound MCP tool calls.

RAGnarok masks PII at two points and both must allocate tokens from ONE
namespace keyed `<user_id>:<chat_id>`. The user id survives the hop as a signed
JWT; without this, the chat id did not, and the assistant rendered raw
`<ORGANIZATION_1>` tokens to the user.

Gated on the SAME `forward_user_identity` opt-in as the identity, deliberately:
a deployment with one and not the other reproduces exactly the bug this closes.

These tests assert the headers of a REAL httpx round trip driven through the
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

CHAT_A = "0f7c1a2e-9b3d-4c5f-8a1b-2d3e4f5a6b7c"
CHAT_B = "3a9d5e71-2c4b-4f8a-9e0d-1b2c3d4e5f60"
MSG_A = "9d1f4b60-1111-4222-8333-444455556666"

CHAT_HEADER = "x-hermes-chat-id"   # httpx.Headers lookups are casefolded
REQUEST_HEADER = "x-hermes-request-id"
IDENTITY_HEADER = "x-hermes-end-user-jwt"


@pytest.fixture
def ragnarok(request):
    from tools import mcp_tool

    config = {"url": MCP_URL, "forward_user_identity": True}
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
def chat_ctx():
    from gateway.session_context import (
        reset_end_user_chat_id,
        reset_end_user_request_id,
        set_end_user_chat_id,
        set_end_user_request_id,
    )

    tokens = []

    def _bind(chat_id, request_id=None):
        tokens.append((set_end_user_chat_id(chat_id),
                       set_end_user_request_id(request_id)))

    try:
        yield _bind
    finally:
        for chat_tok, req_tok in reversed(tokens):
            reset_end_user_request_id(req_tok)
            reset_end_user_chat_id(chat_tok)


def test_outbound_call_carries_this_requests_chat_id(ragnarok, chat_ctx):
    handler, outbound, _server = ragnarok

    chat_ctx(CHAT_A, MSG_A)
    result = handler({"q": "list the companies"})

    assert json.loads(result) == {"result": "ok"}
    assert outbound[0][CHAT_HEADER] == CHAT_A
    assert outbound[0][REQUEST_HEADER] == MSG_A


def test_identity_and_chat_id_travel_together(ragnarok, chat_ctx):
    """Both halves of `<user_id>:<chat_id>` on one call, under one opt-in."""
    from gateway.session_context import (
        reset_end_user_identity,
        set_end_user_identity,
    )

    handler, outbound, _server = ragnarok

    chat_ctx(CHAT_A)
    tok = set_end_user_identity(TOKEN_ALICE)
    try:
        handler({"q": "x"})
    finally:
        reset_end_user_identity(tok)

    assert outbound[0][IDENTITY_HEADER] == TOKEN_ALICE
    assert outbound[0][CHAT_HEADER] == CHAT_A


def test_sequential_calls_each_carry_their_own_chat_id(ragnarok):
    """A sticky chat id would merge two conversations' mapping namespaces."""
    from gateway.session_context import (
        reset_end_user_chat_id,
        set_end_user_chat_id,
    )

    handler, outbound, _server = ragnarok

    tok = set_end_user_chat_id(CHAT_A)
    handler({"q": "first chat"})
    reset_end_user_chat_id(tok)

    tok = set_end_user_chat_id(CHAT_B)
    handler({"q": "second chat"})
    reset_end_user_chat_id(tok)

    assert [h[CHAT_HEADER] for h in outbound] == [CHAT_A, CHAT_B]


def test_chat_id_is_released_after_each_call(ragnarok, chat_ctx):
    handler, _outbound, server = ragnarok

    chat_ctx(CHAT_A)
    handler({"q": "x"})

    assert server._end_user_chat_id is None


def test_a_later_call_without_a_chat_id_does_not_inherit(ragnarok):
    from gateway.session_context import (
        reset_end_user_chat_id,
        set_end_user_chat_id,
    )

    handler, outbound, _server = ragnarok

    tok = set_end_user_chat_id(CHAT_A)
    handler({"q": "first"})
    reset_end_user_chat_id(tok)

    handler({"q": "no chat id"})

    assert outbound[0][CHAT_HEADER] == CHAT_A
    assert CHAT_HEADER not in outbound[1]


def test_no_chat_id_means_no_header_and_no_error(ragnarok):
    handler, outbound, _server = ragnarok

    result = handler({"q": "public doc"})

    assert json.loads(result) == {"result": "ok"}
    assert CHAT_HEADER not in outbound[0]


@pytest.mark.parametrize("ragnarok", [{"forward_user_identity": False}], indirect=True)
def test_server_without_optin_never_receives_the_chat_id(ragnarok, chat_ctx):
    """One opt-in covers both values; an un-opted server sees neither."""
    handler, outbound, server = ragnarok

    chat_ctx(CHAT_A)
    handler({"q": "x"})

    assert CHAT_HEADER not in outbound[0]
    assert server._end_user_chat_id is None


def test_hermes_does_not_synthesize_a_chat_id_from_session_vars(ragnarok):
    """Hermes' OWN session chat id is not Open WebUI's conversation id.

    Forwarding `HERMES_SESSION_CHAT_ID` would key the RAG side by a value the
    filter has never seen -- the same silent mismatch with a convincing id.
    """
    from gateway.session_context import clear_session_vars, set_session_vars

    handler, outbound, _server = ragnarok

    tokens = set_session_vars(platform="api_server", chat_id="hermes-session-abc")
    try:
        handler({"q": "x"})
    finally:
        clear_session_vars(tokens)

    assert CHAT_HEADER not in outbound[0]
