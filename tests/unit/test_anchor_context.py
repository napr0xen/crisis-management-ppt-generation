"""Static heading/label context per anchor (pptx cards, table cells, docx labels), two domains."""

from io import BytesIO

import pytest
from docx import Document
from pptx import Presentation
from pptx.util import Inches

from doc_intel.templates.anchors_docx import extract as extract_docx
from doc_intel.templates.anchors_pptx import extract as extract_pptx


def _save(obj) -> bytes:
    out = BytesIO()
    obj.save(out)
    return out.getvalue()


def _box(shapes, text, left, top, width=3.0, height=1.0):
    box = shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    box.text_frame.text = text
    return box


def _ctx(anchors):
    return {a.text: a.context for a in anchors}


@pytest.mark.parametrize(
    "heads, slots",
    [
        (("Emergency department", "Outpatient clinic"), ("[Wait time]", "[Wait time 2]")),
        (("Wheat yield", "Barley yield"), ("{{tons_a}}", "{{tons_b}}")),
    ],
)
def test_pptx_cards_side_by_side_get_their_own_heading(heads, slots):
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _box(slide.shapes, heads[0], 0.5, 1.0, height=0.5)
    _box(slide.shapes, slots[0], 0.5, 1.6)
    _box(slide.shapes, heads[1], 5.0, 1.0, height=0.5)
    _box(slide.shapes, slots[1], 5.0, 1.6)
    got = _ctx(extract_pptx(_save(prs)))
    assert got[slots[0]] == heads[0] and got[slots[1]] == heads[1]
    assert got[heads[0]] is None  # nothing static above or left of the first heading


@pytest.mark.parametrize(
    "label, slot", [("Patients seen today", "[Count]"), ("Hectares planted", "[Area]")]
)
def test_pptx_label_to_the_left_is_context(label, slot):
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _box(slide.shapes, label, 0.5, 3.0, width=2.0, height=0.5)
    _box(slide.shapes, slot, 3.0, 3.0, width=2.0, height=0.5)
    assert _ctx(extract_pptx(_save(prs)))[slot] == label


def test_pptx_above_wins_ties_and_smallest_gap_wins():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _box(slide.shapes, "Far heading", 0.5, 0.2, width=3, height=0.3)
    _box(slide.shapes, "Near heading", 0.5, 1.2, width=3, height=0.3)
    _box(slide.shapes, "[Slot]", 0.5, 2.0, width=3, height=1.0)
    assert _ctx(extract_pptx(_save(prs)))["[Slot]"] == "Near heading"
    # equal gap above and left: above wins
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _box(slide.shapes, "Above", 3.0, 1.0, width=3, height=0.5)
    _box(slide.shapes, "Left", 1.0, 2.5, width=1.5, height=0.5)
    _box(slide.shapes, "[Slot]", 3.0, 2.0, width=3, height=1.0)
    assert _ctx(extract_pptx(_save(prs)))["[Slot]"] == "Above"


@pytest.mark.parametrize("heading", ["Quarterly admissions", "Soil moisture"])
def test_pptx_heading_beyond_twenty_percent_of_the_slide_is_not_context(heading):
    prs = Presentation()  # 7.5 in tall: the limit is 1.5 in
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _box(slide.shapes, heading, 0.5, 0.2, height=0.3)
    _box(slide.shapes, "[Slot]", 0.5, 5.0)
    assert _ctx(extract_pptx(_save(prs)))["[Slot]"] is None


def test_pptx_slide_title_and_non_static_text_are_never_context():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text_frame.text = "Annual review"
    slide.shapes.title.left, slide.shapes.title.top = Inches(0.5), Inches(0.3)
    slide.shapes.title.width, slide.shapes.title.height = Inches(8), Inches(0.8)
    _box(slide.shapes, "Owner: [Name]", 0.5, 1.2, height=0.4)  # has a token
    _box(slide.shapes, "x" * 200, 0.5, 1.7, height=0.4)  # too long
    _box(slide.shapes, "[Slot]", 0.5, 2.2)
    assert _ctx(extract_pptx(_save(prs)))["[Slot]"] is None


