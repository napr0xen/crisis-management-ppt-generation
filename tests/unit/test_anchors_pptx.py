from io import BytesIO

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE, PP_PLACEHOLDER
from pptx.util import Inches

from doc_intel.templates.anchors_pptx import extract


def _save(prs: Presentation) -> bytes:
    out = BytesIO()
    prs.save(out)
    return out.getvalue()


def _clone(slide, kind):
    layout_ph = next(
        p for p in slide.slide_layout.placeholders if p.placeholder_format.type == kind
    )
    slide.shapes.clone_placeholder(layout_ph)
    return slide.shapes[-1]


def _blank():
    prs = Presentation()
    return prs, prs.slides.add_slide(prs.slide_layouts[6])


def test_empty_decorative_auto_shape_is_skipped():
    prs, slide = _blank()
    slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(1), Inches(1), Inches(1), Inches(1))
    assert extract(_save(prs)) == []


def test_empty_text_box_is_skipped_but_filled_one_kept():
    prs, slide = _blank()
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(1), Inches(1))
    slide.shapes.add_textbox(Inches(1), Inches(3), Inches(1), Inches(1)).text_frame.text = "  "
    slide.shapes.add_textbox(Inches(1), Inches(5), Inches(1), Inches(1)).text_frame.text = "Hi"
    assert [a.text for a in extract(_save(prs))] == ["Hi"]


def test_slide_number_placeholder_is_skipped():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    _clone(slide, PP_PLACEHOLDER.SLIDE_NUMBER)
    assert [a.path[-1] for a in extract(_save(prs))] == ["Title 1"]


def test_footer_placeholder_with_token_is_kept_without_token_skipped():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    _clone(slide, PP_PLACEHOLDER.FOOTER).text_frame.text = "[Client] confidential"
    slide2 = prs.slides.add_slide(prs.slide_layouts[5])
    _clone(slide2, PP_PLACEHOLDER.FOOTER).text_frame.text = "Confidential"
    found = [a for a in extract(_save(prs)) if "Footer" in a.path[-1]]
    assert [(a.anchor_id[:7], a.tokens) for a in found] == [("pptx:s1", ["[Client]"])]


def test_empty_layout_placeholder_is_still_a_placeholder_anchor():
    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[1])
    anchors = extract(_save(prs))
    assert len(anchors) == 2 and all(a.is_placeholder and a.text for a in anchors)


def test_pos_is_shape_centre_percent():
    prs, slide = _blank()
    w, h = prs.slide_width, prs.slide_height
    box = slide.shapes.add_textbox(0, 0, Inches(2), Inches(1))
    box.left, box.top = int((w - Inches(2)) / 2), int((h - Inches(1)) / 2)
    box.text_frame.text = "mid"
    assert extract(_save(prs))[0].pos == (50, 50)


def test_pos_is_clamped_to_slide():
    prs, slide = _blank()
    slide.shapes.add_textbox(
        -Inches(5), prs.slide_height, Inches(1), Inches(1)
    ).text_frame.text = "x"
    assert extract(_save(prs))[0].pos == (0, 100)


def test_table_cells_carry_the_table_pos():
    prs, slide = _blank()
    slide.shapes.add_table(2, 2, 0, 0, prs.slide_width, prs.slide_height)
    anchors = extract(_save(prs))
    assert len(anchors) == 4 and {a.pos for a in anchors} == {(50, 50)}


def test_skipped_empty_shape_does_not_shift_duplicate_id_key():
    prs, slide = _blank()
    for text in ("", "second"):
        s = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(1), Inches(1))
        s.text_frame.text = text
        s._element.xpath("./*[1]/p:cNvPr")[0].set("id", "10")
    assert [a.anchor_id for a in extract(_save(prs))] == ["pptx:s1:sp10_2"]
