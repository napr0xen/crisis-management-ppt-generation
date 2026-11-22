"""Phase 18: native chart population and chart insertion by the PPTX writer."""

import uuid
import zipfile
from io import BytesIO

import pytest
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE as T
from pptx.util import Inches

from doc_intel.mapping.schemas import FieldMappingResult
from doc_intel.templates.anchors_pptx import extract
from doc_intel.templates.schemas import Constraints, IRField
from doc_intel.writers import WriteItem, render
from doc_intel.writers.pptx_writer import write

MISSING = "[MISSING: {label}]"


def _save(prs) -> bytes:
    out = BytesIO()
    prs.save(out)
    return out.getvalue()


def _chart_deck(kind, cats, name, sample, title) -> bytes:
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    data = CategoryChartData()
    data.categories = cats
    data.add_series(name, sample)
    chart = slide.shapes.add_chart(kind, Inches(1), Inches(1), Inches(6), Inches(4), data).chart
    chart.has_title = True
    chart.chart_title.text_frame.text = title
    return _save(prs)


def _placeholder_deck(text="[Chart]") -> bytes:
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(5), Inches(3))
    box.text_frame.text = text
    slide.shapes.add_textbox(
        Inches(1), Inches(0.2), Inches(5), Inches(0.5)
    ).text_frame.text = "[Headline]"
    return _save(prs)


def _field(anchor_id, mode, columns=None, fid="c", label="Series label", etype="chart"):
    return IRField(
        field_id=fid,
        anchor_id=anchor_id,
        section_path=[],
        label=label,
        description="",
        expected_type=etype,
        required=True,
        write_mode=mode,
        constraints=Constraints(columns=columns),
    )


def _anchor(data: bytes, kind: str) -> str:
    return next(a.anchor_id for a in extract(data) if a.kind == kind)


def _charts(data: bytes):
    prs = Presentation(BytesIO(data))
    return [s for s in prs.slides[0].shapes if s.has_chart]


def _plot(chart):
    return list(chart.plots[0].categories), [list(s.values) for s in chart.series]


@pytest.mark.parametrize(
    "kind, cats, name, sample, title, rows, clean_title",
    [
        (
            T.BAR_CLUSTERED,
            [f"[Category {i}]" for i in range(1, 4)],
            "Spend",
            (6, 5, 4),
            "[Spend by category, period ($)]",
            [["Staff", "410.5"], ["Travel", "95"]],
            "Spend by category, period ($)",
        ),
        (
            T.LINE,
            ["[Month 1]", "[Month 2]", "[Month 3]"],
            "Rainfall mm",
            (7, 8, 9),
            "Rainfall at [Station name], monthly",
            [["January", "82"], ["February", "64"], ["March", "71"], ["April", "55"]],
            "Rainfall at Station name, monthly",
        ),
    ],
)
def test_fill_chart_replaces_categories_values_and_unwraps_title(
    kind, cats, name, sample, title, rows, clean_title
):
    tpl = _chart_deck(kind, cats, name, sample, title)
    anchor = _anchor(tpl, "chart")
    out, warnings = write(tpl, [WriteItem(_field(anchor, "fill_chart", columns=[name]), rows)])
    (shape,) = _charts(out)
    chart = shape.chart
    assert _plot(chart) == ([r[0] for r in rows], [[float(r[1]) for r in rows]])
    assert chart.chart_type == kind and chart.series[0].name == name
    assert chart.chart_title.text_frame.text == clean_title
    assert not set(sample) & set(chart.series[0].values)
    assert warnings == []
    Presentation(BytesIO(out))  # opens again
    # only the chart part and its embedded workbook change
    before = zipfile.ZipFile(BytesIO(tpl))
    after = zipfile.ZipFile(BytesIO(out))
    changed = {n for n in before.namelist() if before.read(n) != after.read(n)}
    assert all(n.startswith(("ppt/charts/", "ppt/embeddings/")) for n in changed)


def test_fill_chart_keeps_one_series_per_template_series_and_pads_missing_columns():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    data = CategoryChartData()
    data.categories = ["[A]", "[B]"]
    data.add_series("Loans", (1, 2))
    data.add_series("Returns", (3, 4))
    slide.shapes.add_chart(T.COLUMN_CLUSTERED, 0, 0, Inches(5), Inches(3), data)
    tpl = _save(prs)
    rows = [["Fiction", "120", "80"], ["Poetry", "30"]]
    out, _ = write(
        tpl, [WriteItem(_field(_anchor(tpl, "chart"), "fill_chart", ["Loans", "Returns"]), rows)]
    )
    chart = _charts(out)[0].chart
    assert [s.name for s in chart.series] == ["Loans", "Returns"]
    assert _plot(chart)[1] == [[120.0, 30.0], [80.0, None]]


