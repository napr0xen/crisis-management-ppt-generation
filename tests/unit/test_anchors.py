import json
import re
import time
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches
from structlog.testing import capture_logs

from doc_intel.core.errors import ValidationFailed
from doc_intel.ingestion.pipeline import ParseError
from doc_intel.templates.anchors import build_anchor_map, find_tokens
from doc_intel.templates.schemas import MAX_ANCHORS
from tests.fixtures.templates import _sdt, docx_template, pptx_template, xlsx_template

GOLDEN = Path(__file__).parents[1] / "fixtures" / "templates"
BUILDERS = {"pptx": pptx_template, "docx": docx_template, "xlsx": xlsx_template}
GRAMMAR = {
    "pptx": r"pptx:s\d+:sp\d+(:r\d+c\d+)?",
    "docx": r"docx:(body:p\d+|body:t\d+:r\d+c\d+|sec\d+:(header|footer):p\d+|sdt:.+)",
    "xlsx": r"xlsx:.+![A-Z]+\d+",
}


def _bytes(wb_or_doc) -> bytes:
    out = BytesIO()
    wb_or_doc.save(out)
    return out.getvalue()


@pytest.mark.parametrize("fmt", ["pptx", "docx", "xlsx"])
def test_golden_and_grammar(fmt):
    amap = build_anchor_map(fmt, BUILDERS[fmt]())
    assert amap.model_dump(mode="json") == json.loads((GOLDEN / f"{fmt}.anchors.json").read_text())
    ids = [a.anchor_id for a in amap.anchors]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(GRAMMAR[fmt], i) for i in ids)


def test_xlsx_formula_never_anchored():
    ids = [a.anchor_id for a in build_anchor_map("xlsx", xlsx_template()).anchors]
    assert "xlsx:Summary!B4" not in ids
    assert "xlsx:Summary!B2" in ids  # empty value next to a label
    assert "xlsx:Q1!Data!B1" in ids  # split on the last "!"


def test_docx_split_run_token_found():
    amap = build_anchor_map("docx", docx_template())
    assert any(a.tokens == ["{{client_name}}"] for a in amap.anchors)
    assert any(a.tokens == ["____"] for a in amap.anchors)


def test_docx_duplicate_sdt_tag_keeps_first():
    doc = Document()
    doc.add_paragraph("x")
    sect_pr = doc.element.body.sectPr
    sect_pr.addprevious(_sdt("dup", "first"))
    sect_pr.addprevious(_sdt("dup", "second"))
    anchors = [
        a for a in build_anchor_map("docx", _bytes(doc)).anchors if a.kind == "content_control"
    ]
    assert [(a.anchor_id, a.text) for a in anchors] == [("docx:sdt:dup", "first")]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        ("plain text", []),
        ("{{a}} and {{a}} and {{b}}", ["{{a}}", "{{b}}"]),
        ("Dear [client name], <date> XXX TBD", ["[client name]", "<date>", "XXX", "TBD"]),
        ("XXXX TBDX", []),
        ("___", []),
        ("Sign ____ and _____", ["____", "_____"]),
        ("{{" + "a" * 101 + "}}", []),
        ("{{multi\nline}}", []),
        ("[1] and [ x] and [2024]", []),
        ("a < b > c", []),
        ("[client name] <classification>", ["[client name]", "<classification>"]),
    ],
)
def test_find_tokens(text, expected):
    assert find_tokens(text) == expected


def test_find_tokens_linear_on_hostile_input():
    assert find_tokens("[" * 50_000 + "{{" * 50_000) == []


def _many_xlsx(n: int) -> bytes:
    wb = Workbook()
    for i in range(1, n + 1):
        wb.active.cell(row=i, column=1, value="{{x}}")
    return _bytes(wb)


def _many_pptx(n: int) -> bytes:
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for _ in range(n):
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(1), Inches(1)).text_frame.text = "a"
    return _bytes(prs)


@pytest.mark.parametrize(("fmt", "build"), [("xlsx", _many_xlsx), ("pptx", _many_pptx)])
def test_max_anchors_boundary(fmt, build):
    assert len(build_anchor_map(fmt, build(MAX_ANCHORS)).anchors) == MAX_ANCHORS
    with pytest.raises(ValidationFailed, match=rf"{MAX_ANCHORS + 1}.*{MAX_ANCHORS}"):
        build_anchor_map(fmt, build(MAX_ANCHORS + 1))


