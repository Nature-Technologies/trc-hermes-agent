"""Inbound capture of the Open WebUI per-user identity JWT.

Open WebUI, with ``ENABLE_FORWARD_USER_INFO_HEADERS=true`` and
``FORWARD_USER_INFO_HEADER_JWT_SECRET`` set, mints a short-lived HS256 JWT per
user (claims: ``sub``/``email``/``name``/``role``/``iss``/``iat``/``exp``) and
sends it on every proxied request as ``X-OpenWebUI-User-Jwt``.

Hermes binds that value to a per-request ContextVar so the MCP client can
forward it on the outbound ``tools/call``. Two properties matter more than the
happy path:

  * it must NOT survive into a later request (the process is shared, so a
    sticky value is a cross-user document leak);
  * Hermes must never invent one — an absent header means no identity.

Hermes does not verify the signature; RAGnarok owns verification against the
shared secret. Hermes is a forwarder.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.platforms.api_server import APIServerAdapter
from gateway.session_context import get_end_user_identity


TOKEN_ALICE = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhbGljZSJ9.c2lnLWFsaWNl"
TOKEN_BOB = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJib2IifQ.c2lnLWJvYg"

INBOUND_HEADER = "X-OpenWebUI-User-Jwt"


async def _probe_app(seen: list):
    """An app wired with only the identity middleware and a probe route.

    The probe records what ``get_end_user_identity()`` returns *inside* the
    request, which is the contract the MCP client depends on.
    """
    async def _probe(request):
        seen.append(get_end_user_identity())
        return web.json_response({"ok": True})

    app = web.Application(
        middlewares=[APIServerAdapter._make_end_user_identity_middleware()]
    )
    app.router.add_route("POST", "/v1/chat/completions", _probe)
    return app


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Ingress binding
# ---------------------------------------------------------------------------


def test_inbound_jwt_is_bound_to_the_request_context():
    seen: list = []

    async def _drive():
        app = await _probe_app(seen)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={"messages": []},
                headers={INBOUND_HEADER: TOKEN_ALICE},
            )
            assert resp.status == 200

    _run(_drive())
    assert seen == [TOKEN_ALICE]


def test_absent_header_leaves_the_identity_unset():
    seen: list = []

    async def _drive():
        app = await _probe_app(seen)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/v1/chat/completions", json={"messages": []})
            assert resp.status == 200

    _run(_drive())
    assert seen == [None]


def test_identity_does_not_leak_into_a_later_request():
    """A request with no identity must not observe the previous request's.

    This is the shared-process invariant: request-scoped, never process-global.
    """
    seen: list = []

    async def _drive():
        app = await _probe_app(seen)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/v1/chat/completions",
                json={"messages": []},
                headers={INBOUND_HEADER: TOKEN_ALICE},
            )
            await client.post("/v1/chat/completions", json={"messages": []})
            await client.post(
                "/v1/chat/completions",
                json={"messages": []},
                headers={INBOUND_HEADER: TOKEN_BOB},
            )

    _run(_drive())
    assert seen == [TOKEN_ALICE, None, TOKEN_BOB]


def test_concurrent_requests_keep_their_own_identities():
    """Interleaved in-flight requests must not observe each other's token."""
    seen: dict = {}
    gate = asyncio.Event()

    async def _drive():
        async def _probe(request):
            who = request.headers.get(INBOUND_HEADER, "")
            if who == TOKEN_ALICE:
                # Hold Alice open until Bob has been admitted and bound, so
                # the two requests genuinely overlap.
                await gate.wait()
            else:
                gate.set()
            seen[who] = get_end_user_identity()
            return web.json_response({"ok": True})

        app = web.Application(
            middlewares=[APIServerAdapter._make_end_user_identity_middleware()]
        )
        app.router.add_route("POST", "/v1/chat/completions", _probe)

        async with TestClient(TestServer(app)) as client:
            await asyncio.gather(
                client.post("/v1/chat/completions", json={},
                            headers={INBOUND_HEADER: TOKEN_ALICE}),
                client.post("/v1/chat/completions", json={},
                            headers={INBOUND_HEADER: TOKEN_BOB}),
            )

    _run(_drive())
    assert seen == {TOKEN_ALICE: TOKEN_ALICE, TOKEN_BOB: TOKEN_BOB}


