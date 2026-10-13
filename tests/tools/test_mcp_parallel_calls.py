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
        server._configure_call_limits(config)
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


# ---------------------------------------------------------------------------
# B2 — bounded concurrency instead of one global lock
# ---------------------------------------------------------------------------


def test_two_users_interleaved_calls_each_carry_their_own_identity(ragnarok_factory):
    """Six calls from two users in flight at once: no POST carries the other's."""
    handler, wire, _server = ragnarok_factory(limits=PARALLEL, hold=True)
    bob = [_jwt("bob", i) for i in range(1, 4)]
    callers = [
        (ALICE_1, CHAT_A, "a-1"), (bob[0], CHAT_B, "b-1"),
        (ALICE_2, CHAT_A, "a-2"), (bob[1], CHAT_B, "b-2"),
        (ALICE_3, CHAT_A, "a-3"), (bob[2], CHAT_B, "b-3"),
    ]
    expected = {tok: (chat, req) for tok, chat, req in callers}

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [
            pool.submit(_as_user(handler, tok, chat, req, {"q": req}))
            for tok, chat, req in callers
        ]
        try:
            all_in_flight = wire.wait_for(lambda w: w.in_flight == 6)
        finally:
            wire.open_gate(_mcp())
        results = [json.loads(f.result(timeout=60)) for f in futures]

    assert all_in_flight, f"calls did not overlap (max in flight {wire.max_in_flight})"
    assert results == [{"result": "ok"}] * 6
    seen_tokens = set()
    for rec in wire.records:
        token = rec["headers"][IDENTITY_HEADER]
        chat, req = expected[token]
        assert rec["headers"][CHAT_HEADER] == chat
        assert rec["headers"][REQUEST_HEADER] == req
        assert rec["args"] == {"q": req}
        seen_tokens.add(token)
    assert seen_tokens == set(expected)


def test_three_parallel_calls_overlap_in_time(ragnarok_factory):
    handler, wire, _server = ragnarok_factory(limits=PARALLEL, delay=1.0)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(_as_user(handler, tok, CHAT_A, f"r-{i}", {"q": i}))
            for i, tok in enumerate((ALICE_1, ALICE_2, ALICE_3))
        ]
        results = [json.loads(f.result(timeout=30)) for f in futures]
    elapsed = time.monotonic() - started

    assert results == [{"result": "ok"}] * 3
    assert wire.max_in_flight == 3
    # Serial would be >= 3.0 s; overlapped is ~1.0 s. Loose bound for busy runners.
    assert elapsed < 2.5, f"took {elapsed:.2f}s: calls ran one after another"


def test_a_fourth_call_from_one_user_waits_while_another_users_call_does_not(
    ragnarok_factory,
):
    from tools.mcp_tool import _end_user_slot_key

    handler, wire, server = ragnarok_factory(limits=PARALLEL, hold=True)

    def _alice_count(w):
        return sum(1 for r in w.records if r["headers"][IDENTITY_HEADER] != BOB_1)

    with ThreadPoolExecutor(max_workers=5) as pool:
        try:
            first_three = [
                pool.submit(_as_user(handler, tok, CHAT_A, f"a-{i}", {"q": i}))
                for i, tok in enumerate((ALICE_1, ALICE_2, ALICE_3))
            ]
            assert wire.wait_for(lambda w: w.in_flight == 3)
            fourth = pool.submit(_as_user(handler, ALICE_4, CHAT_A, "a-4", {"q": 4}))
            bobs = pool.submit(_as_user(handler, BOB_1, CHAT_B, "b-1", {"q": "b"}))

            assert wire.wait_for(
                lambda w: any(r["headers"][IDENTITY_HEADER] == BOB_1 for r in w.records)
            ), "another user's call was blocked by this user's cap"
            alice_sem = server._user_call_semaphores[_end_user_slot_key(ALICE_4)]
            assert alice_sem.locked()
            assert _alice_count(wire) == 3, "the 4th call bypassed the per-user cap"
            assert not fourth.done()
        finally:
            wire.open_gate(_mcp())
        results = [json.loads(f.result(timeout=60)) for f in (*first_three, fourth, bobs)]

    assert results == [{"result": "ok"}] * 5
    assert _alice_count(wire) == 4


