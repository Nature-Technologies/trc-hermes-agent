"""The TRC deploy's SOUL.md must survive Hermes' own context-file scanner.

`load_soul_md` passes the file through `tools/threat_patterns.py`, and ANY match replaces
the whole file with `[BLOCKED: ...]`. The model then runs on the stock Hermes identity with
no TRC rules at all, and nothing errors. The first deployed copy tripped the scanner twice
(an HTML comment header, and a security rule that quoted an injection phrase in order to
forbid it). See deploy/trc/README-SOUL.md.

Loaded through the real `load_soul_md` against a temp HERMES_HOME, not by calling the
scanner alone, so a future change to how SOUL.md is read is covered too.
"""

from __future__ import annotations

import shutil
from pathlib import Path

_DEPLOYED = Path(__file__).resolve().parents[1] / "deploy" / "trc" / "SOUL.md"


def _load(tmp_path, monkeypatch, context_length: int | None = 256_000) -> str:
    """SOUL.md through the real loader (threat scanner + trim) at the deployed window."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    shutil.copy(_DEPLOYED, home / "SOUL.md")

    from agent.prompt_builder import load_soul_md

    loaded = load_soul_md(context_length) or ""
    assert not loaded.startswith("[BLOCKED"), loaded[:200]
    return loaded


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    return text[start:text.index("\n# ", start + 1)]


def _flat(text: str) -> str:
    """Line wraps are layout, not wording."""
    return " ".join(text.split())


def test_the_deployed_soul_loads_as_the_trc_prompt(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    shutil.copy(_DEPLOYED, home / "SOUL.md")

    from agent.prompt_builder import load_soul_md

    loaded = load_soul_md() or ""
    assert not loaded.startswith("[BLOCKED"), loaded[:200]
    assert loaded.startswith("RULE ZERO")


def test_the_soul_keeps_subagents_for_reports_and_comparisons_in_one_turn(tmp_path, monkeypatch):
    """Spec 2026-10-13 §5 C8: subagents only for multi-section research or reports; a
    comparison is parallel lookups in one turn (cheaper and faster)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    shutil.copy(_DEPLOYED, home / "SOUL.md")

    from agent.prompt_builder import load_soul_md

    # A real context window, as the agent passes: without one the 20K floor head/tail-trims.
    loaded = load_soul_md(256_000) or ""
    start = loaded.index("# Subagents (`delegate_task`)")
    section = loaded[start:loaded.index("\n# ", start + 1)]
    assert "ONLY for multi-section research or a report" in section
    assert "Never use" in section and "comparison" in section
    assert "all of them in the same turn" in section
    assert "two or three entities" in section and "execute_code" in section
    assert "four or more entities or periods" in section
    assert "one `execute_code` run" in section
    assert "Never mention helpers or subagents to the user." in section


def test_the_whole_soul_fits_the_deployed_context_window(tmp_path, monkeypatch):
    """SOUL.md grows with every rule; past the window's budget the loader head/tail-trims
    the middle away silently (agent/prompt_builder.py::_truncate_content)."""
    from agent.prompt_builder import _dynamic_context_file_max_chars

    raw = _DEPLOYED.read_text(encoding="utf-8").strip()
    assert len(raw) <= _dynamic_context_file_max_chars(256_000), len(raw)
    assert _load(tmp_path, monkeypatch) == raw


def test_the_soul_settles_a_name_with_resolve_entity_before_any_expensive_call(
    tmp_path, monkeypatch
):
    """Orchestrator spec 2026-10-13 entity resolution §8: resolve first, then §3."""
    loaded = _load(tmp_path, monkeypatch)
    section = _flat(_section(loaded, "# A name: settle WHO before anything else"))
    assert "names a client, company, fund, entity or person, call `resolve_entity`" in section
    assert "BEFORE" in section
    for tool in (
        "query", "find_relationships", "lookup_live", "period_series",
        "meeting_briefing", "generate_report", "knowledge_graph",
    ):
        assert f"`{tool}`" in section, tool
    assert "TRC itself is the firm, not a name to resolve" in section
    assert "its `ref` for `knowledge_graph`" in section
    # Listed with the other tools, before any routing rule uses it.
    tools = _flat(_section(loaded, '# The "ragnarok" tools'))
    assert "- `resolve_entity` —" in tools and "- `knowledge_graph` —" in tools


def test_the_soul_asks_by_the_clarification_policy_in_plain_words(tmp_path, monkeypatch):
    """Spec §3, one rule per row, asked as a plain question with no tool talk."""
    section = _flat(_section(_load(tmp_path, monkeypatch), "# A name: settle WHO before anything else"))
    # Row 1: one exact/normalized match → proceed, say which record was used.
    assert "`ok` → go ahead with that candidate" in section
    assert "naming the record you used" in section
    # Row 2: one near match → "Did you mean X?"
    assert "`ambiguous` with one candidate (a near match) → ask \"Did you mean X?\"" in section
    # Row 3: several → "Which of these do you mean?", up to five, with type and source.
    assert "\"Which of these do you mean?\" with up to five" in section
    assert "\"in Addepar\" or \"in Affinity\"" in section
    # Row 4: none → say so, ask for the name, no broad search.
    assert "`not_found` → say no record matches that name" in section
    assert "Never run a broad search to find it." in section
    # Nothing expensive first; the user chooses; no tool talk.
    assert "Make no other call before the user answers, and never choose for them." in section
    assert "naming no tool or search" in section


def test_one_broad_call_first_is_for_unclear_topics_never_for_names(tmp_path, monkeypatch):
    """Spec §8: the rule-2 conflict is removed; a token is resolved, not queried."""
    loaded = _load(tmp_path, monkeypatch)
    rules = _flat(_section(loaded, "# When a question can mean more than one thing"))
    assert "different answers on WHAT or WHEN → make ONE broad call" in rules
    assert "a name is never settled this way" in rules
    tokens = _flat(_section(loaded, "# Placeholder tokens"))
    assert "is an ordinary question: call `resolve_entity` with it." in tokens
    assert "call `query` with it" not in tokens


def test_resolving_names_spends_no_lookup_and_subagents_get_settled_names(tmp_path, monkeypatch):
    loaded = _load(tmp_path, monkeypatch)
    calls = _flat(_section(loaded, "# Calling tools in one turn"))
    assert "up to THREE data-tool calls for one user message (`resolve_entity` calls not counted)" in calls
    helpers = _flat(_section(loaded, "# Subagents (`delegate_task`)"))
    assert "settle every name with `resolve_entity` first" in helpers