# ---------------------------------------------------------------------------
# Ingress hygiene — the value is re-emitted as an outbound HTTP header
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "abc\r\nX-Injected: evil",
    "abc\ndef",
    "abc\x00def",
    "tok\x7fen",
    "café",
])
def test_malformed_header_values_are_rejected_not_forwarded(bad):
    """A value that cannot legally sit in an HTTP header is dropped.

    Treated as absent rather than sanitized: a mangled identity is not an
    identity, and forwarding a partially-stripped value could authenticate the
    wrong subject.
    """
    seen: list = []

    async def _drive():
        app = await _probe_app(seen)
        async with TestClient(TestServer(app)) as client:
            try:
                await client.post(
                    "/v1/chat/completions",
                    json={"messages": []},
                    headers={INBOUND_HEADER: bad},
                )
            except ValueError:
                # aiohttp's client refuses to even send some of these; that is
                # a stronger guarantee than our own guard, so it counts.
                seen.append(None)

    _run(_drive())
    assert seen == [None]


def test_over_long_header_value_is_rejected():
    """Well above any real token, but below aiohttp's own header-field limit,
    so the request reaches our guard instead of being refused as a bare 431."""
    seen: list = []

    async def _drive():
        app = await _probe_app(seen)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={"messages": []},
                headers={INBOUND_HEADER: "a" * 5000},
            )
            assert resp.status == 200

    _run(_drive())
    assert seen == [None]


def test_inbound_header_name_is_configurable(monkeypatch):
    """Mirrors Open WebUI's own ``FORWARD_USER_INFO_HEADER_JWT`` override."""
    import gateway.platforms.api_server as api_server

    monkeypatch.setattr(api_server, "_END_USER_JWT_HEADER", "X-Custom-Identity")
    seen: list = []

    async def _drive():
        app = await _probe_app(seen)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/v1/chat/completions",
                json={"messages": []},
                headers={"X-Custom-Identity": TOKEN_ALICE},
            )

    _run(_drive())
    assert seen == [TOKEN_ALICE]


# ---------------------------------------------------------------------------
# The executor hop — ContextVars do not follow run_in_executor
# ---------------------------------------------------------------------------


def test_identity_survives_the_agent_thread_hop():
    """``_run_agent`` runs the agent in a thread executor, which starts with a
    fresh context. The request's identity must be re-established there, or the
    MCP tool handler (which reads it on the agent thread) sees nothing.
    """
    from gateway.session_context import (
        reset_end_user_identity,
        set_end_user_identity,
    )

    adapter = APIServerAdapter.__new__(APIServerAdapter)
    # Bare instance: supply only the in-flight bookkeeping _run_agent touches.
    adapter._inflight_agent_runs = 0
    adapter._activate_admitted_request = lambda: None
    observed: list = []

    class _FakeAgent:
        session_id = "s1"
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def run_conversation(self, **kwargs):
            # Runs on the executor thread — exactly where the MCP tool
            # handler reads the identity.
            observed.append(get_end_user_identity())
            return {"final_response": "hi", "completed": True}

    async def _drive():
        tok = set_end_user_identity(TOKEN_ALICE)
        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(APIServerAdapter, "_create_agent",
                           lambda self, **kw: _FakeAgent())
                mp.setattr(APIServerAdapter, "_profile_scope",
                           staticmethod(lambda profile: __import__(
                               "contextlib").nullcontext()))
                await adapter._run_agent(
                    user_message="hi",
                    conversation_history=[],
                    session_id="s1",
                )
        finally:
            reset_end_user_identity(tok)

    _run(_drive())
    assert observed == [TOKEN_ALICE]
