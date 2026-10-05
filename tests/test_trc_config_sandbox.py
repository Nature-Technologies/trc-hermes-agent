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
}


def _cfg() -> dict:
    return yaml.safe_load(_DEPLOYED.read_text(encoding="utf-8"))


def test_the_api_server_gets_ragnarok_and_code_execution_and_nothing_dangerous():
    """Plugin toolsets are on by default upstream, so this pins what must be present
    and what must never be, rather than exact equality."""
    from hermes_cli.tools_config import _get_platform_tools
    from tools.registry import discover_builtin_tools

    discover_builtin_tools()
    enabled = _get_platform_tools(_cfg(), "api_server")
    assert {"ragnarok", "code_execution", "skills_readonly"} <= enabled
    # `skills` would add skill_manage: a model-written skill is an unreviewed prompt
    # that persists into every later session (deploy/trc/config.yaml, 2026-10-05).
    forbidden = {
        "terminal", "file", "web", "browser", "browser-cdp",
        "delegation", "computer_use", "debugging", "coding", "skills",
    }
    assert not enabled & forbidden, enabled & forbidden


def test_the_model_sees_exactly_the_eight_end_user_tools():
    """record_computed is gateway-only and ingest_document is gone (L2); render_report is
    model-callable too (spec §7.1), so it is here as well as in sandbox_tools; period_series
    is the multi-period series tool (trc-backend spec 2026-10-01)."""
    include = set(_cfg()["mcp_servers"]["ragnarok"]["tools"]["include"])
    assert include == {
        "query",
        "period_series",
        "read_document",
        "list_entities",
        "generate_report",
        "render_report",
        "find_relationships",
        "lookup_live",
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
