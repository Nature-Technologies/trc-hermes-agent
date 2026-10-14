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


def test_the_mcp_resource_and_prompt_tools_stay_off_the_wire(deployed):
    """They bypass `tools.include`: without `resources: false` / `prompts: false` the
    model gets four tools no rule mentions."""
    wire = _ragnarok_wire(deployed)
    assert not wire & _UTILITY, wire & _UTILITY


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
