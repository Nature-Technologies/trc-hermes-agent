"""The interim status line for an Open WebUI turn — a pure state machine and its text.

While a streaming ``/v1/chat/completions`` turn runs, the API server emits a status line
above the answer: "Reading your question…", the tool's own line when a call starts, a
few activity lines relevant to the question, "Preparing your answer…", and a one-line
trace once the answer streams. Stock Open WebUI renders those — its middleware forwards
an ``event`` object found inside a chat-completion chunk straight to the browser — so
the frontend is untouched.

Everything that decides WHAT to show lives here, with no I/O and the clock injected, so
the timeline is testable to the second. The SSE writer in ``api_server.py`` feeds it
(request receipt, the tool-progress queue items, the 0.5 s idle tick, first content,
stream end) and writes whatever it returns. ``sanitize_lines`` is the only gate between
an auxiliary model's output and the screen on this side; the Open WebUI filter in
``trc-backend`` enforces the same ``STATUS_TEXT_RE`` again at the boundary.

Design and limits: trc-backend
``docs/superpowers/specs/2026-09-16-interim-status-while-answering-design.md``.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional

# Letters, spaces, commas, periods, apostrophes and hyphens; starts with a letter; 7 to
# 89 characters, plus an optional trailing ellipsis (U+2026) — so a token, a marker, a
# digit, a link, an address or a bracket cannot match. MIRRORED, character for
# character, in trc-backend/integrations/openwebui/filter.py (`_STATUS_TEXT_RE`): change
# both together.
STATUS_TEXT_RE = re.compile("^[A-Za-z][A-Za-z ,.\x27’’\\-]{6,88}…?$")

# Hermes registers MCP tools as ``mcp__<server>__<tool>``; these four are the ones that
# search TRC's records. Anything else — ``ingest_document`` included — is "other".
DATA_TOOL_PREFIX = "mcp__ragnarok__"
DATA_TOOLS = frozenset({"query", "list_entities", "generate_report", "find_relationships"})

READING_LINE = "Reading your question…"
COMPOSING_LINE = "Preparing your answer…"
DONE_LINE = "Searched TRC records"
OTHER_TOOL_LINE = "Working on it…"

OPENERS = {
    "query": "Searching TRC records…",
    "list_entities": "Gathering the full list…",
    "generate_report": "Reading the source documents…",
    "find_relationships": "Checking relationship records…",
}

# The floor: what rotates when the hints side-call produced nothing usable.
CANNED = {
    "query": ("Reading the most relevant documents…", "Checking the details…"),
    "generate_report": ("Composing the document…",),
    "list_entities": (),
    "find_relationships": (),
}


def data_tool_of(tool_name: str) -> Optional[str]:
    """``"query"`` for ``"mcp__ragnarok__query"``; None for anything that is not a data tool."""
    if not tool_name or not tool_name.startswith(DATA_TOOL_PREFIX):
        return None
    suffix = tool_name[len(DATA_TOOL_PREFIX):]
    return suffix if suffix in DATA_TOOLS else None


def status_payload(description: str, *, done: bool = False, hidden: bool = False) -> dict:
    """The ``data`` of a status event — exactly the three keys the filter admits."""
    return {"description": description, "done": bool(done), "hidden": bool(hidden)}


def status_chunk(completion_id: str, model: str, created: int, payload: dict) -> dict:
    """A chat-completion chunk carrying one status event and no content.

    ``choices`` is empty on purpose: Open WebUI forwards the ``event`` and then finds
    nothing else to do with the frame; any other OpenAI-compatible client ignores the
    unknown key.
    """
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [],
        "event": {"type": "status", "data": dict(payload)},
    }


_BULLET_RE = re.compile(r"^(?:[-*•]\s*|\d+[.)]\s*)")
_EDGE_QUOTES = "\"'“”‘’`"
_FENCE_RE = re.compile(r"^```[A-Za-z]*\s*|\s*```$")


def _candidate_lines(raw) -> list[str]:
    """A JSON array of strings if that is what the model returned, else its lines."""
    text = _FENCE_RE.sub("", str(raw or "").strip()).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, str)]
    return text.splitlines()


def _normalize_line(candidate: str) -> Optional[str]:
    line = " ".join(candidate.split())
    line = _BULLET_RE.sub("", line).strip().strip(_EDGE_QUOTES).strip()
    line = line.rstrip(".… ")
    if not STATUS_TEXT_RE.match(line):
        return None
    return line


def sanitize_lines(raw, max_lines: int) -> list[str]:
    """Turn the hints model's reply into at most ``max_lines`` safe status lines.

    Lenient on shape (a JSON array, a fenced array, or plain lines with bullets and
    quotes), strict on content: a line survives only if it matches ``STATUS_TEXT_RE``
    before the ellipsis is added — so a token, a digit, a date, a link, an address or a
    bracket cannot. Survivors are deduplicated case-insensitively and get a trailing
    ellipsis so every rotating line reads the same way. Zero survivors is a legitimate
    answer: the caller falls back to the canned lines.
    """
    out: list[str] = []
    seen: set[str] = set()
    for candidate in _candidate_lines(raw):
        line = _normalize_line(candidate)
        if line is None:
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(line + "…")
        if len(out) >= max(0, int(max_lines)):
            break
    return out


class TurnStatus:
    """The status timeline of one turn.

    States: ``reading`` → ``searching`` → ``composing`` → ``done`` | ``hidden``. Every
    ``on_*`` method returns the list of status payloads to write (zero or one). The
    terminal frame — ``done`` with the trace line if a data tool ran this turn, else
    ``hidden`` — is emitted once, on the first content in ``composing`` or at stream end,
    whichever comes first.
    """

    def __init__(
        self,
        clock: Callable[[], float],
        cadence_seconds: float = 6.0,
        max_lines: int = 4,
    ) -> None:
        self.clock = clock
        self.cadence_seconds = float(cadence_seconds)
        self.max_lines = max(0, int(max_lines))
        self.state = "reading"
        self._data_tool_ran = False
        self._current_tool: Optional[str] = None  # data-tool suffix, or None for "other"
        self._hints: Optional[list[str]] = None  # None = never arrived (or nothing usable)
        self._hint_idx = 0
        self._canned_idx = 0
        self._last_emit = self.clock()

    # -- inputs ----------------------------------------------------------------------

    def on_request(self) -> list[dict]:
        return self._emit(status_payload(READING_LINE))

    def on_hints(self, lines: list[str]) -> list[dict]:
        kept = [line for line in (lines or []) if isinstance(line, str) and line]
        self._hints = kept[: self.max_lines] if kept else None
        return []

    def on_tool_start(self, tool_name: str) -> list[dict]:
        if self.state in ("done", "hidden"):
            return []
        self.state = "searching"
        self._canned_idx = 0
        tool = data_tool_of(tool_name)
        self._current_tool = tool
        if tool is None:
            return self._emit(status_payload(OTHER_TOOL_LINE))
        self._data_tool_ran = True
        return self._emit(status_payload(OPENERS[tool]))

    def on_tick(self) -> list[dict]:
        if self.state != "searching" or self._current_tool is None:
            return []
        if self.clock() - self._last_emit < self.cadence_seconds:
            return []
        if self._hints is not None:
            if self._hint_idx < len(self._hints):
                line = self._hints[self._hint_idx]
                self._hint_idx += 1
                return self._emit(status_payload(line))
            return []
        canned = CANNED.get(self._current_tool, ())
        if self._canned_idx < len(canned):
            line = canned[self._canned_idx]
            self._canned_idx += 1
            return self._emit(status_payload(line))
        return []

    def on_tool_complete(self, tool_name: str) -> list[dict]:
        if self.state in ("done", "hidden"):
            return []
        self.state = "composing"
        return self._emit(status_payload(COMPOSING_LINE))

    def on_content(self) -> list[dict]:
        # In `reading` the model may be narrating before a tool call or answering a
        # greeting outright; both look the same here, so only `composing` treats content
        # as the answer. In `searching` a tool is running and content cannot arrive.
        if self.state != "composing":
            return []
        return self._terminal()

    def on_end(self) -> list[dict]:
        if self.state in ("done", "hidden"):
            return []
        return self._terminal()

    # -- internals -------------------------------------------------------------------

    def _terminal(self) -> list[dict]:
        if self._data_tool_ran:
            self.state = "done"
            return self._emit(status_payload(DONE_LINE, done=True))
        self.state = "hidden"
        return self._emit(status_payload("", done=True, hidden=True))

    def _emit(self, payload: dict) -> list[dict]:
        self._last_emit = self.clock()
        return [payload]
