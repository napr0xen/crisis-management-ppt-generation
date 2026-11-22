"""Repeating cards: identical containers holding >= 2 anchor shapes get `card = set.member`."""

from io import BytesIO

import pytest
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches

from doc_intel.templates.anchors_pptx import extract


def _save(prs) -> bytes:
    out = BytesIO()
    prs.save(out)
    return out.getvalue()


def _deck():
    prs = Presentation()
    return prs, prs.slides.add_slide(prs.slide_layouts[6])


def _card(slide, left, top, texts, w=2.5, h=2.0):
    """A rectangle with one text box per entry inside it."""
    slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left), Inches(top), Inches(w), Inches(h))
    for i, text in enumerate(texts):
        box = slide.shapes.add_textbox(
            Inches(left + 0.1), Inches(top + 0.1 + 0.6 * i), Inches(w - 0.2), Inches(0.5)
        )
        box.text_frame.text = text


def _cards(prs):
    return {a.text: a.card for a in extract(_save(prs))}


@pytest.mark.parametrize(
    "texts, left_to_right",
    [
        (("[Gallery name]", "[Visitors]"), (0.5, 3.5, 6.5)),
        (("[Route]", "[Passengers]"), (0.5, 3.5, 6.5)),
    ],
)
def test_three_identical_cards_are_numbered_in_reading_order_whatever_the_draw_order(
    texts, left_to_right
):
    prs, slide = _deck()
    for left in (left_to_right[2], left_to_right[0], left_to_right[1]):  # drawn out of order
        _card(slide, left, 1.0, [f"{t} {int(left)}" for t in texts])
    got = _cards(prs)
    assert [got[f"{texts[0]} {int(x)}"] for x in left_to_right] == ["1.1", "1.2", "1.3"]
    assert [got[f"{texts[1]} {int(x)}"] for x in left_to_right] == ["1.1", "1.2", "1.3"]


@pytest.mark.parametrize("title", ["Ward", "Plot"])
def test_a_two_by_two_grid_reads_left_to_right_then_down_and_static_texts_count(title):
    prs, slide = _deck()
    spots = [(5.0, 4.0), (0.5, 1.0), (5.0, 1.0), (0.5, 4.0)]  # BR, TL, TR, BL
    for i, (x, y) in enumerate(spots):
        _card(slide, x, y, [f"{title} {i}", f"[Beds {i}]", f"[Staff {i}]"], h=2.5)
    got = _cards(prs)
    by_pos = {spots[i]: got[f"[Beds {i}]"] for i in range(4)}
    assert [by_pos[p] for p in [(0.5, 1.0), (5.0, 1.0), (0.5, 4.0), (5.0, 4.0)]] == [
        "1.1",
        "1.2",
        "1.3",
        "1.4",
    ]
    assert got["Ward 0" if title == "Ward" else "Plot 0"] == got["[Beds 0]"] == got["[Staff 0]"]


@pytest.mark.parametrize("texts", [("[Gallery name]", "[Visitors]"), ("[Route]", "[Fare]")])
def test_a_single_panel_is_not_a_card(texts):
    prs, slide = _deck()
    _card(slide, 0.5, 1.0, list(texts))
    assert set(_cards(prs).values()) == {None}


def test_two_differently_sized_panels_are_not_cards():
    prs, slide = _deck()
    _card(slide, 0.5, 1.0, ["[A]", "[B]"], w=2.5, h=2.0)
    _card(slide, 4.0, 1.0, ["[C]", "[D]"], w=3.5, h=3.0)
    assert set(_cards(prs).values()) == {None}


def test_sizes_within_two_percent_repeat_and_beyond_do_not():
    prs, slide = _deck()
    _card(slide, 0.5, 1.0, ["[A1]", "[A2]"], w=2.50)
    _card(slide, 3.5, 1.0, ["[B1]", "[B2]"], w=2.54)  # +1.6%
    _card(slide, 6.5, 1.0, ["[C1]", "[C2]"], w=2.60)  # +4%, a set of its own: one member
    got = _cards(prs)
    assert [got["[A1]"], got["[B1]"], got["[C1]"]] == ["1.1", "1.2", None]


def test_a_text_box_over_a_marker_of_the_same_size_is_not_contained_by_it():
    prs, slide = _deck()
    for left in (0.5, 3.5, 6.5):
        slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left), Inches(1), Inches(2.5), Inches(2))
        slide.shapes.add_shape(
            MSO_SHAPE.OVAL, Inches(left + 0.1), Inches(1.1), Inches(0.5), Inches(0.5)
        )
        marker = slide.shapes.add_textbox(Inches(left + 0.1), Inches(1.1), Inches(0.5), Inches(0.5))
        marker.text_frame.text = f"0{int(left)}"
        slide.shapes.add_textbox(
            Inches(left + 0.1), Inches(1.8), Inches(2.0), Inches(0.5)
        ).text_frame.text = f"[Title {int(left)}]"
    got = _cards(prs)
    assert [got[f"0{x}"] for x in (0, 3, 6)] == ["1.1", "1.2", "1.3"]  # the card, not the oval


@pytest.mark.parametrize("outside", ["Quarterly overview", "Season summary"])
def test_anchors_outside_any_card_and_two_sets_get_their_own_numbers(outside):
    prs, slide = _deck()
    slide.shapes.add_textbox(
        Inches(0.5), Inches(0.1), Inches(4), Inches(0.5)
    ).text_frame.text = outside
    for left in (0.5, 3.5):
        _card(slide, left, 1.0, [f"[A {left}]", f"[B {left}]"], w=2.5, h=2.0)
    for left in (0.5, 3.5, 6.5):
        _card(slide, left, 4.0, [f"[C {left}]", f"[D {left}]"], w=2.5, h=1.5)
    got = _cards(prs)
    assert got[outside] is None
    assert [got["[A 0.5]"], got["[A 3.5]"]] == ["1.1", "1.2"]
    assert [got["[C 0.5]"], got["[C 3.5]"], got["[C 6.5]"]] == ["2.1", "2.2", "2.3"]


def test_table_cells_of_a_contained_table_carry_the_card():
    prs, slide = _deck()
    for left in (0.5, 5.0):
        slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left), Inches(1), Inches(4), Inches(3))
        slide.shapes.add_textbox(
            Inches(left + 0.1), Inches(1.1), Inches(3.8), Inches(0.5)
        ).text_frame.text = f"[Name {left}]"
        slide.shapes.add_table(2, 2, Inches(left + 0.1), Inches(2), Inches(3.8), Inches(1.5))
    anchors = extract(_save(prs))
    assert {a.card for a in anchors if a.kind == "table_cell"} == {"1.1", "1.2"}
    assert {a.card for a in anchors if a.kind == "shape_text"} == {"1.1", "1.2"}
