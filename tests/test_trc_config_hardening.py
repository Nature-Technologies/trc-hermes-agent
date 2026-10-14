"""What reaches the TRC chat model, and what outlives a turn (trc-orchestrator spec
2026-10-14 answer-first §3.5, §12.3).

Each test copies the deployed config.yaml into a temp HERMES_HOME and asks the code that
acts on it, not the raw YAML: a key Hermes does not read fails here, the way the memory
switch once did (tests/test_trc_config_memory_off.py).

The wire is read through Hermes' real MCP registration path, from a stand-in server that
offers every tool the backend could offer and advertises resources and prompts, as
FastMCP does.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_DEPLOYED = Path(__file__).resolve().parents[1] / "deploy" / "trc" / "config.yaml"
_PREFIX = "mcp__ragnarok__"
# A stand-in for the backend's MCP tool list: the end-user tools, plus the ones that
# must never reach the model (record_computed is the gateway's own call; ingest_document
# writes to the corpus).
_OFFERED = (
    "query", "period_series", "read_document", "list_entities", "generate_report",
    "render_report", "find_relationships", "list_interactions", "meeting_briefing",
    "lookup_live", "resolve_entity", "rank_and_count", "knowledge_graph",
    "record_computed", "ingest_document",
)
_UTILITY = {"list_resources", "read_resource", "list_prompts", "get_prompt"}


@pytest.fixture
def deployed(tmp_path, monkeypatch) -> dict:
    """The MERGED config Hermes acts on, with HERMES_HOME pointing at it."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    shutil.copy(_DEPLOYED, home / "config.yaml")

    from hermes_cli.config import load_config

    return load_config()


def _ragnarok_wire(deployed: dict) -> set[str]:
    """The ragnarok tool names the real registration path puts on the wire."""
    from tools.mcp_tool import MCPServerTask, _forget_mcp_tool_server, _register_server_tools
    from tools.registry import ToolRegistry

    server = MCPServerTask("ragnarok")
    server._tools = [
        SimpleNamespace(name=n, description=f"{n}.", inputSchema={"type": "object", "properties": {}})
        for n in _OFFERED
    ]
    server.session = MagicMock(spec=sorted(_UTILITY))
    server.initialize_result = SimpleNamespace(
        capabilities=SimpleNamespace(
            tools=SimpleNamespace(), resources=SimpleNamespace(), prompts=SimpleNamespace()
        )
    )
    with patch("tools.registry.registry", ToolRegistry()):
        registered = _register_server_tools("ragnarok", server, deployed["mcp_servers"]["ragnarok"])
    for name in registered:
        _forget_mcp_tool_server(name)
    return {name.removeprefix(_PREFIX) for name in registered}


def test_nothing_reaches_the_wire_that_tools_include_does_not_list(deployed):
    """The wire is the allowlist and nothing more. Hermes registers the four MCP utility
    tools (`_UTILITY`) outside `tools.include` when the server advertises resources and
    prompts; without `resources: false` / `prompts: false` the model gets tools no rule
    mentions. Stated as an invariant, not a list of known names, so a tool registered
    outside `include` by any other path fails here too."""
    include = set(deployed["mcp_servers"]["ragnarok"]["tools"]["include"])
    assert _ragnarok_wire(deployed) == include & set(_OFFERED)


def test_meeting_briefing_is_off_the_wire_until_the_backend_has_meeting_data(deployed):
    """No meeting parser is registered yet, so every call would answer `unavailable`."""
    assert "meeting_briefing" not in _ragnarok_wire(deployed)


def test_the_gateway_only_and_corpus_write_tools_never_reach_the_model(deployed):
    wire = _ragnarok_wire(deployed)
    assert "record_computed" not in wire
    assert "ingest_document" not in wire


def test_tool_search_never_swaps_the_ragnarok_tools_for_bridge_tools(deployed):
    from tools.tool_search import load_config, should_activate

    config = load_config()
    assert config.enabled == "off"
    # However large the deferrable surface grows, against a real context window.
    assert should_activate(config, deferrable_tokens=1_000_000, context_length=256_000) is False


def test_openrouter_calls_ask_for_no_response_cache(deployed, monkeypatch):
    """With the header, OpenRouter keeps each masked response to replay: a copy of the
    turn outside TRC's retention sweep. The env var would override config, so it is
    cleared to test the config itself."""
    monkeypatch.delenv("HERMES_OPENROUTER_CACHE", raising=False)
    monkeypatch.delenv("HERMES_OPENROUTER_CACHE_TTL", raising=False)
    from agent.auxiliary_client import build_or_headers

    headers = build_or_headers()
    assert "X-OpenRouter-Cache" not in headers
    assert "X-OpenRouter-Cache-TTL" not in headers


