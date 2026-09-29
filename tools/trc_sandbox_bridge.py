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
    from gateway.session_context import get_end_user_chat_id, get_end_user_request_id

    chat, request = get_end_user_chat_id(), get_end_user_request_id()
    return (chat, request) if chat and request else None


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
