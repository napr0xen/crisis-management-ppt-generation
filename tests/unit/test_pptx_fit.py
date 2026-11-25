from io import BytesIO

from pptx import Presentation
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.util import Inches, Pt

from doc_intel.templates.schemas import Constraints, IRField
from doc_intel.writers import WriteItem
from doc_intel.writers.pptx_writer import write

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _field(anchor_id, mode="replace_token", **constraints) -> IRField:
    return IRField(
        field_id="f1",
        anchor_id=anchor_id,
        section_path=[],
        label="L",
        description="",
        expected_type="text",
        required=True,
        write_mode=mode,
        constraints=Constraints(**constraints),
    )


def _box(height_in, text="[Metric]", width_in=2.0, size=17):
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(width_in), Inches(height_in))
    shape.text_frame.word_wrap = True
    shape.text_frame.auto_size = MSO_AUTO_SIZE.SHAPE_TO_FIT_TEXT
    shape.text_frame.text = text
    shape.text_frame.paragraphs[0].runs[0].font.size = Pt(size)
    out = BytesIO()
    prs.save(out)
    return out.getvalue(), f"pptx:s1:sp{shape.shape_id}"


def _body_pr(data, shape_id):
    shape = next(s for s in Presentation(BytesIO(data)).slides[0].shapes if s.shape_id == shape_id)
    return shape.text_frame._txBody.find(f"{A}bodyPr")


def _run(height_in, content):
    template, anchor = _box(height_in)
    item = WriteItem(_field(anchor, token="[Metric]"), content)
    out, _ = write(template, [item])
    return _body_pr(out, int(anchor.rsplit("sp", 1)[1]))


def test_long_marker_in_a_tight_box_shrinks_and_drops_sp_autofit():
    body = _run(0.34, ["[MISSING: Credit Transaction Count]"])
    (fit,) = body.findall(f"{A}normAutofit")
    assert 25000 <= int(fit.get("fontScale")) < 100000
    assert body.find(f"{A}spAutoFit") is None


def test_short_one_line_value_in_a_tight_box_is_untouched():
    body = _run(0.34, ["42"])
    assert body.find(f"{A}normAutofit") is None
    assert body.find(f"{A}spAutoFit") is not None


def test_multi_line_value_that_fits_a_tall_box_is_untouched():
    body = _run(4.0, ["a fairly long sentence that wraps over a few lines in this box"])
    assert body.find(f"{A}normAutofit") is None


def test_scale_never_goes_below_a_quarter():
    body = _run(0.34, ["word " * 400])
    assert body.find(f"{A}normAutofit").get("fontScale") == "25000"


def test_table_cell_writes_never_get_autofit():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    frame = slide.shapes.add_table(2, 1, Inches(1), Inches(1), Inches(1), Inches(0.3))
    frame.table.cell(1, 0).text = "[Cell]"
    out = BytesIO()
    prs.save(out)
    anchor = f"pptx:s1:sp{frame.shape_id}:r1c0"
    item = WriteItem(_field(anchor, token="[Cell]"), ["very long cell value " * 30])
    data, _ = write(out.getvalue(), [item])
    xml = Presentation(BytesIO(data)).slides[0].shapes._spTree.xml
    assert "normAutofit" not in xml
