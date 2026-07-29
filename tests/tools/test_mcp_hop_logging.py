"""One line per outbound MCP tool call, naming what caller context was attached.

This is how a broken metadata chain becomes visible: `chat_hdr=no` says the
conversation id never reached this hop, which is the difference between "the RAG
is misbehaving" and "Open WebUI is not sending it".

Invariant 1: identities, counts and outcomes only. Never text, never token
values.
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("mcp")

from tests.tools.test_mcp_end_user_identity import (  # noqa: E402
    MCP_URL,
    TOKEN_ALICE,
    _connect_http_server,
    _wire_outbound,
)

CHAT_A = "0f7c1a2e-9b3d-4c5f-8a1b-2d3e4f5a6b7c"
MSG_A = "9d1f4b60-1111-4222-8333-444455556666"


@pytest.fixture
def ragnarok():
    from tools import mcp_tool

    mcp_tool._ensure_mcp_loop()
    server, client_kwargs = _connect_http_server(
        mcp_tool, "ragnarok", {"url": MCP_URL, "forward_user_identity": True},
    )
    outbound: list = []
    _wire_outbound(server, client_kwargs, outbound)
    mcp_tool._servers["ragnarok"] = server
    mcp_tool._server_error_counts.pop("ragnarok", None)
    handler = mcp_tool._make_tool_handler("ragnarok", "query", 15.0)
    try:
        yield handler
    finally:
        mcp_tool._servers.pop("ragnarok", None)


def test_hop_line_reports_the_attached_context(ragnarok, caplog):
    from gateway.session_context import (
        reset_end_user_chat_id,
        reset_end_user_identity,
        reset_end_user_request_id,
        set_end_user_chat_id,
        set_end_user_identity,
        set_end_user_request_id,
    )

    caplog.set_level(logging.INFO, logger="tools.mcp_tool")

    c = set_end_user_chat_id(CHAT_A)
    r = set_end_user_request_id(MSG_A)
    i = set_end_user_identity(TOKEN_ALICE)
    try:
        ragnarok({"q": "list the companies"})
    finally:
        reset_end_user_identity(i)
        reset_end_user_request_id(r)
        reset_end_user_chat_id(c)

    line = next(m for m in caplog.messages if "hop=hermes.mcp" in m)
    assert f"req={MSG_A}" in line
    assert f"chat={CHAT_A}" in line
    assert "jwt=yes" in line
    assert "chat_hdr=yes" in line
    assert "tool=query" in line


def test_missing_context_is_reported_as_no(ragnarok, caplog):
    caplog.set_level(logging.INFO, logger="tools.mcp_tool")

    ragnarok({"q": "x"})

    line = next(m for m in caplog.messages if "hop=hermes.mcp" in m)
    assert "jwt=no" in line
    assert "chat_hdr=no" in line


def test_hop_line_never_contains_the_token_or_the_arguments(ragnarok, caplog):
    from gateway.session_context import (
        reset_end_user_identity,
        set_end_user_identity,
    )

    caplog.set_level(logging.INFO, logger="tools.mcp_tool")

    i = set_end_user_identity(TOKEN_ALICE)
    try:
        ragnarok({"q": "Bob Dylan's account number"})
    finally:
        reset_end_user_identity(i)

    line = next(m for m in caplog.messages if "hop=hermes.mcp" in m)
    assert TOKEN_ALICE not in line
    assert "Bob Dylan" not in line
