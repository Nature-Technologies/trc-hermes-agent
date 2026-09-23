"""The TRC deploy must actually switch Hermes' persistent memory OFF.

`deploy/trc/config.yaml` said `memory: {enabled: false}` — a key Hermes does not read.
The real switches are `memory.memory_enabled` and `memory.user_profile_enabled`, both
`True` in DEFAULT_CONFIG, and the deployed file is deep-merged over those defaults. So
memory stayed on: a "USER PROFILE" written from earlier chats sat in every system prompt
on staging (2026-09-23), persisted on the volume with no retention, and the model quoted
it back to the user as "your system note".

In this deployment that is a PII question, not a preference: Hermes' memory is written
from conversation text outside the redaction boundary's retention sweep, and a profile
of who the user is and what they ask about is exactly what must not accumulate there.

Loaded through the real `load_config` against a temp HERMES_HOME, so the assertion is
about the MERGED config Hermes will act on, not the raw file.
"""

from __future__ import annotations

import shutil
from pathlib import Path

_DEPLOYED = Path(__file__).resolve().parents[1] / "deploy" / "trc" / "config.yaml"


def test_the_deployed_config_turns_both_memory_stores_off(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    shutil.copy(_DEPLOYED, home / "config.yaml")

    from hermes_cli.config import load_config

    memory = load_config().get("memory", {})
    assert memory.get("memory_enabled") is False
    assert memory.get("user_profile_enabled") is False
