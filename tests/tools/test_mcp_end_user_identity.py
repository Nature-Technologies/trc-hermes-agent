"""Per-request end-user identity forwarding onto outbound MCP tool calls.

Why this exists
---------------
One shared Hermes process serves many end users. An MCP server that enforces
per-user document permissions (RAGnarok) must be told who the REAL caller is on
every ``tools/call``, and that identity must not be something the reasoning
model chose. Open WebUI mints a short-lived HS256 JWT per user and sends it as
``X-OpenWebUI-User-Jwt``; Hermes forwards it verbatim as
``X-Hermes-End-User-Jwt`` on the outbound MCP request.

The hard part is that the MCP streamable-HTTP connection is opened ONCE at
startup (one long-lived ``httpx.AsyncClient`` + ``ClientSession`` per server,
living on a dedicated background event loop), so the static
``mcp_servers[].headers`` block is process-wide by construction. The forwarding
therefore happens per-POST via an httpx ``request`` event hook that reads an
identity armed on the server object for the duration of one ``tools/call``,
under the per-server ``_rpc_lock`` that already serializes RPCs.

A wrong token here cross-contaminates users, so these tests assert the
outbound HTTP request headers of a REAL ``httpx`` round trip driven through the
REAL production request hook — not a mock's call args.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

pytest.importorskip("mcp")


MCP_URL = "https://ragnarok.example.com/mcp"

# Shaped like real HS256 JWTs (header.payload.signature, base64url alphabet)
# so the ingress charset guard sees realistic input.
TOKEN_ALICE = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhbGljZSJ9.c2lnLWFsaWNl"
TOKEN_BOB = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJib2IifQ.c2lnLWJvYg"

OUTBOUND_HEADER = "x-hermes-end-user-jwt"  # httpx.Headers lookups are casefolded


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _ok_result(text: str = "ok"):
    """A minimal successful ``CallToolResult`` shape."""
    result = MagicMock()
    result.isError = False
    block = MagicMock()
    block.text = text
    result.content = [block]
    result.structuredContent = None
    return result


def _connect_http_server(mcp_tool, name: str, config: dict):
    """Drive the REAL ``_run_http`` once and return ``(server, client_kwargs)``.

    ``client_kwargs`` is what ``_run_http`` passed to ``httpx.AsyncClient`` —
    including the static header block and the ``event_hooks`` mapping. Tests
    rebuild a real client from those kwargs so the production request hook is
    the thing under test.

    Runs on the MCP background loop so every asyncio primitive on the server
    object (notably ``_rpc_lock``) binds to the same loop the tool handler
    will later use.
    """
    server = mcp_tool.MCPServerTask(name)
    captured: dict = {}

    class DummyAsyncClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class DummyTransportCtx:
        async def __aenter__(self):
            return MagicMock(), MagicMock(), (lambda: None)

        async def __aexit__(self, *a):
            return False

    class DummySession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def initialize(self):
            return None

    async def _discover(self):
        # Ends the (otherwise endless) lifecycle wait so _run_http returns.
        self._shutdown_event.set()

    async def _drive():
        with patch("tools.mcp_tool._MCP_HTTP_AVAILABLE", True), \
             patch("tools.mcp_tool._MCP_NEW_HTTP", True), \
             patch("httpx.AsyncClient", DummyAsyncClient), \
             patch("tools.mcp_tool.streamable_http_client",
                   return_value=DummyTransportCtx()), \
             patch("tools.mcp_tool.ClientSession", DummySession), \
             patch.object(mcp_tool.MCPServerTask, "_discover_tools", _discover):
            await server._run_http(dict(config))

    mcp_tool._run_on_mcp_loop(_drive, timeout=15)
    server._shutdown_event.clear()
    return server, captured


def _wire_outbound(server, client_kwargs: dict, outbound: list):
    """Give *server* a session whose ``call_tool`` makes a real outbound POST.

    The POST goes through a genuine ``httpx.AsyncClient`` built from the kwargs
    ``_run_http`` produced (static headers + production event hooks) over a
    ``MockTransport``, so ``outbound`` records the headers that would really
    have gone on the wire.
    """
    def _transport(request: httpx.Request) -> httpx.Response:
        outbound.append(request.headers)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": {}}
        )

    hooks = dict(client_kwargs.get("event_hooks") or {})
    static_headers = dict(client_kwargs.get("headers") or {})

    async def _call_tool(tool_name, arguments=None, **kwargs):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_transport),
            headers=static_headers,
            event_hooks=hooks,
        ) as client:
            await client.post(MCP_URL, json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments or {}},
            })
        return _ok_result()

    session = MagicMock()
    session.call_tool = _call_tool
    server.session = session


@pytest.fixture
def ragnarok(request):
    """A connected, opt-in MCP server plus its captured outbound headers.

    Yields ``(handler, outbound, server)`` where *handler* is the real registry
    tool handler and *outbound* accumulates one ``httpx.Headers`` per call.
    """
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
def identity():
    """Bind/unbind the per-request end-user identity ContextVar."""
    from gateway.session_context import (
        reset_end_user_identity,
        set_end_user_identity,
    )

    tokens = []

    def _bind(value):
        tokens.append(set_end_user_identity(value))

    try:
        yield _bind
    finally:
        for tok in reversed(tokens):
            reset_end_user_identity(tok)


# ---------------------------------------------------------------------------
# Acceptance case (a) — inbound identity reaches the outbound MCP request
# ---------------------------------------------------------------------------


def test_outbound_mcp_call_carries_this_requests_token(ragnarok, identity):
    handler, outbound, _server = ragnarok

    identity(TOKEN_ALICE)
    result = handler({"q": "salary bands"})

    assert json.loads(result) == {"result": "ok"}
    assert len(outbound) == 1
    assert outbound[0][OUTBOUND_HEADER] == TOKEN_ALICE


# ---------------------------------------------------------------------------
# Acceptance case (b) — no caching, no cross-user leak
# ---------------------------------------------------------------------------


def test_sequential_requests_each_carry_their_own_token(ragnarok, identity):
    """Two different inbound tokens must produce two different outbound tokens.

    This is the cross-contamination guard: a cached or server-level-sticky
    token would make Bob's call carry Alice's identity and hand Bob Alice's
    documents.
    """
    from gateway.session_context import (
        reset_end_user_identity,
        set_end_user_identity,
    )

    handler, outbound, _server = ragnarok

    tok = set_end_user_identity(TOKEN_ALICE)
    handler({"q": "alice doc"})
    reset_end_user_identity(tok)

    tok = set_end_user_identity(TOKEN_BOB)
    handler({"q": "bob doc"})
    reset_end_user_identity(tok)

    assert [h[OUTBOUND_HEADER] for h in outbound] == [TOKEN_ALICE, TOKEN_BOB]


def test_identity_is_released_after_each_call(ragnarok, identity):
    """Nothing may stay armed on the server once the call returns.

    A residual token is what would leak into the NEXT user's call.
    """
    handler, _outbound, server = ragnarok

    identity(TOKEN_ALICE)
    handler({"q": "x"})

    assert server._end_user_identity is None


def test_a_later_anonymous_call_does_not_inherit_the_previous_token(ragnarok):
    """Identity present, then absent → the second call must carry nothing.

    Missing identity must never fall back to some other user's token.
    """
    from gateway.session_context import (
        reset_end_user_identity,
        set_end_user_identity,
    )

    handler, outbound, _server = ragnarok

    tok = set_end_user_identity(TOKEN_ALICE)
    handler({"q": "alice doc"})
    reset_end_user_identity(tok)

    handler({"q": "anonymous"})

    assert outbound[0][OUTBOUND_HEADER] == TOKEN_ALICE
    assert OUTBOUND_HEADER not in outbound[1]


# ---------------------------------------------------------------------------
# Acceptance case (c) — absent identity behaves exactly as before
# ---------------------------------------------------------------------------


def test_no_identity_means_no_header_and_no_error(ragnarok):
    handler, outbound, _server = ragnarok

    result = handler({"q": "public doc"})

    assert json.loads(result) == {"result": "ok"}
    assert len(outbound) == 1
    assert OUTBOUND_HEADER not in outbound[0]


def test_blank_identity_is_treated_as_absent(ragnarok, identity):
    handler, outbound, _server = ragnarok

    identity("   ")
    result = handler({"q": "public doc"})

    assert json.loads(result) == {"result": "ok"}
    assert OUTBOUND_HEADER not in outbound[0]


# ---------------------------------------------------------------------------
# Opt-in gate — never leak a user's signed identity to an unrelated server
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ragnarok", [{"forward_user_identity": False}], indirect=True)
def test_server_without_optin_never_receives_the_identity(ragnarok, identity):
    """A configured-but-not-opted-in MCP server must see no identity header.

    The forwarded value is a live HS256 credential; handing it to every remote
    MCP server a user happens to configure would be a credential leak.
    """
    handler, outbound, server = ragnarok

    identity(TOKEN_ALICE)
    result = handler({"q": "x"})

    assert json.loads(result) == {"result": "ok"}
    assert OUTBOUND_HEADER not in outbound[0]
    assert server._end_user_identity is None


def test_optin_server_installs_a_request_hook_and_optout_does_not():
    """The hook is the injection point; assert it is wired only when opted in."""
    from tools import mcp_tool

    mcp_tool._ensure_mcp_loop()

    _srv_on, kwargs_on = _connect_http_server(
        mcp_tool, "on", {"url": MCP_URL, "forward_user_identity": True},
    )
    _srv_off, kwargs_off = _connect_http_server(
        mcp_tool, "off", {"url": MCP_URL},
    )

    assert (kwargs_on.get("event_hooks") or {}).get("request")
    assert not (kwargs_off.get("event_hooks") or {}).get("request")


# ---------------------------------------------------------------------------
# Coexistence with the existing static credential
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ragnarok", [{
    "headers": {"Authorization": "Bearer service-level-cred"},
}], indirect=True)
def test_static_service_credential_is_preserved_alongside_the_identity(
    ragnarok, identity,
):
    """Service identity (Authorization) and end-user identity are two layers.

    Forwarding the end user must not clobber the server's own credential.
    """
    handler, outbound, _server = ragnarok

    identity(TOKEN_ALICE)
    handler({"q": "x"})

    assert outbound[0]["authorization"] == "Bearer service-level-cred"
    assert outbound[0][OUTBOUND_HEADER] == TOKEN_ALICE


@pytest.mark.parametrize("ragnarok", [{
    "user_identity_header": "X-RAGnarok-Caller",
}], indirect=True)
def test_outbound_header_name_is_configurable(ragnarok, identity):
    """So the MCP server end can be aligned without a Hermes code change."""
    handler, outbound, _server = ragnarok

    identity(TOKEN_ALICE)
    handler({"q": "x"})

    assert outbound[0]["x-ragnarok-caller"] == TOKEN_ALICE
    assert OUTBOUND_HEADER not in outbound[0]


# ---------------------------------------------------------------------------
# Hermes forwards only — it never mints an identity
# ---------------------------------------------------------------------------


def test_hermes_does_not_synthesize_an_identity_from_session_vars(ragnarok):
    """A bound gateway session user must NOT become a forged MCP identity.

    Only a token that actually arrived on the wire may be forwarded.
    """
    from gateway.session_context import clear_session_vars, set_session_vars

    handler, outbound, _server = ragnarok

    tokens = set_session_vars(
        platform="api_server", user_id="alice", user_name="Alice",
    )
    try:
        handler({"q": "x"})
    finally:
        clear_session_vars(tokens)

    assert OUTBOUND_HEADER not in outbound[0]
