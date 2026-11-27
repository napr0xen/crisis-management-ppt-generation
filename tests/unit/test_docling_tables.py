"""Docling table handling with a fake converter: real spans, multi-page joins (Phase 22)."""

from types import SimpleNamespace

import pytest
from docling_core.types.doc import (
    BoundingBox,
    ContentLayer,
    CoordOrigin,
    DocItemLabel,
    ProvenanceItem,
    TableCell,
    TableData,
    TableItem,
    TextItem,
)
from docling_core.types.doc.page import BoundingRectangle, TextCell

from doc_intel.ingestion import docling_parser


def _cell(text, r, c, rs=1, cs=1, header=None):
    return TableCell(
        text=text,
        column_header=r == 0 if header is None else header,  # TableFormer flags the header row
        start_row_offset_idx=r,
        end_row_offset_idx=r + rs,
        start_col_offset_idx=c,
        end_col_offset_idx=c + cs,
        row_span=rs,
        col_span=cs,
    )


PAGE_H = 800.0


def _prov(page, box):
    x0, t, r, b = box
    return ProvenanceItem(
        page_no=page,
        bbox=BoundingBox(l=x0, t=t, r=r, b=b, coord_origin=CoordOrigin.TOPLEFT),
        charspan=(0, 0),
    )


MID = (50, 300, 150, 320)


def _table(cells, rows, cols, page):
    return TableItem(
        self_ref="#/tables/0",
        label=DocItemLabel.TABLE,
        data=TableData(table_cells=cells, num_rows=rows, num_cols=cols),
        prov=[_prov(page, (50, 100, 500, 400))],
    )


def _simple(header, body, page):
    cells = [_cell(h, 0, i) for i, h in enumerate(header)]
    cells += [_cell(v, r + 1, i) for r, row in enumerate(body) for i, v in enumerate(row)]
    return _table(cells, len(body) + 1, len(header), page)


def _text(text, page, box=MID, label=DocItemLabel.TEXT, layer=ContentLayer.BODY):
    return TextItem(
        self_ref="#/texts/0",
        label=label,
        orig=text,
        text=text,
        prov=[_prov(page, box)],
        content_layer=layer,
    )


def _furniture(text, page, box, label):
    return _text(text, page, box, label, ContentLayer.FURNITURE)


def _cells(specs):
    return [
        TextCell(
            index=i,
            text=t,
            orig=t,
            from_ocr=False,
            rect=BoundingRectangle.from_bounding_box(
                BoundingBox(l=b[0], t=b[1], r=b[2], b=b[3], coord_origin=CoordOrigin.TOPLEFT)
            ),
        )
        for i, (t, b) in enumerate(specs)
    ]


@pytest.fixture
def parse(monkeypatch):
    def run(items, pages=4, cells=()):
        doc = SimpleNamespace(
            # like Docling: page headers/footers (furniture) only when asked for
            iterate_items=lambda included_content_layers=frozenset({ContentLayer.BODY}): [
                (i, 0) for i in items if i.content_layer in included_content_layers
            ],
            num_pages=lambda: pages,
            pages={p: SimpleNamespace(size=SimpleNamespace(height=PAGE_H)) for p in range(1, 9)},
        )
        page = SimpleNamespace(
            page_no=1,
            size=SimpleNamespace(height=PAGE_H),
            predictions=SimpleNamespace(
                layout=SimpleNamespace(clusters=[SimpleNamespace(cells=_cells(cells))])
            ),
        )
        result = SimpleNamespace(document=doc, pages=[page])
        fake = SimpleNamespace(convert=lambda stream, max_num_pages: result)
        monkeypatch.setattr(docling_parser, "_converter", lambda: fake)
        return docling_parser.parse_pdf(b"x").elements

    return run


def test_col_span_header_appears_once(parse):
    cells = [_cell("Group", 0, 0, cs=2), _cell("Other", 0, 2)]
    cells += [_cell(v, 1, i) for i, v in enumerate(["a", "b", "c"])]
    (table,) = parse([_table(cells, 2, 3, 1)])
    assert table.rows == [["Group", "", "Other"], ["a", "b", "c"]]