def test_rankings_and_names_each_have_their_one_call_tool_on_the_wire(deployed):
    wire = _ragnarok_wire(deployed)
    assert {"resolve_entity", "rank_and_count"} <= wire
    # Superseded by rank_and_count: with two roster tools the model has to choose.
    assert "list_entities" not in wire


def _script_tools(deployed: dict) -> frozenset[str]:
    """The tools Hermes offers a sandbox script in this session: the allowlist
    intersected with the session's tools, the call model_tools.py builds the
    execute_code schema from."""
    from tools.code_execution_tool import resolve_sandbox_tools

    session = {_PREFIX + name for name in _ragnarok_wire(deployed)}
    return resolve_sandbox_tools(session | {"execute_code"})


def test_every_allowlisted_script_tool_is_one_the_session_has(deployed):
    """An allowlist entry that is not on the wire is silently dropped by the
    intersection: dead config that reads like a capability."""
    from tools.code_execution_tool import _sandbox_allowlist

    assert _script_tools(deployed) == _sandbox_allowlist()


def test_scripts_reach_nothing_but_ragnarok(deployed):
    """On the allowlist itself: intersected with the session's tools (all ragnarok), a
    `terminal` entry would be dropped and this could never fail."""
    from tools.code_execution_tool import _sandbox_allowlist

    allowlist = _sandbox_allowlist()
    assert allowlist
    assert all(name.startswith(_PREFIX) for name in allowlist), allowlist


def test_a_script_can_fetch_the_inputs_of_a_derived_figure(deployed):
    """SOUL.md: a share, growth rate or average comes from execute_code, which fetches
    its own inputs — a ranking's rows and firm total, or one entity's series."""
    assert {_PREFIX + "rank_and_count", _PREFIX + "period_series"} <= _script_tools(deployed)


def test_a_script_cannot_spend_the_live_budget_pick_a_name_or_record_figures(deployed):
    """lookup_live spends the firm-shared Addepar budget; resolve_entity can decide
    `ask`, which only the model can act on; record_computed is the gateway's call."""
    from tools.code_execution_tool import _sandbox_allowlist

    # The allowlist, not the intersection: record_computed is not on the wire, so the
    # intersection would drop it and hide the entry.
    allowlist = _sandbox_allowlist()
    for name in ("lookup_live", "resolve_entity", "record_computed"):
        assert _PREFIX + name not in allowlist, name


@pytest.fixture
def chat_platform(deployed, tmp_path, monkeypatch) -> tuple[set[str], set[str]]:
    """(toolsets, tool names) the API server gives the chat model, resolved the way
    gateway/platforms/api_server.py does it: the gateway's own config loader, then
    _get_platform_tools(..., "api_server"), then each toolset's tools. The ragnarok MCP
    tools register at connect time; _ragnarok_wire covers those."""
    from gateway import run as gateway_run
    from hermes_cli.tools_config import _get_platform_tools
    from tools.registry import discover_builtin_tools
    from toolsets import resolve_toolset

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path / ".hermes")
    discover_builtin_tools()
    toolsets = set(_get_platform_tools(gateway_run._load_gateway_config(), "api_server"))
    tools: set[str] = set()
    for toolset in toolsets:
        tools.update(resolve_toolset(toolset))
    return toolsets, tools


def test_no_skill_tool_reaches_the_chat_model(chat_platform):
    """With none of these on the wire Hermes also leaves the skills index out of the
    system prompt (agent/system_prompt.py), so SOUL.md is the only routing text."""
    _, tools = chat_platform
    assert not tools & {"skills_list", "skill_view", "skill_manage"}


def test_delegate_task_is_off_the_chat_wire_until_long_task_escalation(chat_platform):
    """SOUL v2 routes nothing to it, and its own description invites delegating
    "research A and B simultaneously": a delegated comparison costs minutes (contract
    C5b). It returns in P2, with a prompt rule for when to use it."""
    toolsets, tools = chat_platform
    assert "delegation" not in toolsets
    assert "delegate_task" not in tools


def test_the_execute_code_description_names_only_tools_the_model_has(deployed):
    """The description routes the model too (rankings to rank_and_count, a series to
    period_series); a tool it names that is off the wire is a call to nothing."""
    intro = deployed["code_execution"]["description_intro"]
    wire = _ragnarok_wire(deployed)
    named = {name for name in _OFFERED if name in intro}
    assert named <= wire, named - wire
