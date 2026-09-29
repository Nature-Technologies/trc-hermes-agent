"""The sandbox allowlist fails CLOSED (trc-backend spec 2026-09-28 §4.2).

Upstream fell back to every sandbox tool when a session shared none of them, so a TRC
session (ragnarok + code_execution) would have handed a script `terminal` and file
access. An empty intersection now means no tools."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from tools import code_execution_tool as cet


@pytest.fixture(autouse=True)
def _no_transport_pin(monkeypatch):
    """No transport pinned by the host running the tests (the TRC image sets one)."""
    monkeypatch.delenv(cet.TRANSPORT_ENV, raising=False)
    monkeypatch.setattr(cet, "_warned_sidecar_without_allowlist", False)


def _config(cfg):
    return patch.object(cet, "_load_config", return_value=cfg)


def test_default_allowlist_is_upstreams_list():
    with _config({}):
        assert cet._sandbox_allowlist() == cet.SANDBOX_ALLOWED_TOOLS


def test_configured_allowlist_replaces_the_default():
    with _config({"sandbox_tools": ["mcp__ragnarok__query"]}):
        assert cet._sandbox_allowlist() == frozenset({"mcp__ragnarok__query"})


def test_no_overlap_means_no_tools_not_all_tools():
    with _config({}):
        assert cet.resolve_sandbox_tools(["vision_analyze"]) == frozenset()


def test_none_and_empty_mean_no_tools():
    with _config({}):
        assert cet.resolve_sandbox_tools(None) == frozenset()
        assert cet.resolve_sandbox_tools([]) == frozenset()


def test_the_trc_session_gets_exactly_its_data_tools():
    session = [
        "execute_code",
        "mcp__ragnarok__query",
        "mcp__ragnarok__lookup_live",
        "mcp__ragnarok__list_entities",
    ]
    allow = ["mcp__ragnarok__query", "mcp__ragnarok__list_entities"]
    with _config({"sandbox_tools": allow}):
        assert cet.resolve_sandbox_tools(session) == frozenset(allow)


def test_the_stub_module_never_contains_a_tool_outside_the_allowlist():
    with _config({"sandbox_tools": ["mcp__ragnarok__query"]}):
        src = cet.generate_hermes_tools_module(["terminal", "mcp__ragnarok__query"])
    assert "def terminal(" not in src


def test_malformed_sandbox_tools_value_fails_closed():
    """A non-list value is misconfiguration: fail closed, never fall back to the built-in 7."""
    with _config({"sandbox_tools": "web_search"}):
        assert cet._sandbox_allowlist() == frozenset()


def test_empty_list_in_config_means_no_tools():
    """An explicit empty list means the operator wants zero tools: honour it."""
    with _config({"sandbox_tools": []}):
        assert cet._sandbox_allowlist() == frozenset()


def test_the_sidecar_pin_with_no_config_allows_no_tools(monkeypatch, caplog):
    """config.yaml unreadable reads as {}; the env pin still sends scripts to the
    sidecar, so the built-in seven (terminal, files, web) must not be offered."""
    monkeypatch.setenv(cet.TRANSPORT_ENV, "sidecar")
    with _config({}), caplog.at_level(logging.WARNING, logger=cet.logger.name):
        assert cet._sandbox_allowlist() == frozenset()
        assert cet._sandbox_allowlist() == frozenset()
        assert cet.resolve_sandbox_tools(["terminal", "read_file"]) == frozenset()
    warnings = [r for r in caplog.records if "sandbox_tools is not set" in r.getMessage()]
    assert len(warnings) == 1, "warn once, not per call"


def test_no_pin_and_no_config_keeps_the_built_in_seven():
    with _config({}):
        assert cet._sandbox_allowlist() == cet.SANDBOX_ALLOWED_TOOLS
        assert len(cet.SANDBOX_ALLOWED_TOOLS) == 7
