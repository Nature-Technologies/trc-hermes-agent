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


def test_the_deployed_soul_loads_as_the_trc_prompt(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    shutil.copy(_DEPLOYED, home / "SOUL.md")

    from agent.prompt_builder import load_soul_md

    loaded = load_soul_md() or ""
    assert not loaded.startswith("[BLOCKED"), loaded[:200]
    assert loaded.startswith("RULE ZERO")