def test_pptx_context_is_one_collapsed_line():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _box(slide.shapes, "Harvest\n  total", 0.5, 1.0, height=0.5)
    _box(slide.shapes, "[Slot]", 0.5, 1.6)
    assert _ctx(extract_pptx(_save(prs)))["[Slot]"] == "Harvest total"


@pytest.mark.parametrize("heading, slot", [("Bed capacity", "[Beds]"), ("Rainfall", "{{mm}}")])
def test_pptx_group_children_are_walked(heading, slot):
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    group = slide.shapes.add_group_shape()
    _box(group.shapes, heading, 0.5, 1.0, height=0.5)
    _box(group.shapes, slot, 0.5, 1.6)
    assert _ctx(extract_pptx(_save(prs)))[slot] == heading


def _table_deck(rows):
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    shape = slide.shapes.add_table(
        len(rows), len(rows[0]), Inches(1), Inches(1), Inches(6), Inches(2)
    )
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            shape.table.cell(r, c).text = text
    return _save(prs)


def _by_cell(anchors):
    return {(a.table.row, a.table.col): a.context for a in anchors}


@pytest.mark.parametrize(
    "rows, expected",
    [
        ([["Ward", "Occupancy", "Staff"], ["Cardiology", "[Occ]", ""]],
         {(0, 0): None, (0, 1): None, (0, 2): None, (1, 0): "Ward",
          (1, 1): "Cardiology / Occupancy", (1, 2): "Cardiology / Staff"}),
        ([["Field", "Yield"], ["North plot", "[T]"]],
         {(0, 0): None, (0, 1): None, (1, 0): "Field", (1, 1): "North plot / Yield"}),
        # records grid: no static row label, header only
        ([["Crop", "Yield"], ["", ""], ["", ""]],
         {(0, 0): None, (0, 1): None, (1, 0): "Crop", (1, 1): "Yield", (2, 0): "Crop",
          (2, 1): "Yield"}),
    ],
)  # fmt: skip
def test_pptx_table_cell_context(rows, expected):
    assert _by_cell(extract_pptx(_table_deck(rows))) == expected


def _docx_table(rows):
    doc = Document()
    table = doc.add_table(rows=len(rows), cols=len(rows[0]))
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            table.cell(r, c).text = text
    return _save(doc)


@pytest.mark.parametrize(
    "rows, expected",
    [
        ([["Employee", "Salary"], ["Dana", "[Pay]"]],
         {(0, 0): None, (0, 1): None, (1, 0): "Employee", (1, 1): "Dana / Salary"}),
        ([["Site", "Emissions"], ["", ""]],
         {(0, 0): None, (0, 1): None, (1, 0): "Site", (1, 1): "Emissions"}),
    ],
)  # fmt: skip
def test_docx_table_cell_context(rows, expected):
    assert _by_cell(extract_docx(_docx_table(rows))) == expected


def _docx_ctx(build):
    doc = Document()
    build(doc)
    return {a.text: a.context for a in extract_docx(_save(doc)) if a.kind == "paragraph"}


@pytest.mark.parametrize("label, slot", [("Employee name:", "[Name]"), ("Site address", "TBD")])
def test_docx_static_label_paragraph_is_context(label, slot):
    def build(doc):
        doc.add_paragraph(label)
        doc.add_paragraph(slot)

    assert _docx_ctx(build)[slot] == label


def test_docx_heading_table_or_long_text_before_gives_no_context():
    def heading(doc):
        doc.add_heading("Summary", level=1)
        doc.add_paragraph("[Text]")

    def table(doc):
        doc.add_paragraph("Patient name:")
        doc.add_table(rows=1, cols=1)
        doc.add_paragraph("[Text]")

    def long(doc):
        doc.add_paragraph("w" * 200)
        doc.add_paragraph("[Text]")

    def placeholder_before(doc):
        doc.add_paragraph("[First]")
        doc.add_paragraph("[Text]")

    for build in (heading, table, long, placeholder_before):
        assert _docx_ctx(build)["[Text]"] is None


def test_docx_non_placeholder_paragraph_has_no_context():
    def build(doc):
        doc.add_paragraph("Label")
        doc.add_paragraph("Finished sentence.")

    assert _docx_ctx(build)["Finished sentence."] is None
