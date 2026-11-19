import json
import re
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches

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
