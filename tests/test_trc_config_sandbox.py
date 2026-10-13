"""The TRC deployment's sandbox surface, from the deployed config.yaml
(trc-backend spec 2026-09-28 §4, §8 limit L2)."""

from __future__ import annotations

from pathlib import Path

import yaml

_DEPLOYED = Path(__file__).resolve().parents[1] / "deploy" / "trc" / "config.yaml"
_DATA_TOOLS = {
    "mcp__ragnarok__query",
    "mcp__ragnarok__read_document",
    "mcp__ragnarok__list_entities",
    "mcp__ragnarok__find_relationships",
}
# The full sandbox allowlist: the four read tools plus render_report (a script calls it to
# publish a computed report, trc-backend spec §7) and period_series (one call for a
# multi-period series, trc-backend spec 2026-10-01).
_SANDBOX_TOOLS = _DATA_TOOLS | {
    "mcp__ragnarok__render_report",
    "mcp__ragnarok__period_series",
    # A name's candidates, and a record's neighbours or files, inside a script
    # (orchestrator spec 2026-10-13 entity resolution §8).
    "mcp__ragnarok__resolve_entity",
    "mcp__ragnarok__knowledge_graph",
}


def _cfg() -> dict:
    return yaml.safe_load(_DEPLOYED.read_text(encoding="utf-8"))


def test_the_api_server_gets_ragnarok_and_code_execution_and_nothing_dangerous():
    """Plugin toolsets are on by default upstream, so this pins what must be present
    and what must never be, rather than exact equality. `delegation` is allowed since
    spec 2026-10-13 §5: children inherit only these toolsets, minus delegate_task."""
    from hermes_cli.tools_config import _get_platform_tools
    from tools.registry import discover_builtin_tools

    discover_builtin_tools()
    enabled = _get_platform_tools(_cfg(), "api_server")
    assert {"ragnarok", "code_execution", "skills_readonly", "delegation"} <= enabled
    # `skills` would add skill_manage: a model-written skill is an unreviewed prompt
    # that persists into every later session (deploy/trc/config.yaml, 2026-10-05).
    forbidden = {
        "terminal", "file", "web", "browser", "browser-cdp",
        "computer_use", "debugging", "coding", "skills",
    }
    assert not enabled & forbidden, enabled & forbidden


def test_the_model_sees_exactly_the_end_user_tools():
    """record_computed is gateway-only and ingest_document is gone (L2); render_report is
    model-callable too (spec §7.1), so it is here as well as in sandbox_tools; period_series
    is the multi-period series tool (trc-backend spec 2026-10-01); resolve_entity and
    knowledge_graph settle a name and walk a record (orchestrator spec 2026-10-13 §8)."""
    include = set(_cfg()["mcp_servers"]["ragnarok"]["tools"]["include"])
    assert include == {
        "query",
        "period_series",
        "read_document",
        "list_entities",
        "generate_report",
        "render_report",
        "find_relationships",
        "list_interactions",
        "meeting_briefing",
        "lookup_live",
        "resolve_entity",
        "knowledge_graph",
    }


def test_scripts_reach_the_read_tools_and_render_report_through_the_sidecar():
    ce = _cfg()["code_execution"]
    assert set(ce["sandbox_tools"]) == _SANDBOX_TOOLS
    assert ce["transport"] == "sidecar"
    assert ce["sidecar_socket"] == "/run/hermes-sandbox/sock"
    assert ce["record_computed_server"] == "ragnarok"
    assert ce["max_tool_calls"] == 20
    assert ce["timeout"] == 270
    assert ce["sidecar_limits"] == {"wall": 240, "cpu": 60, "mem_mb": 768}


def test_every_ragnarok_tool_the_sandbox_intro_names_is_reachable_from_a_script():
    """The intro is prose the model reads before writing a script: a tool it names that a
    script cannot call costs a failed run (orchestrator spec 2026-10-13 §8)."""
    import re

    cfg = _cfg()
    intro = cfg["code_execution"]["description_intro"]
    sandbox = set(cfg["code_execution"]["sandbox_tools"])
    named = {
        tool
        for tool in cfg["mcp_servers"]["ragnarok"]["tools"]["include"]
        if re.search(rf"\b{tool}\b", intro)
    }
    assert "knowledge_graph" in named
    assert {f"mcp__ragnarok__{tool}" for tool in named} <= sandbox


def test_subagents_are_foreground_bounded_and_leave_no_files(tmp_path, monkeypatch):
    """Spec 2026-10-13 §5 C2/C3/C5/C6, read through the MERGED config the delegate tool
    acts on, not just the raw file."""
    import shutil

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    shutil.copy(_DEPLOYED, home / "config.yaml")

    from tools import delegate_tool as dt

    cfg = dt._load_config()
    assert dt._background_enabled(cfg) is False
    assert dt._live_log_enabled(cfg) is False
    assert dt._spill_summaries_enabled(cfg) is False
    assert dt._get_max_concurrent_children() == 3
    assert dt._get_max_spawn_depth() == 1
    # An LLM turn + up to 60 s Hermes admission + one 190 s ragnarok call + a compose
    # turn (deploy/trc/config.yaml, final review 2026-10-13).
    assert dt._get_child_timeout() == 330.0
    assert cfg["load_soul"] is True
    assert cfg["max_iterations"] == 6
    # Unset, so children inherit the route's model and its provider filters.
    raw = _cfg()["delegation"]
    assert "model" not in raw and "provider" not in raw
    assert not cfg.get("model") and not cfg.get("provider")
    contract = cfg["result_instructions"]
    for phrase in ("[Sn]", "placeholder", "figure"):
        assert phrase in contract, phrase
    # One batch of up to three lookups, issued together, then findings.
    assert "at most one batch of up to three lookups" in contract
    assert "issued together in one turn" in contract
    # A child never produces a report: the parent owns publication.
    assert (
        "Never call generate_report or render_report and never publish a report; "
        "return findings only." in contract
    )


def test_delegation_is_the_only_toolset_added_for_subagents():
    assert _cfg()["platform_toolsets"]["api_server"] == [
        "ragnarok", "code_execution", "skills_readonly", "delegation",
    ]