def test_call_limits_default_to_one_and_stdio_stays_serialized():
    from tools.mcp_tool import MCPServerTask

    http_default = MCPServerTask("a")
    http_default._configure_call_limits({"url": MCP_URL})
    assert http_default._max_concurrent_calls == 1
    assert http_default._call_semaphore is None  # today's _rpc_lock path

    stdio = MCPServerTask("b")
    stdio._configure_call_limits({"command": "npx", "max_concurrent_calls": 6})
    assert stdio._max_concurrent_calls == 1
    assert stdio._call_semaphore is None

    http = MCPServerTask("c")
    http._configure_call_limits({"url": MCP_URL, "max_concurrent_calls": 4})
    assert http._max_concurrent_calls == 4
    assert http._max_concurrent_calls_per_user == 4  # defaults to the server cap

    capped = MCPServerTask("d")
    capped._configure_call_limits({
        "url": MCP_URL, "max_concurrent_calls": 2, "max_concurrent_calls_per_user": 5,
    })
    assert capped._max_concurrent_calls_per_user == 2  # never above the server cap


def test_the_per_user_key_is_the_subject_not_the_token():
    """Open WebUI mints a new token per request; the cap must follow the user."""
    from tools.mcp_tool import _end_user_slot_key

    alice = _end_user_slot_key(ALICE_1)
    assert alice == _end_user_slot_key(ALICE_4)
    assert alice != _end_user_slot_key(BOB_1)
    assert "alice" not in alice and ALICE_1 not in alice
    assert _end_user_slot_key(None) == "anon"
    assert _end_user_slot_key("not-a-jwt").startswith("tok:")


def test_slots_come_back_after_an_exception_or_a_cancel_and_idle_users_are_forgotten():
    """Review Focus: a user pressing Stop mid-batch (cancel) or a tool blowing up
    must not leak a slot, or that user is throttled for the life of the process.
    And one semaphore per user ever seen must not accumulate forever."""
    import asyncio

    from tools.mcp_tool import MCPServerTask, _admit_call

    server = MCPServerTask("x")
    server._configure_call_limits({"url": MCP_URL, **PARALLEL})

    async def _go():
        with pytest.raises(RuntimeError):
            async with _admit_call(server, "sub:a"):
                raise RuntimeError("tool blew up")

        entered = asyncio.Event()
        release = asyncio.Event()

        async def _hold():
            async with _admit_call(server, "sub:a"):
                entered.set()
                await release.wait()

        # Fill the user's three slots, queue a fourth, then cancel the fourth
        # while it waits and one holder while it runs.
        holders = [asyncio.create_task(_hold()) for _ in range(3)]
        await entered.wait()
        await asyncio.sleep(0.05)
        waiter = asyncio.create_task(_hold())
        await asyncio.sleep(0.05)
        assert server._user_call_holders["sub:a"] == 4
        waiter.cancel()
        holders[0].cancel()
        for t in (waiter, holders[0]):
            with pytest.raises(asyncio.CancelledError):
                await t
        release.set()
        await asyncio.gather(*holders[1:])

        # All three per-user slots are usable again.
        async def _one():
            async with _admit_call(server, "sub:a"):
                await asyncio.sleep(0.01)
        await asyncio.wait_for(asyncio.gather(*[_one() for _ in range(3)]), 2)

    asyncio.run(_go())

    assert server._user_call_semaphores == {}
    assert server._user_call_holders == {}


def test_nine_calls_from_one_user_all_finish_three_at_a_time(ragnarok_factory):
    """Review Focus: three subagents each fanning out three lookups share one
    user's three slots. They queue; none deadlocks or is refused."""
    handler, wire, server = ragnarok_factory(limits=PARALLEL, delay=0.3)
    tokens = [ALICE_1, ALICE_2, ALICE_3] * 3

    with ThreadPoolExecutor(max_workers=9) as pool:
        futures = [
            pool.submit(_as_user(handler, tok, CHAT_A, f"r-{i}", {"q": i}))
            for i, tok in enumerate(tokens)
        ]
        results = [json.loads(f.result(timeout=60)) for f in futures]

    assert results == [{"result": "ok"}] * 9
    assert wire.max_in_flight == 3
    assert server._user_call_semaphores == {}


def test_reconfiguring_with_the_same_limits_keeps_the_live_slots():
    """Review Focus: run() re-reads config on every (re)connect. Swapping the
    semaphores under in-flight calls would let new calls exceed the caps."""
    import asyncio

    from tools.mcp_tool import MCPServerTask

    server = MCPServerTask("x")
    config = {"url": MCP_URL, **PARALLEL}
    server._configure_call_limits(config)
    server_sem = server._call_semaphore
    live = asyncio.Semaphore(3)
    server._user_call_semaphores["sub:a"] = live
    server._user_call_holders["sub:a"] = 1

    server._configure_call_limits(config)

    assert server._call_semaphore is server_sem
    assert server._user_call_semaphores["sub:a"] is live
    assert server._user_call_holders["sub:a"] == 1

    server._configure_call_limits({"url": MCP_URL, "max_concurrent_calls": 2})
    assert server._call_semaphore is not server_sem  # a real change rebuilds them
