"""The SSE writer turns the status machine's decisions into stock Open WebUI frames.

Copies the queue-and-mocked-StreamResponse pattern of test_sse_agent_cancel.py.
Spec: trc-backend docs/superpowers/specs/2026-09-16-interim-status-while-answering-design.md S6.
"""

from __future__ import annotations

import asyncio
import json
import queue
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.platforms.turn_status import (
    COMPOSING_LINE,
    DONE_LINE,
    OPENERS,
    READING_LINE,
    TurnStatus,
    status_payload,
)

QUERY = "mcp__ragnarok__query"


def _adapter():
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    return APIServerAdapter(PlatformConfig(enabled=True, token="test-key"))


def _request():
    req = MagicMock()
    req.headers = {}
    return req


async def _write(stream_q, agent_task, turn_status):
    from aiohttp import web

    adapter = _adapter()
    response = AsyncMock(spec=web.StreamResponse)
    response.write = AsyncMock()
    response.prepare = AsyncMock()
    with patch("gateway.platforms.api_server.web.StreamResponse", return_value=response):
        await adapter._write_sse_chat_completion(
            _request(), "chatcmpl-1", "hermes", 1700000000, stream_q, agent_task,
            turn_status=turn_status,
        )
    frames = []
    for call in response.write.call_args_list:
        raw = call.args[0].decode()
        for block in raw.split("\n\n"):
            block = block.strip()
            if block.startswith("data: ") and block != "data: [DONE]":
                frames.append(json.loads(block[6:]))
            elif block.startswith("event:"):
                frames.append({"_event": block})
            elif block == "data: [DONE]":
                frames.append({"_done": True})
    return frames


def _statuses(frames):
    return [f["event"]["data"] for f in frames if "event" in f]


def _done_agent(text="The answer"):
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(({"final_response": text}, {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}))
    return fut


def test_status_items_become_chat_completion_chunks_with_an_event_and_no_choices():
    async def run():
        q = queue.Queue()
        q.put(("__status__", status_payload(READING_LINE)))
        q.put("The answer")
        q.put(None)
        return await _write(q, _done_agent(), None)

    frames = asyncio.run(run())
    status = [f for f in frames if "event" in f]
    assert len(status) == 1
    assert status[0]["choices"] == []
    assert status[0]["object"] == "chat.completion.chunk"
    assert status[0]["id"] == "chatcmpl-1" and status[0]["model"] == "hermes"
    assert status[0]["event"] == {"type": "status", "data": status_payload(READING_LINE)}


def test_a_full_turn_writes_the_timeline_in_order_and_the_trace_before_the_finish():
    async def run():
        ts = TurnStatus(clock=lambda: 0.0)
        q = queue.Queue()
        for p in ts.on_request():
            q.put(("__status__", p))
        q.put(("__tool_progress__", {"tool": QUERY, "toolCallId": "c1", "status": "running",
                                     "emoji": "", "label": QUERY}))
        q.put(("__hints__", ["Looking through statements\u2026"]))
        q.put(("__tool_progress__", {"tool": QUERY, "toolCallId": "c1", "status": "completed"}))
        q.put("The ")
        q.put("answer")
        q.put(None)
        return await _write(q, _done_agent(), ts)

    frames = asyncio.run(run())
    assert [s["description"] for s in _statuses(frames)] == [
        READING_LINE, OPENERS["query"], COMPOSING_LINE, DONE_LINE,
    ]
    assert _statuses(frames)[-1]["done"] is True
    # The hermes.tool.progress events are still written, unchanged.
    assert sum(1 for f in frames if "_event" in f) == 2
    # Order: the trace frame is written as the first content item arrives -- before that
    # content chunk, since `on_content` runs first -- and before the finish chunk.
    kinds = []
    for f in frames:
        if "event" in f:
            kinds.append(f["event"]["data"]["description"])
        elif f.get("choices") and f["choices"][0].get("finish_reason"):
            kinds.append("<finish>")
        elif f.get("choices") and "content" in f["choices"][0].get("delta", {}):
            kinds.append("<content>")
    assert kinds.index(DONE_LINE) < kinds.index("<finish>")
    assert kinds.index(COMPOSING_LINE) < kinds.index("<content>")


def test_a_greeting_ends_hidden_at_the_finish():
    async def run():
        ts = TurnStatus(clock=lambda: 0.0)
        q = queue.Queue()
        for p in ts.on_request():
            q.put(("__status__", p))
        q.put("Hello!")
        q.put(None)
        return await _write(q, _done_agent("Hello!"), ts)

    statuses = _statuses(asyncio.run(run()))
    assert [s["description"] for s in statuses] == [READING_LINE, ""]
    assert statuses[-1] == status_payload("", done=True, hidden=True)


def test_without_a_machine_the_stream_is_exactly_as_before():
    async def run():
        q = queue.Queue()
        q.put(("__tool_progress__", {"tool": QUERY, "toolCallId": "c1", "status": "running",
                                     "emoji": "", "label": QUERY}))
        q.put("The answer")
        q.put(None)
        return await _write(q, _done_agent(), None)

    frames = asyncio.run(run())
    assert _statuses(frames) == []
    assert sum(1 for f in frames if "_event" in f) == 1


def test_an_idle_tick_rotates_a_hint_while_the_tool_runs():
    async def run():
        ts = TurnStatus(clock=lambda: 0.0, cadence_seconds=0.0)  # cadence always satisfied
        q = queue.Queue()
        for p in ts.on_request():
            q.put(("__status__", p))
        q.put(("__tool_progress__", {"tool": QUERY, "toolCallId": "c1", "status": "running",
                                     "emoji": "", "label": QUERY}))
        q.put(("__hints__", ["Looking through statements\u2026", "Checking the agreement\u2026"]))

        async def slow_agent():
            await asyncio.sleep(1.3)  # two idle ticks of 0.5 s go by
            q.put("The answer")
            q.put(None)
            return {"final_response": "The answer"}, {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}

        return await _write(q, asyncio.ensure_future(slow_agent()), ts)

    descriptions = [s["description"] for s in _statuses(asyncio.run(run()))]
    assert descriptions[:2] == [READING_LINE, OPENERS["query"]]
    assert "Looking through statements\u2026" in descriptions
    assert descriptions[-1] == DONE_LINE
