"""Concurrent MCP tool calls: per-call identity, admission limits, deadlines.

Several RAGnarok calls from one turn (and from several users' turns) are now in
flight on ONE long-lived streamable-HTTP client at the same time. Two things
must hold that the old one-call-at-a-time ``_rpc_lock`` used to guarantee:

* every outbound POST carries the identity, chat id and request id of the end
  user whose call it is (invariant I1), and a POST that belongs to no call
  carries none (I2);
* concurrency is bounded per server and per end user, and the wait for a slot
  neither eats into the call's own timeout nor counts as a server failure.

Like test_mcp_end_user_identity, these drive the REAL ``_make_tool_handler`` and
the REAL production httpx request hook, and assert what went on the wire.
"""

from __future__ import annotations

import base64
import contextvars
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

pytest.importorskip("mcp")

from tests.tools.test_mcp_end_user_identity import (  # noqa: E402
    MCP_URL,
    _connect_http_server,
    _ok_result,
)

IDENTITY_HEADER = "x-hermes-end-user-jwt"
CHAT_HEADER = "x-hermes-chat-id"
REQUEST_HEADER = "x-hermes-request-id"
BRIDGED_HEADER = "x-hermes-bridged"


def _jwt(sub: str, iat: int) -> str:
    """An HS256-shaped token; a fresh ``iat`` per request, like Open WebUI mints."""
    def enc(obj) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"{enc({'alg': 'HS256'})}.{enc({'sub': sub, 'iat': iat})}.c2ln"


ALICE_1, ALICE_2, ALICE_3, ALICE_4 = (_jwt("alice", i) for i in range(1, 5))
BOB_1 = _jwt("bob", 1)
CHAT_A = "0f7c1a2e-9b3d-4c5f-8a1b-2d3e4f5a6b7c"
CHAT_B = "3a9d5e71-2c4b-4f8a-9e0d-1b2c3d4e5f60"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Wire:
    """Outbound side of a fake RAGnarok: records every POST, can hold them.

    ``session.call_tool`` makes a genuine POST through an ``httpx.AsyncClient``
    built from the kwargs ``_run_http`` produced (so the production request
    hook runs) over an async ``MockTransport``. The transport records the
    stamped headers and the body's ``_meta``, then sleeps ``delay`` seconds or
    waits for ``release`` — so tests can hold calls in flight.
    """

    def __init__(self, server, client_kwargs, *, delay=0.0, hold=False, text="ok"):
        self.records: list = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.delay = delay
        self.hold = hold
        self.text = text
        self._guard = threading.Lock()
        self.release = None  # asyncio.Event, created on the MCP loop
        hooks = dict(client_kwargs.get("event_hooks") or {})
        static_headers = dict(client_kwargs.get("headers") or {})

        async def _transport(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            with self._guard:
                self.records.append({
                    "headers": request.headers,
                    "meta": (body.get("params") or {}).get("_meta"),
                    "args": (body.get("params") or {}).get("arguments"),
                })
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                if self.hold:
                    await self._release_event().wait()
                elif self.delay:
                    import asyncio
                    await asyncio.sleep(self.delay)
            finally:
                with self._guard:
                    self.in_flight -= 1
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

        async def _call_tool(tool_name, arguments=None, *, meta=None, **kwargs):
            params = {"name": tool_name, "arguments": arguments or {}}
            if meta is not None:
                params["_meta"] = meta
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(_transport),
                headers=static_headers,
                event_hooks=hooks,
            ) as client:
                await client.post(MCP_URL, json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": params,
                })
            return _ok_result(self.text)

        session = MagicMock()
        session.call_tool = _call_tool
        server.session = session

    def _release_event(self):
        import asyncio
        if self.release is None:
            self.release = asyncio.Event()
        return self.release

    def open_gate(self, mcp_tool):
        async def _set():
            self._release_event().set()
        mcp_tool._run_on_mcp_loop(_set, timeout=5)

    def wait_for(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._guard:
                if predicate(self):
                    return True
            time.sleep(0.01)
        return False


def _as_user(handler, token, chat_id, request_id, args):
    """Run *handler* on this worker thread as one end user's agent thread.

    A fresh ``contextvars.Context`` per call, so a pooled worker thread can
    never carry the previous task's identity into this one.
    """
    def _run():
        from gateway.session_context import (
            set_end_user_chat_id,
            set_end_user_identity,
            set_end_user_request_id,
        )
        set_end_user_identity(token)
        set_end_user_chat_id(chat_id)
        set_end_user_request_id(request_id)
        return handler(args)
    return lambda: contextvars.Context().run(_run)


@pytest.fixture
def ragnarok_factory():
    """Build a connected opt-in RAGnarok with the given limits and wire."""
    from tools import mcp_tool

    created: list = []

    def _make(*, limits=None, tool_timeout=15.0, **wire_kwargs):
        config = {"url": MCP_URL, "forward_user_identity": True}
        config.update(limits or {})
        mcp_tool._ensure_mcp_loop()
        server, client_kwargs = _connect_http_server(mcp_tool, "ragnarok", config)
        wire = _Wire(server, client_kwargs, **wire_kwargs)
        mcp_tool._servers["ragnarok"] = server
        mcp_tool._server_error_counts.pop("ragnarok", None)
        created.append(server)
        handler = mcp_tool._make_tool_handler("ragnarok", "query", tool_timeout)
        return handler, wire, server

    try:
        yield _make
    finally:
        mcp_tool._servers.pop("ragnarok", None)
        mcp_tool._server_error_counts.pop("ragnarok", None)
        mcp_tool._server_breaker_opened_at.pop("ragnarok", None)


PARALLEL = {"max_concurrent_calls": 6, "max_concurrent_calls_per_user": 3}


def _mcp():
    from tools import mcp_tool
    return mcp_tool


# ---------------------------------------------------------------------------
# B1 — identity is looked up per call, from the request's own body
# ---------------------------------------------------------------------------


def _send_through_hook(entries: dict, body: dict) -> httpx.Headers:
    """POST *body* through the production hook for a server holding *entries*."""
    import asyncio

    from tools.mcp_tool import _make_end_user_identity_hook

    server = SimpleNamespace(_call_identities=entries)
    hook = _make_end_user_identity_hook(
        server, "X-Hermes-End-User-Jwt", "X-Hermes-Chat-Id", "X-Hermes-Request-Id",
    )
    seen: list = []

    def _transport(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, json={})

    async def _go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_transport),
            # A client-level default must never survive onto the wire.
            headers={"X-Hermes-End-User-Jwt": "client-default-must-not-leak"},
            event_hooks={"request": [hook]},
        ) as client:
            await client.post(MCP_URL, json=body)

    asyncio.run(_go())
    return seen[0]