def test_row_span_cell_appears_once(parse):
    cells = [_cell("K", 0, 0), _cell("V", 0, 1), _cell("North", 1, 0, rs=2)]
    cells += [_cell("1", 1, 1), _cell("2", 2, 1)]
    (table,) = parse([_table(cells, 3, 2, 1)])
    assert table.rows == [["K", "V"], ["North", "1"], ["", "2"]]


def test_span_clipped_to_table_bounds(parse):
    cells = [_cell("Wide", 0, 0, cs=5), _cell("x", 1, 0)]
    (table,) = parse([_table(cells, 2, 2, 1)])
    assert table.rows == [["Wide", ""], ["x", ""]]


def test_same_header_consecutive_pages_join(parse):
    first = _simple(["Item", "Qty"], [["a", "1"]], 3)
    second = _simple(["Item", "Qty"], [["b", "2"]], 4)
    (table,) = parse([first, second])
    assert table.rows == [["Item", "Qty"], ["a", "1"], ["b", "2"]]
    assert table.page_number == 3


def test_three_page_chain_joins(parse):
    items = [_simple(["Item", "Qty"], [[c, "1"]], p) for p, c in ((2, "a"), (3, "b"), (4, "c"))]
    (table,) = parse(items)
    assert table.rows == [["Item", "Qty"], ["a", "1"], ["b", "1"], ["c", "1"]]
    assert table.page_number == 2


def test_different_header_no_join(parse):
    items = [_simple(["Item", "Qty"], [["a", "1"]], 3), _simple(["Name", "Qty"], [["b", "2"]], 4)]
    assert len(parse(items)) == 2


def test_non_consecutive_pages_no_join(parse):
    items = [_simple(["Item", "Qty"], [["a", "1"]], 3), _simple(["Item", "Qty"], [["b", "2"]], 5)]
    assert len(parse(items)) == 2


def test_text_between_tables_no_join(parse):
    items = [
        _simple(["Item", "Qty"], [["a", "1"]], 3),
        _text("A note", 3),
        _simple(["Item", "Qty"], [["b", "2"]], 4),
    ]
    assert [e.element_type for e in parse(items)] == ["table", "text", "table"]


# ---- Phase 27 layout pass ---------------------------------------------------------------------
def at(x, y, w=80, h=14):
    return (x, y, x + w, y + h)


def test_adjacent_label_value_pair_is_kv(parse):
    items = [_text("Previous balance", 1, at(50, 200)), _text("1.234,56 $", 1, at(300, 201))]
    (kv,) = parse(items)
    assert (kv.element_type, kv.region) == ("kv", "kv")
    assert (kv.label, kv.value, kv.text) == (
        "Previous balance",
        "1.234,56 $",
        "Previous balance: 1.234,56 $",
    )
    assert kv.bbox == (50, 200, 380, 215)


def test_label_with_value_below_is_kv(parse):
    items = [_text("Credit total:", 1, at(50, 200)), _text("5,00", 1, at(60, 220))]
    (kv,) = parse(items)
    assert kv.label == "Credit total" and kv.value == "5,00"


def test_multi_column_summary_pairs_by_x(parse):
    items = [
        _text("Alpha", 1, at(50, 200, 60)),
        _text("Beta", 1, at(200, 200, 60)),
        _text("Gamma", 1, at(350, 200, 60)),
        _text("1,00", 1, at(52, 230, 50)),
        _text("2,00", 1, at(202, 231, 50)),
        _text("3,00", 1, at(352, 230, 50)),
    ]
    kvs = parse(items)
    assert [(k.region, k.text) for k in kvs] == [
        ("summary", "Alpha: 1,00"),
        ("summary", "Beta: 2,00"),
        ("summary", "Gamma: 3,00"),
    ]


def test_banner_row_becomes_caption(parse):
    cells = [_cell("Movements", 0, 0)]
    cells += [_cell(v, 1, i) for i, v in enumerate(["Item", "Qty"])]
    cells += [_cell(v, 2, i) for i, v in enumerate(["a", "1"])]
    (table,) = parse([_table(cells, 3, 2, 1)])
    assert table.caption == "Movements"
    assert table.rows == [["Item", "Qty"], ["a", "1"]]


