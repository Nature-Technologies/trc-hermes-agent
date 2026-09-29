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
import os
import sys
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

# Terminal parameters that sandbox scripts must not supply (blocking at the
# dispatch layer prevents a script from spawning persistent background processes
# or attaching a pty that outlives the run).
_TERMINAL_BLOCKED_PARAMS: frozenset = frozenset(
    {"background", "pty", "notify_on_complete", "watch_patterns"}
)

# --------------------------------------------------------------------------
# Bridged ContextVar — marks tool calls a sandbox script makes vs. model-direct
# --------------------------------------------------------------------------
#
# Why a ContextVar: the MCP loop runs on a dedicated event-loop thread and
# copies THAT thread's context when spawning coroutines via
# run_coroutine_threadsafe, NOT the agent thread's.  So a raw ContextVar
# value set on the agent thread would NOT be visible to the MCP coroutine.
#
# The solution mirrors what the identity / chat / request headers do:
#   1. _serve_bridged_call (below) sets _BRIDGED to True for the duration of
#      its dispatch — on the agent thread.
#   2. _make_tool_handler (mcp_tool.py) reads _BRIDGED.get() on the agent
#      thread in the same breath as identity / chat / request, and arms
#      server._end_user_bridged as a PLAIN ATTRIBUTE.
#   3. The outbound HTTP-request hook (_stamp_end_user_identity, mcp_tool.py)
#      reads server._end_user_bridged on the MCP loop and stamps (or removes)
#      X-Hermes-Bridged: 1.  _rpc_lock serialises calls so at most one value
#      is ever armed, and the finally block clears it unconditionally.
#
# A model-direct call never enters _serve_bridged_call, so _BRIDGED stays at
# its default False and the header is actively REMOVED — a stale True can
# never leak to a subsequent call.

_BRIDGED: ContextVar[bool] = ContextVar("hermes_bridged", default=False)


def _bridged_now() -> bool:
    """True iff the running code is inside a bridged dispatch (_serve_bridged_call)."""
    return _BRIDGED.get()


def _dispatch_one(
    tool_name: str,
    tool_args: dict,
    *,
    task_id: Optional[str],
) -> str:
    """Dispatch one sandboxed tool call to handle_function_call.

    Kept as a named module-level function (rather than an inline closure) so
    tests can patch it — e.g. to assert that _BRIDGED is True during dispatch
    without needing a live model_tools installation.  The lazy import keeps
    model_tools out of the module-load critical path.
    """
    from model_tools import handle_function_call

    call_id = notify_bridged_start(tool_name, tool_args)
    _real_stdout, _real_stderr = sys.stdout, sys.stderr
    devnull = open(os.devnull, "w", encoding="utf-8")  # noqa: WPS515
    try:
        sys.stdout = devnull
        sys.stderr = devnull
        result = handle_function_call(tool_name, tool_args, task_id=task_id)
    except Exception as exc:
        logger.error("Tool call failed in sandbox: %s", exc, exc_info=True)
        result = json.dumps({"error": str(exc)})
    finally:
        sys.stdout, sys.stderr = _real_stdout, _real_stderr
        devnull.close()
    if not isinstance(result, str):
        result = json.dumps(result, ensure_ascii=False, default=str)
    notify_bridged_complete(call_id, tool_name, tool_args, result)
    return result


def _serve_bridged_call(
    tool_name: str,
    tool_args: Any,
    *,
    allowed_tools: frozenset,
    tool_call_counter: list,
    max_tool_calls: int,
    task_id: Optional[str],
) -> str:
    """Serve one tool call a sandbox script made.

    The ONE place the bridged-call rules live, shared by the UDS loop, the
    file-RPC loop and the sidecar transport: allowlist, call cap, argument
    stripping (prepare_bridged_args), per-turn cache, status frames.

    Sets _BRIDGED for exactly the duration of this dispatch so the backend
    receives X-Hermes-Bridged: 1 on every MCP request the call makes — which
    routes returned tokens to PENDING rather than DELIVERED (spec §4.4, L4).
    The ContextVar is reset in a finally so a cached result or an exception
    never leaves it armed for the next call.
    """
    if tool_name not in allowed_tools:
        available = ", ".join(sorted(allowed_tools))
        return json.dumps({
            "error": (
                f"Tool '{tool_name}' is not available in execute_code. "
                f"Available: {available}"
            )
        })
    if tool_call_counter[0] >= max_tool_calls:
        return json.dumps({
            "error": (
                f"Tool call limit reached ({max_tool_calls}). "
                "No more tool calls allowed in this execution."
            )
        })
    if not isinstance(tool_args, dict):
        tool_args = {}
    if tool_name == "terminal":
        for param in _TERMINAL_BLOCKED_PARAMS:
            tool_args.pop(param, None)
    tool_args = prepare_bridged_args(tool_name, tool_args)

    tok = _BRIDGED.set(True)
    try:
        result = cached_bridged_call(
            tool_name,
            tool_args,
            lambda: _dispatch_one(tool_name, tool_args, task_id=task_id),
        )
    finally:
        _BRIDGED.reset(tok)
    tool_call_counter[0] += 1
    return result


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
        # One retry on a TRANSPORT failure (an exception raised by the handler —
        # a dropped connection, a timeout, a not-yet-connected session).  A
        # well-formed error reply from the backend (detected by _record_computed_failure)
        # is NOT retried: the backend already parsed the call and said no, so
        # repeating it would only add latency before the same refusal.
        raw: Optional[str] = None
        for attempt in range(1, 3):
            try:
                raw = handler({"texts": [stdout_text]})
                break
            except Exception as exc:
                logger.warning(
                    "record_computed transport failure attempt %d/2: %s",
                    attempt,
                    type(exc).__name__,
                )
                if attempt >= 2:
                    return  # never raise — a failure only costs the computed tier
        if raw is None:
            return
        failed, reason = _record_computed_failure(raw)
        if failed:
            logger.warning(
                "record_computed failed: %s (%d chars sent)",
                reason or "the call returned an error",
                len(stdout_text),
            )
            return
        logger.info("record_computed: sent (%d chars) -> %s", len(stdout_text), str(raw)[:80])
    except Exception as exc:
        logger.warning("record_computed failed: %s", type(exc).__name__)


def _record_computed_failure(raw: Any) -> tuple:
    """(failed, reason) for the handler's reply to `record_computed`.

    The handler returns error JSON rather than raising, and wraps the backend's own
    `{"status": "error", "error": ...}` as `{"result": <its JSON text>,
    "structuredContent": {...}}`, so a top-level check alone logged the backend's
    fail-closed refusals ("no verified caller identity") as sent. `reason` is the
    backend's error string, a fixed message and never script output; it stays empty
    for a top-level error, whose text (a validation error, say) can echo the arguments.
    """
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return False, ""  # not JSON: treat as success
    if not isinstance(parsed, dict):
        return False, ""
    if "error" in parsed:
        return True, ""
    for body in (parsed.get("structuredContent"), parsed.get("result")):
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except (json.JSONDecodeError, TypeError):
                continue
        if isinstance(body, dict) and body.get("status") == "error":
            return True, str(body.get("error") or "")[:200]
    return False, ""
