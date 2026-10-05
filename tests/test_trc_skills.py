"""The TRC playbooks (deploy/trc/skills) and the read-only skills surface that serves
them (2026-10-05).

The playbooks are mounted read-only over $HERMES_HOME/skills, so they are the only
skills indexed, and the platform gets `skills_readonly` — skills_list and skill_view,
never skill_manage. These tests pin the toolset, the files, and the index the model
actually reads.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import yaml

_SKILLS = Path(__file__).resolve().parents[1] / "deploy" / "trc" / "skills"
_DEPLOYED_CONFIG = _SKILLS.parent / "config.yaml"
_SKILL_FILES = sorted(_SKILLS.glob("*/*/SKILL.md"))
_EXPECTED = {
    "trc-deals-and-investments",
    "trc-performance-and-profit",
    "trc-aum-and-portfolio-value",
}


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, f"{path} has no frontmatter"
    return yaml.safe_load(match.group(1))


def test_skills_readonly_resolves_to_list_and_view_only():
    from toolsets import resolve_toolset

    assert set(resolve_toolset("skills_readonly")) == {"skills_list", "skill_view"}


def test_the_playbooks_are_the_expected_set():
    assert {_frontmatter(p)["name"] for p in _SKILL_FILES} == _EXPECTED


@pytest.mark.parametrize("path", _SKILL_FILES, ids=lambda p: p.parent.name)
def test_each_playbook_is_well_formed(path):
    meta = _frontmatter(path)
    assert meta["name"] == path.parent.name
    assert 0 < len(meta["description"]) <= 60, meta["description"]


@pytest.mark.parametrize("path", _SKILL_FILES, ids=lambda p: p.parent.name)
def test_each_playbook_is_clean_for_the_scanners_and_carries_no_tokens(path):
    """Read-only text a model follows: it must pass the same scanner SOUL.md does, and
    carry no placeholder token or source marker, which a model could copy as data."""
    from agent.prompt_builder import _scan_context_content
    from tools.skills_tool import _INJECTION_PATTERNS

    text = path.read_text(encoding="utf-8")
    assert not _scan_context_content(text, path.name).startswith("[BLOCKED")
    assert not any(p in text.lower() for p in _INJECTION_PATTERNS)
    assert not re.search(r"<[A-Z_]+_\d+>", text)
    assert not re.search(r"\[S\d+\]", text)


def test_the_index_lists_only_the_playbooks_with_no_write_path(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    shutil.copytree(_SKILLS, home / "skills")

    from agent import prompt_builder

    prompt_builder._SKILLS_PROMPT_CACHE.clear()
    index = prompt_builder.build_skills_system_prompt(
        available_tools={"skills_list", "skill_view", "execute_code"}
    )
    for name in _EXPECTED:
        assert name in index
    assert "skill_manage" not in index
    assert "save as a skill" not in index
    assert "hermes-agent" not in index


def test_the_full_index_is_unchanged_when_skill_manage_is_present(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    shutil.copytree(_SKILLS, home / "skills")

    from agent import prompt_builder

    prompt_builder._SKILLS_PROMPT_CACHE.clear()
    index = prompt_builder.build_skills_system_prompt(
        available_tools={"skills_list", "skill_view", "skill_manage"}
    )
    assert "## Skills (mandatory)" in index
    assert "skill_manage" in index


def test_the_deployment_drops_the_hermes_help_paragraph():
    cfg = yaml.safe_load(_DEPLOYED_CONFIG.read_text(encoding="utf-8"))
    assert cfg["agent"]["hermes_help_guidance"] is False