def _live_entries():
    from tools.mcp_tool import _CallIdentity

    return {
        "call-a": _CallIdentity(ALICE_1, CHAT_A, "req-a", False),
        "call-b": _CallIdentity(BOB_1, CHAT_B, "req-b", True),
    }


def test_hook_stamps_the_entry_named_in_the_request_body():
    headers = _send_through_hook(_live_entries(), {
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "query", "arguments": {}, "_meta": {"hermes_call": "call-b"}},
    })

    assert headers[IDENTITY_HEADER] == BOB_1
    assert headers[CHAT_HEADER] == CHAT_B
    assert headers[REQUEST_HEADER] == "req-b"
    assert headers[BRIDGED_HEADER] == "1"


@pytest.mark.parametrize("body", [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "query", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
     "params": {"name": "query", "arguments": {}, "_meta": {"hermes_call": "finished"}}},
], ids=["initialize", "list_tools", "notification", "call-without-id", "call-already-finished"])
def test_a_request_without_a_live_entry_carries_no_identity(body):
    """Other calls are in flight (entries exist), but none is THIS request's."""
    headers = _send_through_hook(_live_entries(), body)

    assert IDENTITY_HEADER not in headers
    assert CHAT_HEADER not in headers
    assert REQUEST_HEADER not in headers
    assert BRIDGED_HEADER not in headers


def test_each_call_sends_its_own_correlation_id_and_leaves_nothing_armed(ragnarok_factory):
    handler, wire, server = ragnarok_factory()

    _as_user(handler, ALICE_1, CHAT_A, "req-1", {"q": "one"})()
    _as_user(handler, ALICE_2, CHAT_A, "req-2", {"q": "two"})()

    call_ids = [r["meta"]["hermes_call"] for r in wire.records]
    assert len(call_ids) == 2 and len(set(call_ids)) == 2
    assert [r["headers"][IDENTITY_HEADER] for r in wire.records] == [ALICE_1, ALICE_2]
    assert server._call_identities == {}


def test_a_server_without_optin_gets_no_meta_and_no_identity(ragnarok_factory):
    """No opt-in, no hook and no correlation id: a third-party server sees nothing new."""
    handler, wire, server = ragnarok_factory(limits={"forward_user_identity": False})

    _as_user(handler, ALICE_1, CHAT_A, "req-1", {"q": "x"})()

    assert wire.records[0]["meta"] is None
    assert IDENTITY_HEADER not in wire.records[0]["headers"]
    assert server._call_identities == {}


def test_a_failed_call_leaves_nothing_armed(ragnarok_factory):
    """Review Focus: a call that dies mid-flight (connection reset, reconnect)
    must not leave its identity entry behind for a later request to pick up."""
    handler, _wire, server = ragnarok_factory()
    armed_during_call: list = []

    async def _boom(tool_name, arguments=None, *, meta=None, **kwargs):
        armed_during_call.append(dict(server._call_identities))
        raise RuntimeError("connection reset mid-call")

    server.session.call_tool = _boom

    result = json.loads(_as_user(handler, ALICE_1, CHAT_A, "req-1", {"q": "x"})())

    assert "error" in result
    assert len(armed_during_call) == 1 and len(armed_during_call[0]) == 1
    assert server._call_identities == {}
