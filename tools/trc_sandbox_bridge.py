"""TRC's rules for tool calls a sandboxed script makes through the gateway bridge.

trc-backend spec docs/superpowers/specs/2026-09-28-hermes-analysis-sandbox-design.md §4.3.
Kept out of code_execution_tool.py so the fork's diff to upstream files stays small:
that file only calls in here.

- Identity-shaped arguments never reach the backend from a script: identity comes from
  the gateway's verified headers (invariant 3), and with enforcement off a script could
  otherwise choose the mask namespace.
- `answer` is a SCRIPT-only argument: `query(answer=false)` returns rows without the
  composed answer. The model's own calls must keep relaying `answer`, so it is stripped
  from them (agent/tool_executor.py) and kept here.
- A per-turn cache means a rewritten script does not repeat slow calls; a new turn
  (a new Open WebUI message id) fetches fresh, so permissions are re-checked.
- Progress callbacks let each bridged call drive the interim status line.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from collections import OrderedDict
from contextvars import ContextVar
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

MCP_PREFIX = "mcp__"
_IDENTITY_ARGS = frozenset({"requesting_user", "session_id"})
SCRIPT_ONLY_ARGS = frozenset({"answer"})

_CACHE_MAX_ENTRIES = 128
_cache: "OrderedDict[tuple, str]" = OrderedDict()
_cache_lock = threading.Lock()

_PROGRESS: ContextVar[Optional[tuple]] = ContextVar("trc_bridged_progress", default=None)


def prepare_bridged_args(tool_name: str, args: dict) -> dict:
    """A script's arguments for an MCP tool, minus identity-shaped ones."""
    if not tool_name.startswith(MCP_PREFIX) or not isinstance(args, dict):
        return args
    return {k: v for k, v in args.items() if k not in _IDENTITY_ARGS}


def strip_script_only_args(tool_name: str, args: Any) -> Any:
    """The MODEL's own arguments for an MCP tool, minus script-only ones."""
    if not tool_name.startswith(MCP_PREFIX) or not isinstance(args, dict):
        return args
    return {k: v for k, v in args.items() if k not in SCRIPT_ONLY_ARGS}


def _turn_key() -> Optional[tuple]:
    from gateway.session_context import (
        get_end_user_chat_id,
        get_end_user_identity,
        get_end_user_request_id,
    )

    identity = get_end_user_identity()
    chat = get_end_user_chat_id()
    request = get_end_user_request_id()
    if not identity or not chat or not request:
        return None
    # Hash the identity so a raw JWT never sits in a cache key.
    identity_hash = hashlib.sha256(identity.encode()).hexdigest()[:32]
    return (identity_hash, chat, request)


def _is_cacheable(result: Any) -> bool:
    if not isinstance(result, str):
        return False
    try:
        parsed = json.loads(result)
    except ValueError:
        return True
    return not (isinstance(parsed, dict) and "error" in parsed)


def cached_bridged_call(tool_name: str, args: dict, call: Callable[[], str]) -> str:
    """`call()`, reused within one turn for the same MCP tool and arguments."""
    turn = _turn_key() if tool_name.startswith(MCP_PREFIX) else None
    if turn is None:
        return call()
    key = (turn, tool_name, json.dumps(args, sort_keys=True, default=str))
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    result = call()
    if _is_cacheable(result):
        with _cache_lock:
            _cache[key] = result
            while len(_cache) > _CACHE_MAX_ENTRIES:
                _cache.popitem(last=False)
    return result


def discard_turn_cache() -> int:
    """Remove every cache entry belonging to the CURRENT turn.

    Called by the API server when a request ends (Task 4 wires it), so a
    turn's results do not outlive it; the LRU bound stays as a backstop.
    Returns the number of entries dropped. No-op (returns 0) when there is
    no current turn key.
    """
    turn = _turn_key()
    if turn is None:
        return 0
    with _cache_lock:
        to_drop = [k for k in _cache if k[0] == turn]
        for k in to_drop:
            del _cache[k]
    return len(to_drop)


def set_progress_callbacks(start: Optional[Callable], complete: Optional[Callable]):
    """Bind the request's tool start/complete callbacks for bridged calls."""
    return _PROGRESS.set((start, complete) if start or complete else None)


def reset_progress_callbacks(token) -> None:
    _PROGRESS.reset(token)


def notify_bridged_start(tool_name: str, args: dict) -> Optional[str]:
    callbacks = _PROGRESS.get()
    if not callbacks or not callbacks[0]:
        return None
    call_id = f"bridged-{uuid.uuid4().hex[:12]}"
    try:
        callbacks[0](call_id, tool_name, args)
    except Exception as exc:
        logger.debug("bridged start callback failed: %s", type(exc).__name__)
    return call_id


def notify_bridged_complete(call_id, tool_name: str, args: dict, result: Any) -> None:
    callbacks = _PROGRESS.get()
    if not call_id or not callbacks or not callbacks[1]:
        return
    try:
        callbacks[1](call_id, tool_name, args, result)
    except Exception as exc:
        logger.debug("bridged complete callback failed: %s", type(exc).__name__)


def record_computed(stdout_text: str, server_name: Optional[str]) -> None:
    """Send a run's printed output to the backend's `record_computed` (spec §4.4).

    The backend digests the figures in it into the turn's COMPUTED set, so `/unmask`
    gives them the softer "calculated" note. `record_computed` is not in the model's
    `tools.include`, so it is not in the registry: call it through the MCP client
    directly. Runs on the execute_code thread, whose context carries the end-user
    identity and chat id the handler forwards. Never raises: a failure only means
    those figures get the stronger "not quoted" warning — the safe direction.
    """
    if not server_name or not stdout_text or not stdout_text.strip():
        return
    try:
        from tools.mcp_tool import _make_tool_handler, _servers

        server = _servers.get(server_name)
        if server is None:
            logger.warning("record_computed: MCP server %r is not connected", server_name)
            return
        handler = _make_tool_handler(server_name, "record_computed", server.tool_timeout)
        raw = handler({"texts": [stdout_text]})
        logger.info("record_computed: sent (%d chars) -> %s", len(stdout_text), str(raw)[:80])
    except Exception as exc:
        logger.warning("record_computed failed: %s", type(exc).__name__)