@pytest.mark.parametrize(
    "rows, kind, series",
    [
        (
            [["January", "82"], ["February", "64"], ["March", "71"]],
            T.LINE,
            ["Rainfall mm"],
        ),
        (
            [["Fiction", "1240"], ["Poetry", "310"], ["Travel", "95"]],
            T.COLUMN_CLUSTERED,
            ["Loans"],
        ),
        ([["Rent", "50"], ["Food", "30"], ["Other", "20"]], T.PIE, ["Share"]),
    ],
)
def test_insert_chart_replaces_placeholder_at_its_exact_box(rows, kind, series):
    tpl = _placeholder_deck()
    prs = Presentation(BytesIO(tpl))
    box = next(s for s in prs.slides[0].shapes if s.text_frame.text == "[Chart]")
    geometry = (box.left, box.top, box.width, box.height)
    anchor = next(a.anchor_id for a in extract(tpl) if a.text == "[Chart]")
    field = _field(anchor, "insert_chart", series, etype="chart")
    out, _ = write(tpl, [WriteItem(field, rows)])
    (shape,) = _charts(out)
    assert (shape.left, shape.top, shape.width, shape.height) == geometry
    assert shape.chart.chart_type == kind
    assert _plot(shape.chart) == ([r[0] for r in rows], [[float(r[1]) for r in rows]])
    assert [s.name for s in shape.chart.series] == series
    assert shape.chart.has_legend == (kind == T.PIE)
    texts = [
        s.text_frame.text for s in Presentation(BytesIO(out)).slides[0].shapes if s.has_text_frame
    ]
    assert "[Chart]" not in texts and texts == ["[Headline]"]  # the other box is untouched


def test_insert_chart_with_two_series_has_a_legend():
    tpl = _placeholder_deck("Insert a chart of loans and returns")
    anchor = next(a.anchor_id for a in extract(tpl) if a.text.startswith("Insert"))
    rows = [["Fiction", "120", "80"], ["Poetry", "30", "20"]]
    out, _ = write(tpl, [WriteItem(_field(anchor, "insert_chart", ["Loans", "Returns"]), rows)])
    chart = _charts(out)[0].chart
    assert chart.has_legend and [s.name for s in chart.series] == ["Loans", "Returns"]


def test_missing_marker_leaves_a_clean_empty_chart_and_marker_text_in_the_placeholder():
    tpl = _chart_deck(T.COLUMN_CLUSTERED, ["[A]", "[B]"], "Spend", (6, 5), "[Title]")
    marker = ["[MISSING: Spend by category]"]
    out, _ = write(tpl, [WriteItem(_field(_anchor(tpl, "chart"), "fill_chart"), marker)])
    chart = _charts(out)[0].chart
    assert chart.chart_title.text_frame.text == marker[0]
    assert [v for s in chart.series for v in s.values] == [None]
    assert chart.chart_type == T.COLUMN_CLUSTERED

    tpl = _placeholder_deck()
    anchor = next(a.anchor_id for a in extract(tpl) if a.text == "[Chart]")
    out, _ = write(tpl, [WriteItem(_field(anchor, "insert_chart", ["S"]), ["[MISSING: Chart]"])])
    prs = Presentation(BytesIO(out))
    assert not _charts(out)
    assert "[MISSING: Chart]" in [s.text_frame.text for s in prs.slides[0].shapes]


def _user(field_id, value) -> FieldMappingResult:
    return FieldMappingResult(
        field_id=field_id,
        status="populated" if value is not None else "missing",
        value=value,
        evidence=[],
        confidence=1.0,
        missing_reason=None if value is not None else "cleared_by_user",
        checked_documents=[uuid.uuid4()],
        conflict_details=None,
        provenance="user",
    )


def test_render_mixed_ir_text_and_both_chart_modes():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_textbox(
        Inches(1), Inches(0.2), Inches(5), Inches(0.5)
    ).text_frame.text = "Station: [Station]"
    box = slide.shapes.add_textbox(Inches(1), Inches(5), Inches(4), Inches(2))
    box.text_frame.text = "[Chart]"
    data = CategoryChartData()
    data.categories = ["[Genre 1]", "[Genre 2]"]
    data.add_series("Loans", (9, 8))
    slide.shapes.add_chart(T.BAR_CLUSTERED, Inches(5), Inches(1), Inches(4), Inches(3), data)
    tpl = _save(prs)
    ids = {a.kind + ("_box" if a.text == "[Chart]" else ""): a.anchor_id for a in extract(tpl)}
    text_id = next(a.anchor_id for a in extract(tpl) if a.tokens == ["[Station]"])
    fields = [
        _field(ids["chart"], "fill_chart", ["Loans"], fid="loans"),
        _field(text_id, "replace_token", fid="station", etype="text").model_copy(
            update={"constraints": Constraints(token="[Station]")}
        ),
        _field(ids["shape_text_box"], "insert_chart", ["Rainfall mm"], fid="rain"),
    ]
    results = {
        "loans": _user("loans", [["Fiction", "1240"], ["Poetry", "310"]]),
        "station": _user("station", "North Gauge"),
        "rain": _user("rain", [["Jan", "82"], ["Feb", "64"], ["Mar", "71"]]),
    }
    out, _ = render("pptx", tpl, fields, results, missing_marker=MISSING, conflict_marker="[C]")
    shapes = Presentation(BytesIO(out)).slides[0].shapes
    assert "Station: North Gauge" in [s.text_frame.text for s in shapes if s.has_text_frame]
    charts = sorted((s.chart for s in shapes if s.has_chart), key=lambda c: c.chart_type)
    assert [c.chart_type for c in charts] == sorted([T.BAR_CLUSTERED, T.LINE])
    by_type = {c.chart_type: _plot(c) for c in charts}
    assert by_type[T.BAR_CLUSTERED] == (["Fiction", "Poetry"], [[1240.0, 310.0]])
    assert by_type[T.LINE] == (["Jan", "Feb", "Mar"], [[82.0, 64.0, 71.0]])
