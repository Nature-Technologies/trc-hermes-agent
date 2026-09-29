"""Client for the TRC sandbox runner (deploy/trc/sandbox/runner.py) — the wire protocol
of trc-backend spec 2026-09-28 §4.5. One script per connection; the script's tool calls
arrive on the same connection and are answered by `on_call`.

Stdlib only, with no Hermes imports, so the sandbox's adversarial check can import it
on its own."""

from __future__ import annotations

import json
import logging
import socket
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

MAX_LINE = 4_000_000
# How often a wait for the runner's next frame looks at the deadline and `should_stop`.
TICK_SECONDS = 0.2
_OVERALL_DEADLINE = {"status": "timeout", "error": "overall deadline"}
_INTERRUPTED = {"status": "interrupted"}


class _Ended(Exception):
    """The run is over on this side; `result` is what run_in_sidecar returns."""

    def __init__(self, result: dict):
        super().__init__(result.get("status"))
        self.result = result


def _stopped(should_stop: Optional[Callable[[], bool]]) -> bool:
    if should_stop is None:
        return False
    try:
        return bool(should_stop())
    except Exception:
        return True  # fail closed: a stop check that cannot answer is a stop


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


class _Connection:
    """One run's connection to the runner, bounded by one overall deadline.

    Frames are read with plain recv() into a buffer of our own, never through a
    makefile() reader. A socket.timeout leaves a raw socket usable, but it leaves the
    SocketIO under a makefile() reader refusing every later read ("cannot read from
    timed out object"). That is what lets the wait for a frame tick every TICK_SECONDS
    to look at the deadline and at `should_stop` without losing a byte. (select() is
    not used: it cannot take a descriptor past FD_SETSIZE, which a busy gateway can
    reach.)"""

    def __init__(self, sock, deadline: float, should_stop):
        self._sock = sock
        self._deadline = deadline
        self._should_stop = should_stop
        self._buf = bytearray()
        self._scanned = 0  # bytes of _buf already searched for a newline

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise _Ended(dict(_OVERALL_DEADLINE))
        return remaining

    def check_stop(self) -> None:
        if _stopped(self._should_stop):
            raise _Ended(dict(_INTERRUPTED))

    def send(self, obj) -> None:
        # sendall's timeout bounds the whole send, so give it the rest of the deadline.
        self._sock.settimeout(max(self._remaining(), 0.1))
        # ASCII-escaped like the runner's own frames, so no string content — a lone
        # surrogate json.loads let through, say — can make the encoding fail.
        self._sock.sendall(json.dumps(obj, ensure_ascii=True).encode("ascii") + b"\n")

    def frame(self):
        """The runner's next frame, parsed. Raises _Ended at the deadline, on a stop,
        or when the runner closes the connection."""
        while True:
            end = self._buf.find(b"\n", self._scanned)
            if end >= 0:
                if end > MAX_LINE:
                    raise ValueError("runner frame too long")
                line = bytes(self._buf[:end])
                del self._buf[:end + 1]
                self._scanned = 0
                return json.loads(line.decode("utf-8"))
            self._scanned = len(self._buf)
            if len(self._buf) > MAX_LINE:
                raise ValueError("runner frame too long")
            remaining = self._remaining()
            self.check_stop()
            self._sock.settimeout(min(TICK_SECONDS, remaining))
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise _Ended({"status": "unavailable", "error": "runner closed the connection"})
            self._buf += chunk


def run_in_sidecar(
    socket_path: str,
    code: str,
    stubs: str,
    limits: dict,
    on_call: Callable[[str, dict], str],
    *,
    connect_timeout: float = 5.0,
    overall_timeout: float = 300.0,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict:
    """Run `code` in the sidecar. Returns the runner's `done` message,
    `{"status": "timeout", "error": "overall deadline"}` once `overall_timeout` has
    passed, `{"status": "interrupted"}` once `should_stop` answers True (or raises), or
    `{"status": "unavailable", "error": <exception class>}` — never raises.

    `should_stop` is asked before the run starts, at least every TICK_SECONDS while
    waiting for the runner, and before each tool call is served; a stop closes the
    connection, which the runner notices and ends the script. `on_call` runs on the
    calling thread."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as exc:
        return {"status": "unavailable", "error": type(exc).__name__}
    try:
        try:
            sock.settimeout(connect_timeout)
            sock.connect(socket_path)
        except OSError as exc:
            return {"status": "unavailable", "error": type(exc).__name__}
        # A deadline, not a per-read timeout: a runner that kept sending frames would
        # otherwise hold this thread for as long as it liked.
        conn = _Connection(sock, time.monotonic() + overall_timeout, should_stop)
        conn.check_stop()
        conn.send({"op": "run", "code": code, "stubs": stubs, "limits": limits})
        while True:
            msg = conn.frame()
            if not isinstance(msg, dict):
                return {"status": "unavailable", "error": "malformed runner frame"}
            if msg.get("op") == "call":
                conn.check_stop()  # not one more call once the turn has been stopped
                result = _answer(on_call, msg)
                conn.send({"op": "result", "id": msg.get("id"), "result": result})
            elif msg.get("op") == "done":
                return msg
    except _Ended as ended:
        return ended.result
    except socket.timeout:
        return dict(_OVERALL_DEADLINE)  # a send that outlasted the deadline
    except (OSError, ValueError, RecursionError) as exc:
        return {"status": "unavailable", "error": type(exc).__name__}
    finally:
        sock.close()
