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

import re
import shutil
from pathlib import Path

_DEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "trc"
_DEPLOYED = _DEPLOY / "SOUL.md"


def _load(tmp_path, monkeypatch, context_length=None) -> str:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    shutil.copy(_DEPLOYED, home / "SOUL.md")

    from agent.prompt_builder import load_soul_md

    return load_soul_md(context_length) or ""


def test_the_deployed_soul_loads_as_the_trc_prompt(tmp_path, monkeypatch):
    loaded = _load(tmp_path, monkeypatch)
    assert not loaded.startswith("[BLOCKED"), loaded[:200]
    assert loaded.startswith("RULE ZERO")


def test_the_whole_prompt_loads_unchanged_even_without_a_context_window(tmp_path, monkeypatch):
    """No scanner rewrite and no head/tail trim. With no context window (the deploy
    smoke check calls it that way) the loader trims at its 20 K floor; the budget keeps
    the prompt well under it."""
    loaded = _load(tmp_path, monkeypatch)
    assert loaded == _DEPLOYED.read_text(encoding="utf-8").strip()


def test_the_loaded_prompt_carries_the_contracts_other_components_rely_on(tmp_path, monkeypatch):
    loaded = _load(tmp_path, monkeypatch, 256_000)
    # Contract C2: resolve_entity's three decisions, each with what to do.
    for decision in ("`pick`", "`each`", "`ask`"):
        assert decision in loaded, decision
    # Contract C3: the block the backend filter and the Open WebUI fork parse.
    assert "trc:followups" in loaded


def test_every_tool_the_prompt_routes_to_reaches_the_model(tmp_path, monkeypatch):
    """Each route in SOUL.md (an arrow, then a backticked tool name) is a ragnarok tool
    on the wire, or execute_code from the code_execution toolset: a route to a tool the
    model does not have is a call to nothing."""
    soul = _DEPLOYED.read_text(encoding="utf-8")
    routed = set(re.findall(r"→\s+`([a-z_]+)`", soul))
    assert {"resolve_entity", "rank_and_count"} <= routed

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    shutil.copy(_DEPLOY / "config.yaml", home / "config.yaml")
    from hermes_cli.config import load_config

    cfg = load_config()
    include = set(cfg["mcp_servers"]["ragnarok"]["tools"]["include"])
    assert routed - {"execute_code"} <= include, routed - {"execute_code"} - include
    if "execute_code" in routed:
        assert "code_execution" in cfg["platform_toolsets"]["api_server"]
