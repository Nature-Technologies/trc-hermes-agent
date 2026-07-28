"""Inbound capture of the Open WebUI conversation id.

Open WebUI, with ``ENABLE_FORWARD_USER_INFO_HEADERS=true``, sends the active
chat's id on every proxied request as ``X-OpenWebUI-Chat-Id`` (see
``routers/openai.py``). RAGnarok keys its PII-masking namespace by
``<user_id>:<chat_id>`` at BOTH mask points, so this value is not telemetry —
without it the two ends allocate placeholder tokens from different namespaces
and the user is shown raw ``<PERSON_1>`` tokens instead of real values.

Hermes binds it to a per-request ContextVar so the MCP client can stamp it on
the outbound ``tools/call``. The same two properties that matter for the
identity JWT matter here:

  * it must NOT survive into a later request — a sticky chat id merges two
    conversations' mapping namespaces in a shared process;
  * Hermes must never invent one — an absent header means no chat id, not
    Hermes' own session id (which the RAG side has never seen).

Hermes is a forwarder. It does not interpret the value.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.platforms.api_server import APIServerAdapter
from gateway.session_context import get_end_user_chat_id


CHAT_ALICE = "0f7c1a2e-9b3d-4c5f-8a1b-2d3e4f5a6b7c"
CHAT_BOB = "3a9d5e71-2c4b-4f8a-9e0d-1b2c3d4e5f60"

INBOUND_HEADER = "X-OpenWebUI-Chat-Id"


async def _probe_app(seen: list):
    """An app wired with only the identity middleware and a probe route.

    The chat id rides the SAME middleware as the identity JWT — one request
    scope, bound and released together — so this is the production wiring.
    """
    async def _probe(request):
        seen.append(get_end_user_chat_id())
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


def test_inbound_chat_id_is_bound_to_the_request_context():
    seen: list = []

    async def _drive():
        app = await _probe_app(seen)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={"messages": []},
                headers={INBOUND_HEADER: CHAT_ALICE},
            )
            assert resp.status == 200

    _run(_drive())
    assert seen == [CHAT_ALICE]


def test_absent_header_leaves_the_chat_id_unset():
    seen: list = []

    async def _drive():
        app = await _probe_app(seen)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/v1/chat/completions", json={"messages": []})
            assert resp.status == 200

    _run(_drive())
    assert seen == [None]


def test_chat_id_does_not_leak_into_a_later_request():
    """A request with no chat id must not observe the previous request's."""
    seen: list = []

    async def _drive():
        app = await _probe_app(seen)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/v1/chat/completions",
                json={"messages": []},
                headers={INBOUND_HEADER: CHAT_ALICE},
            )
            await client.post("/v1/chat/completions", json={"messages": []})
            await client.post(
                "/v1/chat/completions",
                json={"messages": []},
                headers={INBOUND_HEADER: CHAT_BOB},
            )

    _run(_drive())
    assert seen == [CHAT_ALICE, None, CHAT_BOB]


def test_concurrent_requests_keep_their_own_chat_ids():
    """Interleaved in-flight requests must not observe each other's chat id."""
    seen: dict = {}
    gate = asyncio.Event()

    async def _drive():
        async def _probe(request):
            who = request.headers.get(INBOUND_HEADER, "")
            if who == CHAT_ALICE:
                await gate.wait()
            else:
                gate.set()
            seen[who] = get_end_user_chat_id()
            return web.json_response({"ok": True})

        app = web.Application(
            middlewares=[APIServerAdapter._make_end_user_identity_middleware()]
        )
        app.router.add_route("POST", "/v1/chat/completions", _probe)

        async with TestClient(TestServer(app)) as client:
            await asyncio.gather(
                client.post("/v1/chat/completions", json={},
                            headers={INBOUND_HEADER: CHAT_ALICE}),
                client.post("/v1/chat/completions", json={},
                            headers={INBOUND_HEADER: CHAT_BOB}),
            )

    _run(_drive())
    assert seen == {CHAT_ALICE: CHAT_ALICE, CHAT_BOB: CHAT_BOB}


# ---------------------------------------------------------------------------
# Ingress hygiene — the value is re-emitted as an outbound HTTP header
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "abc\r\nX-Injected: evil",
    "has spaces",
    "curly{braces}",
    "x" * 5000,
])
def test_illegal_chat_id_is_dropped_not_sanitized(bad):
    """A value that cannot legally sit in a header is DROPPED whole.

    Partially stripping it would forward a chat id that is not the caller's,
    silently keying the mask namespace to the wrong conversation.
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
            except Exception:
                # aiohttp's own client rejects CR/LF before it reaches us —
                # equally acceptable: the value never gets bound either way.
                seen.append(None)

    _run(_drive())
    assert seen == [None]