def test_split_table_joins_across_header_footer_carry_forward(parse):
    items = [
        _text("Statement title", 1, at(50, 20), DocItemLabel.PAGE_HEADER),
        _simple(["Item", "Qty"], [["a", "1"]], 1),
        _text("Statement title", 2, at(50, 20), DocItemLabel.PAGE_HEADER),
        _text("Notice text", 2, at(50, 770), DocItemLabel.PAGE_FOOTER),
        _text("Balance brought forward 9,99", 2, at(50, 100)),
        _simple(["Item", "Qty"], [["b", "2"]], 2),
    ]
    out = parse(items)
    tables = [e for e in out if e.element_type == "table"]
    assert len(tables) == 1
    assert tables[0].rows == [["Item", "Qty"], ["a", "1"], ["b", "2"]]


def test_repeated_bottom_band_text_is_footer(parse):
    out = parse([_text("Notice 1", 1, at(50, 760)), _text("Notice 2", 2, at(50, 760))])
    assert [e.region for e in out] == ["footer", "footer"]


def test_docling_page_header_label_is_header(parse):
    (e,) = parse([_text("Acme Statement", 1, at(50, 300), DocItemLabel.PAGE_HEADER)])
    assert e.region == "header"


def test_table_region_and_body_default(parse):
    out = parse([_simple(["A", "B"], [["1", "2"]], 1), _text("Some prose here", 1, at(50, 500))])
    assert [e.region for e in out] == ["table", "body"]


def test_text_above_table_on_its_page_does_not_block_join(parse):
    # emitted after the table by reading order but located above it
    items = [
        _simple(["Item", "Qty"], [["a", "1"]], 1),
        _text("Side panel", 1, at(50, 20 + 60)),
        _simple(["Item", "Qty"], [["b", "2"]], 2),
    ]
    assert len([e for e in parse(items) if e.element_type == "table"]) == 1


def test_kept_between_text_does_not_block_next_part_of_chain(parse):
    items = [
        _simple(["Item", "Qty"], [["a", "1"]], 1),
        _text("Balance carried forward 1,00", 1, at(50, 600)),
        _simple(["Item", "Qty"], [["b", "2"]], 2),
        _text("Balance carried forward 2,00", 2, at(50, 600)),
        _simple(["Item", "Qty"], [["c", "3"]], 3),
    ]
    assert len([e for e in parse(items) if e.element_type == "table"]) == 1


def _banner_table(body, page=1, banner="Closing"):
    cells = [_cell(banner, 0, 0)]
    cells += [_cell(v, r + 1, i) for r, row in enumerate(body) for i, v in enumerate(row)]
    return _table(cells, len(body) + 1, 2, page)


def test_headerless_banner_table_keeps_amount_row_as_data(parse):
    (t,) = parse([_banner_table([["Fees", "10,00 $"], ["Total charged", "10,00 $"]])])
    assert t.caption == "Closing"
    assert t.rows == [["", ""], ["Fees", "10,00 $"], ["Total charged", "10,00 $"]]


def test_headerless_tables_do_not_join(parse):
    items = [_banner_table([["Fees", "1,00"], ["Other", "2,00"]], p) for p in (1, 2)]
    assert len(parse(items)) == 2


def _merged(cells, width=360):
    items = [
        _text("Alpha Beta Gamma", 1, at(50, 200, width)),
        _text("1,00", 1, at(52, 230, 50)),
        _text("2,00", 1, at(202, 231, 50)),
        _text("3,00", 1, at(352, 230, 50)),
    ]
    return items, cells


def test_merged_label_line_split_by_cell_geometry(parse):
    items, cells = _merged(
        [
            ("Alpha", at(50, 200, 60)),
            ("Beta", at(200, 200, 60)),
            ("Gamma", at(350, 200, 60)),
        ]
    )
    out = parse(items, cells=cells)
    assert [(k.region, k.text) for k in out] == [
        ("summary", "Alpha: 1,00"),
        ("summary", "Beta: 2,00"),
        ("summary", "Gamma: 3,00"),
    ]


