"""The sandbox `table()` helper prints a GFM table with the number formats of
trc-backend spec 2026-09-29 section 5.1, an em dash for missing values, and a Source
column. It is generated into the stub module and is pure stdlib (no socket)."""

from __future__ import annotations

import io
from contextlib import redirect_stdout

from tools.code_execution_tool import generate_hermes_tools_module


def _table_fn():
    """Exec the generated stub module and return its `table` callable.

    Defining the module must not require a socket — the transport helpers connect
    lazily, so exec of the module text is side-effect free.
    """
    src = generate_hermes_tools_module(["mcp__ragnarok__query"], transport="pipe")
    ns: dict = {}
    exec(compile(src, "hermes_tools", "exec"), ns)
    return ns["table"]


def _render(rows, **kw):
    table = _table_fn()
    buf = io.StringIO()
    with redirect_stdout(buf):
        table(rows, **kw)
    return buf.getvalue()


def test_table_formats_amounts_and_adds_a_source_column():
    out = _render(
        [
            {"name": "<ORGANIZATION_1>", "value": 1622100.0, "source": "S78"},
            {"name": "<ORGANIZATION_2>", "value": 2284926.04, "source": "S100"},
        ]
    )
    assert "Name" in out and "Value" in out and "Source" in out
    assert "1,622,100.00" in out  # thousands + 2 decimals
    assert "[S78]" in out  # source rendered as a marker
    assert "<ORGANIZATION_1>" in out  # token printed verbatim (Part 0 promotes it)


def test_table_renders_missing_as_em_dash_not_zero():
    out = _render([{"name": "<ORGANIZATION_1>", "value": None, "source": "S1"}])
    assert "—" in out
    assert "0.00" not in out


def test_table_without_a_source_leaves_the_cell_an_em_dash_not_crashing():
    out = _render([{"name": "<PERSON_1>", "value": 10}])
    assert "—" in out  # empty Source cell is an em dash
    assert "<PERSON_1>" in out  # no exception, token present


def test_percent_change_carries_a_sign():
    out = _render(
        [
            {"period": "2024", "change_pct": 2.0},
            {"period": "2025", "change_pct": -3.1},
        ]
    )
    assert "+2.0%" in out and "-3.1%" in out


def test_empty_rows_do_not_crash():
    assert "no rows" in _render([]).lower()
