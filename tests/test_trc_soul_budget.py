"""The budget SOUL v2 was written to (trc-orchestrator spec 2026-10-14 answer-first §4,
§4.1; contract C6).

The prompt is sent on every model call of every turn, so its length is paid on each one,
and its wording sets the voice the answers take. The limits:

- at most 6,500 characters (1,500 more are reserved for learned lessons, spec §8);
- "never" kept for the security and token rules, at most ten times (v1 had 47);
- none of the self-doubt phrases the eval's hedge lint counts in answers (spec §7);
- routes to the P1 tools and names none that is off the chat wire;
- a shape the backend's reviewed copy can hold byte for byte: no line that would close
  its ```text block, and exactly one trailing newline, which is what
  scripts/check_soul_matches_backend.py extracts from that block.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SOUL = _ROOT / "deploy" / "trc" / "SOUL.md"
_CHECK = _ROOT / "scripts" / "check_soul_matches_backend.py"

_HEDGES = (
    "honestly",
    "I want to be straight",
    "unfortunately",
    "it seems",
    "I wasn't able",
    "I couldn't",
    "try again",
)


def _soul() -> str:
    return _SOUL.read_text(encoding="utf-8")


def test_the_prompt_fits_its_budget():
    assert len(_soul()) <= 6500, len(_soul())


def test_never_is_kept_for_the_rules_that_need_it():
    hits = re.findall(r"\bnever\b", _soul(), flags=re.IGNORECASE)
    assert len(hits) <= 10, len(hits)


@pytest.mark.parametrize("phrase", _HEDGES)
def test_the_prompt_models_no_hedge(phrase):
    text = _soul().replace("’", "'").lower()
    assert phrase.lower() not in text


def test_names_and_rankings_route_to_their_one_call_tools():
    soul = _soul()
    assert "`resolve_entity`" in soul
    assert "`rank_and_count`" in soul


@pytest.mark.parametrize("tool", ["list_entities", "meeting_briefing", "skill_view"])
def test_the_prompt_names_no_tool_that_is_off_the_chat_wire(tool):
    assert tool not in _soul()


def test_no_line_would_close_the_backend_text_block_early():
    assert "```" not in _soul().splitlines()


def test_the_file_ends_with_exactly_one_newline():
    soul = _soul()
    assert soul.endswith("\n")
    assert not soul.endswith("\n\n")


def test_the_drift_check_reads_this_file_back_out_of_a_backend_block(tmp_path):
    """Through the drift check's own extraction: the backend doc holds the prompt as
    ```text, the prompt, then ``` on the next line."""
    spec = importlib.util.spec_from_file_location("check_soul_matches_backend", _CHECK)
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)

    soul = _soul()
    doc = tmp_path / "integrations" / "openwebui" / "SYSTEM_PROMPT.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "# TRC system prompt\n\nReviewed here.\n\n```text\n" + soul + "```\n\nNotes.\n",
        encoding="utf-8",
    )
    assert check.backend_prompt(tmp_path) == soul