@pytest.mark.parametrize("fmt", ["pptx", "docx", "xlsx"])
def test_zero_anchors(fmt):
    empty = {"pptx": Presentation(), "docx": Document(), "xlsx": Workbook()}[fmt]
    with pytest.raises(ValidationFailed, match="no fillable locations"):
        build_anchor_map(fmt, _bytes(empty))


@pytest.mark.parametrize("fmt", ["pptx", "docx", "xlsx"])
def test_corrupt_bytes(fmt):
    with pytest.raises(ParseError):
        build_anchor_map(fmt, b"not a zip file")


def test_missing_part_is_parse_error():
    import zipfile

    out = BytesIO()
    with zipfile.ZipFile(BytesIO(docx_template())) as src, zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            if item.filename != "word/document.xml":
                dst.writestr(item, src.read(item.filename))
    with pytest.raises(ParseError):
        build_anchor_map("docx", out.getvalue())


def test_is_placeholder_means_unfilled():
    by_id = {a.anchor_id: a for a in build_anchor_map("pptx", pptx_template()).anchors}
    assert not by_id["pptx:s1:sp2"].is_placeholder  # title placeholder already holding a title
    assert by_id["pptx:s1:sp3"].is_placeholder  # empty on the slide: layout prompt text
    docx_by_id = {a.anchor_id: a for a in build_anchor_map("docx", docx_template()).anchors}
    assert docx_by_id["docx:sdt:owner"].is_placeholder  # content controls exist to be filled


def test_xlsx_over_limit_fails_before_path_scan(monkeypatch):
    def boom(*a):
        raise AssertionError("path scan ran before the MAX_ANCHORS check")

    monkeypatch.setattr("doc_intel.templates.anchors_xlsx._nearest", boom)
    start = time.monotonic()
    with pytest.raises(ValidationFailed, match=rf"{MAX_ANCHORS + 1}.*{MAX_ANCHORS}"):
        build_anchor_map("xlsx", _many_xlsx(MAX_ANCHORS + 1))
    assert time.monotonic() - start < 2


def test_docx_vertical_merge_yields_one_anchor_at_origin():
    doc = Document()
    table = doc.add_table(rows=3, cols=2)
    table.cell(1, 0).merge(table.cell(2, 0))
    ids = {a.anchor_id for a in build_anchor_map("docx", _bytes(doc)).anchors}
    assert "docx:body:t0:r1c0" in ids
    assert "docx:body:t0:r2c0" not in ids


def test_extractor_failure_is_logged_with_type_only(monkeypatch):
    def broken(data):
        raise KeyError("secret document text")

    monkeypatch.setattr("doc_intel.templates.anchors_docx.extract", broken)
    with capture_logs() as logs, pytest.raises(ParseError):
        build_anchor_map("docx", docx_template())
    assert logs == [
        {
            "event": "template_parse_failed",
            "fmt": "docx",
            "error_type": "KeyError",
            "log_level": "warning",
        }
    ]


def test_docx_short_header_row_does_not_crash():
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    doc = Document()
    table = doc.add_table(rows=2, cols=3)
    table.cell(0, 0).text = "A"
    table.cell(0, 1).text = "B"
    tr = table.rows[0]._tr
    tr.remove(tr.tc_lst[-1])
    grid_after = OxmlElement("w:gridAfter")
    grid_after.set(qn("w:val"), "1")
    tr.get_or_add_trPr().append(grid_after)
    by_id = {a.anchor_id: a for a in build_anchor_map("docx", _bytes(doc)).anchors}
    assert by_id["docx:body:t0:r1c2"].table.header is None
    assert by_id["docx:body:t0:r1c0"].table.header == "A"
    assert by_id["docx:body:t0:r1c1"].table.header == "B"


@pytest.mark.parametrize(
    ("label_cell", "off_grid", "on_grid"),
    [("XFD1", "XFE1", "XFD2"), ("A1048576", "A1048577", "B1048576")],
)
def test_xlsx_neighbours_stay_inside_the_grid(label_cell, off_grid, on_grid):
    wb = Workbook()
    wb.active.title = "S"
    wb.active[label_cell] = "Label"
    ids = {a.anchor_id for a in build_anchor_map("xlsx", _bytes(wb)).anchors}
    assert f"xlsx:S!{off_grid}" not in ids
    assert f"xlsx:S!{on_grid}" in ids
