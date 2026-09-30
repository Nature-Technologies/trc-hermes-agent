"""The sandbox `Report` builder assembles masked markdown from the table()/chart() helpers
and publishes it through render_report (trc-backend spec 2026-09-29 section 7.1). It is
generated into the stub module only when render_report is reachable, and publish() calls
`<namespace>.render_report(title, document)` and prints the report_id for the model.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from unittest.mock import patch

from tools.code_execution_tool import generate_hermes_tools_module


def _module(tools=("mcp__ragnarok__render_report",), allow=None):
    """Generate + exec the stub module with the sandbox allowlist pinned to `allow`
    (defaults to `tools`), so the builder's presence depends only on what we grant, not on
    whichever config.yaml the test environment happens to load."""
    allow = set(tools) if allow is None else set(allow)
    with patch(
        "tools.code_execution_tool._sandbox_allowlist",
        return_value=frozenset(allow),
    ):
        src = generate_hermes_tools_module(list(tools), transport="pipe")
    ns: dict = {}
    exec(compile(src, "hermes_tools", "exec"), ns)
    return ns


def _recording_module():
    """A module whose `_call` records render_report calls instead of hitting a socket."""
    ns = _module()
    calls: list = []

    def fake_call(full, kwargs):
        calls.append((full, kwargs))
        return {
            "status": "ok",
            "report_id": "R1",
            "title": kwargs.get("title"),
            "page_count": 2,
            "expires_in_minutes": 15,
        }

    ns["_call"] = fake_call
    return ns, calls


def test_publish_calls_render_report_with_the_assembled_document():
    ns, calls = _recording_module()
    report = ns["Report"]("<PERSON_1> Q3 Review")
    report.heading("Overview").text("Body about <PERSON_1>.")
    report.table([{"name": "<PERSON_1>", "v": 10.0}], columns=["name", "v"])
    report.chart("bar", [{"name": "<PERSON_1>", "v": 10.0}], x="name", y="v")

    with redirect_stdout(io.StringIO()):
        result = report.publish()

    assert calls, "publish must call render_report"
    full, kwargs = calls[0]
    assert full == "mcp__ragnarok__render_report"
    assert kwargs["title"] == "<PERSON_1> Q3 Review"
    document = kwargs["document"]
    assert "## Overview" in document
    assert "Body about <PERSON_1>." in document
    assert "```vega-lite" in document  # the chart block
    assert "| " in document  # a table row
    assert "<PERSON_1>" in document  # tokens kept verbatim for the backend to restore
    assert result["report_id"] == "R1"


def test_publish_prints_the_report_id_for_the_model():
    ns, _calls = _recording_module()
    buf = io.StringIO()
    with redirect_stdout(buf):
        ns["Report"]("T").text("hi").publish()
    printed = buf.getvalue()
    assert "R1" in printed
    assert "report_id" in printed


def test_a_failed_publish_is_reported_not_silent():
    ns = _module()
    ns["_call"] = lambda full, kwargs: {"status": "invalid_token"}
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = ns["Report"]("T").text("x").publish()
    assert result == {"status": "invalid_token"}
    assert "not published" in buf.getvalue().lower()


def test_heading_level_is_clamped_to_the_subset():
    ns, calls = _recording_module()
    with redirect_stdout(io.StringIO()):
        ns["Report"]("T").heading("Deep", level=7).publish()
    assert "### Deep" in calls[0][1]["document"]  # clamped to h3, never h7


def test_bullets_render_as_a_flat_list():
    ns, calls = _recording_module()
    with redirect_stdout(io.StringIO()):
        ns["Report"]("T").bullets(["one", "two"]).publish()
    document = calls[0][1]["document"]
    assert "- one" in document and "- two" in document


def test_the_report_builder_is_absent_without_render_report():
    """A session that cannot reach render_report gets no Report class — publish would have
    nothing to call."""
    ns = _module(tools=("mcp__ragnarok__query",))
    assert "Report" not in ns
    assert "table" in ns  # the always-on helpers are still there
