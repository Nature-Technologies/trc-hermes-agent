"""The sandbox `chart()` helper prints a ```vega-lite block within the strict subset
(trc-backend spec 2026-09-29 section 6). It is generated into the stub module and is
pure stdlib (no socket). The subset itself is validated backend-side; here we assert the
emitted spec is structurally within it and carries tokens/numbers verbatim."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

from tools.code_execution_tool import generate_hermes_tools_module


def _chart_fn():
    src = generate_hermes_tools_module(["mcp__ragnarok__query"], transport="pipe")
    ns: dict = {}
    exec(compile(src, "hermes_tools", "exec"), ns)
    return ns["chart"]


def _render(*args, **kw):
    chart = _chart_fn()
    buf = io.StringIO()
    with redirect_stdout(buf):
        chart(*args, **kw)
    return buf.getvalue()


def _spec_of(out):
    assert out.strip().startswith("```vega-lite")
    body = out.split("```vega-lite\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(body)


def test_line_chart_over_a_date_field_is_temporal():
    out = _render(
        "line",
        [{"d": "2024-01-01", "v": 10.0}, {"d": "2024-06-01", "v": 20.0}],
        x="d",
        y="v",
    )
    spec = _spec_of(out)
    assert spec["mark"] in ("line", {"type": "line"})
    assert spec["encoding"]["x"]["type"] == "temporal"
    assert spec["encoding"]["y"]["type"] == "quantitative"
    assert spec["data"]["values"][0]["v"] == 10.0


def test_bar_chart_with_a_token_label_kept_verbatim():
    out = _render(
        "bar",
        [{"name": "<ORGANIZATION_1>", "v": 1622100.0}],
        x="name",
        y="v",
    )
    spec = _spec_of(out)
    assert spec["mark"] in ("bar", {"type": "bar"})
    assert spec["encoding"]["x"]["type"] in ("nominal", "ordinal")
    assert spec["data"]["values"][0]["name"] == "<ORGANIZATION_1>"


def test_series_becomes_a_color_channel():
    out = _render(
        "line",
        [{"d": "2024-01-01", "v": 10.0, "who": "<PERSON_1>"}],
        x="d",
        y="v",
        series="who",
    )
    spec = _spec_of(out)
    assert "color" in spec["encoding"]
    assert spec["encoding"]["color"]["field"] == "who"


def test_title_is_carried():
    out = _render("bar", [{"a": "x", "v": 1.0}], x="a", y="v", title="Growth")
    assert _spec_of(out).get("title") == "Growth"


def test_no_url_or_transform_ever_emitted():
    out = _render("line", [{"d": "2024", "v": 1.0}], x="d", y="v")
    dumped = json.dumps(_spec_of(out))
    assert "url" not in dumped
    assert "transform" not in dumped
    assert "params" not in dumped


def test_arc_chart_uses_theta():
    out = _render("arc", [{"cat": "A", "v": 3.0}, {"cat": "B", "v": 7.0}], x="cat", y="v")
    spec = _spec_of(out)
    assert spec["mark"] in ("arc", {"type": "arc"})
    # arc encodes magnitude as theta (and category as color) — both allowed channels
    enc = spec["encoding"]
    assert "theta" in enc or "y" in enc


# --- expanded chart grammar (trc-backend spec 2026-09-30) ------------------------------


def test_area_and_point_marks_are_supported():
    area = _spec_of(_render("area", [{"d": "2024", "v": 1.0}], x="d", y="v"))
    assert area["mark"] in ("area", {"type": "area"})
    point = _spec_of(_render("point", [{"d": "2024", "v": 1.0}], x="d", y="v"))
    assert point["mark"] in ("point", {"type": "point"})


def test_size_and_opacity_become_channels():
    out = _render(
        "point", [{"d": "2024", "v": 1.0, "w": 3.0}], x="d", y="v", size="w", opacity="w"
    )
    enc = _spec_of(out)["encoding"]
    assert enc["size"]["field"] == "w" and enc["size"]["type"] == "quantitative"
    assert enc["opacity"]["field"] == "w"


def test_point_true_adds_points_to_a_line():
    out = _render("line", [{"d": "2024", "v": 1.0}], x="d", y="v", point=True)
    assert _spec_of(out)["mark"] == {"type": "line", "point": True}


def test_axis_titles_are_carried():
    out = _render(
        "bar", [{"d": "Q1", "v": 1.0}], x="d", y="v", x_title="Quarter", y_title="Value"
    )
    enc = _spec_of(out)["encoding"]
    assert enc["x"]["axis"]["title"] == "Quarter"
    assert enc["y"]["axis"]["title"] == "Value"