def test_merged_label_cell_straddling_columns_fails_closed(parse):
    items, cells = _merged([("Alpha Beta", at(50, 200, 200)), ("Gamma", at(350, 200, 60))])
    out = parse(items, cells=cells)
    assert all(e.element_type == "text" for e in out) and len(out) == 4


def test_merged_label_without_cells_is_not_paired_with_one_value(parse):
    items, _ = _merged([])
    assert all(e.element_type == "text" for e in parse(items))


def test_merged_label_whose_bbox_covers_only_its_first_cell(parse):
    # Docling keeps the merged text but the bbox of the first column only
    items, cells = _merged(
        [("Alpha", at(50, 200, 60)), ("Beta", at(200, 200, 60)), ("Gamma", at(350, 200, 60))], 60
    )
    out = parse(items, cells=cells)
    assert [k.text for k in out] == ["Alpha: 1,00", "Beta: 2,00", "Gamma: 3,00"]


# ---- review fixes -------------------------------------------------------------------------------
def _joined(parse, *items):
    return len([e for e in parse(list(items)) if e.element_type == "table"]) == 1


def test_different_captions_do_not_join(parse):
    a = _banner_table([["Item", "Qty"], ["a", "1"]], 1, "Account 111")
    b = _banner_table([["Item", "Qty"], ["b", "2"]], 2, "Account 222")
    assert not _joined(parse, a, b)


def _t(page, c):
    return _simple(["Item", "Qty"], [[c, "1"]], page)


def _hdr(text, page):
    return _text(text, page, at(50, 20), DocItemLabel.PAGE_HEADER)


def test_one_off_header_blocks_join(parse):
    assert not _joined(parse, _t(1, "a"), _hdr("Account 222", 2), _t(2, "b"))


def test_page_number_header_allows_join(parse):
    assert _joined(parse, _t(1, "a"), _hdr("Page 2 of 3", 2), _t(2, "b"))


def test_running_header_allows_join(parse):
    items = [_hdr("Acme", 1), _t(1, "a"), _hdr("Acme", 2), _t(2, "b")]
    assert _joined(parse, *items)


@pytest.mark.parametrize(
    "line", ["Continued on next page", "Continued overleaf", "Continúa en la página siguiente"]
)
def test_continuation_line_allows_join(parse, line):
    assert _joined(parse, _t(1, "a"), _text(line, 2, at(50, 100)), _t(2, "b"))


def test_kv_label_over_two_value_columns_is_not_paired(parse):
    items = [
        _text("Revenue", 1, at(50, 200, 60)),
        _text("2023", 1, at(52, 230, 40)),
        _text("2024", 1, at(202, 231, 40)),
    ]
    assert all(e.element_type == "text" for e in parse(items))


def test_currency_code_between_label_and_amount_is_not_paired(parse):
    items = [
        _text("Balance", 1, at(50, 200, 50)),
        _text("USD", 1, at(150, 200, 30)),
        _text("1,234.00", 1, at(250, 200, 50)),
    ]
    assert all(e.element_type == "text" for e in parse(items))


def test_year_row_under_banner_keeps_real_header(parse):
    cells = [_cell("Income (EUR)", 0, 0)]
    cells += [_cell(v, 1, i) for i, v in enumerate(["Item", "2023", "2024"])]
    cells += [_cell(v, 2, i) for i, v in enumerate(["a", "1", "2"])]
    (t,) = parse([_table(cells, 3, 3, 1)])
    assert t.caption == "Income (EUR)" and t.rows[0] == ["Item", "2023", "2024"]


def test_header_row_with_empty_cell_is_not_a_banner(parse):
    cells = [_cell("Item", 0, 0), _cell("", 0, 1)]
    cells += [_cell(v, 1, i) for i, v in enumerate(["Alice", "Bob"])]
    cells += [_cell(v, 2, i) for i, v in enumerate(["Carol", "Dan"])]
    (t,) = parse([_table(cells, 3, 2, 1)])
    assert t.caption is None and t.rows[0] == ["Item", ""]


