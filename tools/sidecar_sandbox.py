"""Client for the TRC sandbox runner (deploy/trc/sandbox/runner.py) — the wire protocol
of trc-backend spec 2026-09-28 §4.5. One script per connection; the script's tool calls
arrive on the same connection and are answered by `on_call`."""

from __future__ import annotations

import json
import logging
import socket
import time
from typing import Callable

logger = logging.getLogger(__name__)

MAX_LINE = 4_000_000
_OVERALL_DEADLINE = {"status": "timeout", "error": "overall deadline"}


def _send(sock, obj) -> None:
    # ASCII-escaped like the runner's own frames, so no string content — a lone
    # surrogate json.loads let through, say — can make the encoding fail.
    sock.sendall(json.dumps(obj, ensure_ascii=True).encode("ascii") + b"\n")


def _recv(fp):
    line = fp.readline(MAX_LINE)
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


def _answer(on_call, msg) -> str:
    """`on_call`'s reply to one call frame — always a JSON string, since the runner hands
    a one-line result to the script verbatim and the stub json.loads it."""
    args = msg.get("args") if isinstance(msg.get("args"), dict) else {}
    try:
        return on_call(str(msg.get("tool", "")), args)
    except Exception as exc:
        # The class name only: the message could carry tool data.
        logger.warning("sandbox tool call failed: %s", type(exc).__name__)
        return json.dumps({"error": f"tool call failed: {type(exc).__name__}"})


def run_in_sidecar(
    socket_path: str,
    code: str,
    stubs: str,
    limits: dict,
    on_call: Callable[[str, dict], str],
    *,
    connect_timeout: float = 5.0,
    overall_timeout: float = 300.0,
) -> dict:
    """Run `code` in the sidecar. Returns the runner's `done` message,
    `{"status": "timeout", "error": "overall deadline"}` once `overall_timeout` has
    passed, or `{"status": "unavailable", "error": <exception class>}` — never raises."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as exc:
        return {"status": "unavailable", "error": type(exc).__name__}
    rfile = None
    try:
        try:
            sock.settimeout(connect_timeout)
            sock.connect(socket_path)
        except OSError as exc:
            return {"status": "unavailable", "error": type(exc).__name__}
        # A deadline, not a per-read timeout: a runner that kept sending frames would
        # otherwise hold this thread for as long as it liked.
        deadline = time.monotonic() + overall_timeout
        sock.settimeout(max(deadline - time.monotonic(), 0.1))
        rfile = sock.makefile("rb")
        _send(sock, {"op": "run", "code": code, "stubs": stubs, "limits": limits})
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return dict(_OVERALL_DEADLINE)
            sock.settimeout(max(remaining, 0.1))
            msg = _recv(rfile)
            if msg is None:
                return {"status": "unavailable", "error": "runner closed the connection"}
            if not isinstance(msg, dict):
                return {"status": "unavailable", "error": "malformed runner frame"}
            if msg.get("op") == "call":
                _send(sock, {"op": "result", "id": msg.get("id"), "result": _answer(on_call, msg)})
            elif msg.get("op") == "done":
                return msg
    except socket.timeout:
        return dict(_OVERALL_DEADLINE)
    except (OSError, ValueError, RecursionError) as exc:
        return {"status": "unavailable", "error": type(exc).__name__}
    finally:
        if rfile is not None:
            rfile.close()
        sock.close()
