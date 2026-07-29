"""Inbound capture of the Open WebUI conversation id and turn id.

RAGnarok keys its PII-masking namespace by `<user_id>:<chat_id>` at two
independent mask points. Drop the chat id and the two ends key differently, so
`<PERSON_1>` denotes different people at each end and the user is shown raw
placeholder tokens. This is not telemetry.

Two properties matter as much as the happy path: the values must NOT survive
into a later request (the process is shared, so a sticky value merges two
conversations), and Hermes must never invent one.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.platforms.api_server import APIServerAdapter
from gateway.session_context import (
    clear_session_vars,
    get_end_user_chat_id,
    get_end_user_request_id,
    set_end_user_chat_id,
    set_end_user_request_id,
)

CHAT_A = "0f7c1a2e-9b3d-4c5f-8a1b-2d3e4f5a6b7c"
CHAT_B = "3a9d5e71-2c4b-4f8a-9e0d-1b2c3d4e5f60"
MSG_A = "9d1f4b60-1111-4222-8333-444455556666"

CHAT_HEADER = "X-OpenWebUI-Chat-Id"
MSG_HEADER = "X-OpenWebUI-Message-Id"


async def _probe_app(seen: list):
    async def _probe(request):
        seen.append((get_end_user_chat_id(), get_end_user_request_id()))
        return web.json_response({"ok": True})

    app = web.Application(
        middlewares=[APIServerAdapter._make_end_user_identity_middleware()]
    )
    app.router.add_route("POST", "/v1/chat/completions", _probe)
    return app


def _run(coro):
    return asyncio.run(coro)


def test_inbound_chat_id_and_request_id_are_bound():
    seen: list = []

    async def _drive():
        app = await _probe_app(seen)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={"messages": []},
                headers={CHAT_HEADER: CHAT_A, MSG_HEADER: MSG_A},
            )
            assert resp.status == 200

    _run(_drive())
    assert seen == [(CHAT_A, MSG_A)]


def test_absent_headers_leave_both_unset():
    seen: list = []

    async def _drive():
        app = await _probe_app(seen)
        async with TestClient(TestServer(app)) as client:
            await client.post("/v1/chat/completions", json={"messages": []})

    _run(_drive())
    assert seen == [(None, None)]


def test_chat_id_does_not_leak_into_a_later_request():
    """A sticky chat id would merge two conversations' mapping namespaces."""
    seen: list = []

    async def _drive():
        app = await _probe_app(seen)
        async with TestClient(TestServer(app)) as client:
            await client.post("/v1/chat/completions", json={},
                              headers={CHAT_HEADER: CHAT_A})
            await client.post("/v1/chat/completions", json={})
            await client.post("/v1/chat/completions", json={},
                              headers={CHAT_HEADER: CHAT_B})

    _run(_drive())
    assert [c for c, _ in seen] == [CHAT_A, None, CHAT_B]


def test_concurrent_requests_keep_their_own_chat_ids():
    seen: dict = {}
    gate = asyncio.Event()

    async def _drive():
        async def _probe(request):
            who = request.headers.get(CHAT_HEADER, "")
            if who == CHAT_A:
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
                            headers={CHAT_HEADER: CHAT_A}),
                client.post("/v1/chat/completions", json={},
                            headers={CHAT_HEADER: CHAT_B}),
            )

    _run(_drive())
    assert seen == {CHAT_A: CHAT_A, CHAT_B: CHAT_B}


@pytest.mark.parametrize("bad", [
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
            await client.post("/v1/chat/completions", json={},
                              headers={CHAT_HEADER: bad})

    _run(_drive())
    assert seen == [(None, None)]


def test_clear_session_vars_zeroes_chat_and_request_ids():
    """clear_session_vars must not leave a stale chat/turn id for whatever
    task-spawning code runs next in this context.

    Mirrors the guarantee ``clear_session_vars`` already gives the end-user
    identity token: a task spawned from a context where a concurrent request
    had bound its chat id would otherwise mask that spawned task's output
    under the WRONG conversation's namespace.
    """

    async def _drive():
        set_end_user_chat_id(CHAT_A)
        set_end_user_request_id(MSG_A)
        assert get_end_user_chat_id() == CHAT_A
        assert get_end_user_request_id() == MSG_A

        clear_session_vars([])

        assert get_end_user_chat_id() is None
        assert get_end_user_request_id() is None

    _run(_drive())
