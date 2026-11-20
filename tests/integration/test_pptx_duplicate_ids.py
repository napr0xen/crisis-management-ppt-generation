"""Real decks reuse shape ids: the extractor keys them apart and the writer resolves the keys."""

from io import BytesIO

import pytest
from pptx import Presentation
from pptx.util import Inches

from doc_intel.templates.anchors_pptx import extract
from doc_intel.writers.pptx_writer import write
from tests.integration.test_writers_pptx import _item


def _deck(*kinds: str) -> bytes:
    """One slide; each kind ('text' or 'table') becomes a shape forced to shape_id 10."""
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for i, kind in enumerate(kinds):
        if kind == "text":
            shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1))
            shape.text_frame.text = f"box{i}"
        else:
            shape = slide.shapes.add_table(2, 1, Inches(1), Inches(3), Inches(3), Inches(1))
            shape.table.cell(0, 0).text = f"head{i}"
            shape.table.cell(1, 0).text = f"cell{i}"
        shape._element.xpath("./*[1]/p:cNvPr")[0].set("id", "10")
    out = BytesIO()
    prs.save(out)
    return out.getvalue()


def _ids(data: bytes) -> list[str]:
    return [a.anchor_id for a in extract(data)]


def _texts(data: bytes) -> list[str]:
    shapes = Presentation(BytesIO(data)).slides[0].shapes
    return [s.text_frame.text if s.has_text_frame else s.table.cell(1, 0).text for s in shapes]


def test_text_box_and_table_sharing_an_id_keep_standard_ids():
    ids = _ids(_deck("text", "table"))
    assert ids == ["pptx:s1:sp10", "pptx:s1:sp10:r0c0", "pptx:s1:sp10:r1c0"]


def test_two_text_boxes_sharing_an_id_get_a_suffix():
    assert _ids(_deck("text", "text")) == ["pptx:s1:sp10", "pptx:s1:sp10_2"]


def test_two_tables_sharing_an_id_get_a_suffix():
    assert _ids(_deck("table", "table")) == [
        "pptx:s1:sp10:r0c0",
        "pptx:s1:sp10:r1c0",
        "pptx:s1:sp10_2:r0c0",
        "pptx:s1:sp10_2:r1c0",
    ]


def test_text_box_and_table_sharing_an_id_both_land_on_their_shape():
    out, _ = write(
        _deck("text", "table"),
        [_item("pptx:s1:sp10", ["NEW"]), _item("pptx:s1:sp10:r1c0", ["CELL"], "fill_cell")],
    )
    assert _texts(out) == ["NEW", "CELL"]


def test_second_text_box_with_the_same_id_is_written_not_the_first():
    out, _ = write(_deck("text", "text"), [_item("pptx:s1:sp10_2", ["NEW"])])
    assert _texts(out) == ["box0", "NEW"]


@pytest.mark.parametrize(
    ("kinds", "anchor"),
    [
        (("text", "text"), "pptx:s1:sp10_3"),
        (("text",), "pptx:s1:sp10:r0c0"),
        (("table",), "pptx:s1:sp10"),
    ],
)
def test_unresolvable_key_or_capability_raises(kinds, anchor):
    with pytest.raises(KeyError, match="anchor_unresolved"):
        write(_deck(*kinds), [_item(anchor, ["x"])])


@pytest.mark.parametrize("kinds", [("text", "table"), ("text", "text"), ("table", "table")])
def test_every_extracted_id_round_trips_through_the_writer(kinds):
    deck = _deck(*kinds)
    ids = _ids(deck)
    assert len(ids) == len(set(ids))
    for anchor_id in ids:
        write(deck, [_item(anchor_id, ["x"], "fill_cell" if ":r" in anchor_id else "replace_text")])