def test_one_off_bottom_band_text_stays_body(parse):
    (e,) = parse([_text("Source: survey", 1, at(50, 760))])
    assert e.region == "body"


def test_page_number_bottom_band_is_footer(parse):
    out = parse([_text("Page 1 of 2", 1, at(50, 760)), _text("Page 2 of 2", 2, at(50, 760))])
    assert [e.region for e in out] == ["footer", "footer"]


def test_one_off_page_number_bottom_band_is_footer(parse):
    (e,) = parse([_text("Page 1", 1, at(50, 760))])
    assert e.region == "footer"


def test_merged_label_split_fails_closed_on_a_crowded_line(parse):
    # a crafted line of thousands of cells must not cost cubic time: over the cap, no split
    n = docling_parser.MAX_LINE_CELLS + 1
    filler = [("z", at(500 + i, 200, 0.5)) for i in range(n)]
    items, cells = _merged(
        [("Alpha", at(50, 200, 60)), ("Beta", at(200, 200, 60)), ("Gamma", at(350, 200, 60))]
    )
    out = parse(items, cells=[*cells, *filler])
    assert all(e.element_type == "text" for e in out)


def test_docling_furniture_headers_and_footers_are_parsed(parse):
    out = parse(
        [
            _furniture("Ref: QR-7 v1.2", 1, at(50, 20), DocItemLabel.PAGE_HEADER),
            _text("Body prose here", 1, at(50, 300)),
            _furniture("Issuer Ltd, Tax ID 99", 1, at(50, 770), DocItemLabel.PAGE_FOOTER),
        ]
    )
    assert [(e.text, e.region) for e in out] == [
        ("Ref: QR-7 v1.2", "header"),
        ("Body prose here", "body"),
        ("Issuer Ltd, Tax ID 99", "footer"),
    ]


def test_running_furniture_header_keeps_one_copy_and_the_table_still_joins(parse):
    items = []
    for p, c in ((1, "a"), (2, "b"), (3, "c")):
        items += [
            _furniture("Bank X statement", p, at(50, 20), DocItemLabel.PAGE_HEADER),
            _simple(["Item", "Qty"], [[c, "1"]], p),
            _furniture(f"Page {p}/3", p, at(50, 770), DocItemLabel.PAGE_FOOTER),
        ]
    out = parse(items, pages=3)
    tables = [e for e in out if e.element_type == "table"]
    assert [t.rows for t in tables] == [[["Item", "Qty"], ["a", "1"], ["b", "1"], ["c", "1"]]]
    assert [e.page_number for e in out if e.text == "Bank X statement"] == [1]


def test_table_without_a_docling_column_header_is_headerless(parse):
    body = [["Prepared by", "R. Lee, Head"], ["Contributors", "Team A"]]
    cells = [_cell(v, r, c, header=False) for r, row in enumerate(body) for c, v in enumerate(row)]
    (table,) = parse([_table(cells, 2, 2, 1)])
    assert table.rows == [["", ""], *body]


def test_joined_table_records_the_page_of_each_row(parse):
    items = [_simple(["Item", "Qty"], [[c, "1"], [c + c, "2"]], p) for p, c in ((2, "a"), (3, "b"))]
    (table,) = parse(items)
    assert table.page_number == 2
    assert table.row_pages == [2, 2, 3, 3]


def test_single_page_table_has_no_row_pages(parse):
    (table,) = parse([_simple(["Item", "Qty"], [["a", "1"]], 2)])
    assert table.row_pages is None


def test_banner_table_without_column_header_flags_keeps_its_caption_and_one_header(parse):
    cells = [_cell("Closing statement", 0, 0, cs=2, header=False)]
    body = [["Fees", "2,275.26"], ["Total charged", "2,275.26"]]
    cells += [
        _cell(v, r + 1, c, header=False) for r, row in enumerate(body) for c, v in enumerate(row)
    ]
    (table,) = parse([_table(cells, 3, 2, 4)])
    assert table.caption == "Closing statement"
    assert table.rows == [["", ""], *body]
