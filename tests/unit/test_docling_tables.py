"""Docling table handling with a fake converter: real spans, multi-page joins (Phase 22)."""

from types import SimpleNamespace

import pytest
from docling_core.types.doc import (
    BoundingBox,
    DocItemLabel,
    ProvenanceItem,
    TableCell,
    TableData,
    TableItem,
    TextItem,
)

from doc_intel.ingestion import docling_parser


def _cell(text, r, c, rs=1, cs=1):
    return TableCell(
        text=text,
        start_row_offset_idx=r,
        end_row_offset_idx=r + rs,
        start_col_offset_idx=c,
        end_col_offset_idx=c + cs,
        row_span=rs,
        col_span=cs,
    )


def _table(cells, rows, cols, page):
    return TableItem(
        self_ref="#/tables/0",
        label=DocItemLabel.TABLE,
        data=TableData(table_cells=cells, num_rows=rows, num_cols=cols),
        prov=[ProvenanceItem(page_no=page, bbox=BoundingBox(l=0, t=0, r=1, b=1), charspan=(0, 0))],
    )


def _simple(header, body, page):
    cells = [_cell(h, 0, i) for i, h in enumerate(header)]
    cells += [_cell(v, r + 1, i) for r, row in enumerate(body) for i, v in enumerate(row)]
    return _table(cells, len(body) + 1, len(header), page)


def _text(text, page):
    return TextItem(
        self_ref="#/texts/0",
        label=DocItemLabel.TEXT,
        orig=text,
        text=text,
        prov=[ProvenanceItem(page_no=page, bbox=BoundingBox(l=0, t=0, r=1, b=1), charspan=(0, 0))],
    )


@pytest.fixture
def parse(monkeypatch):
    def run(items, pages=4):
        doc = SimpleNamespace(
            iterate_items=lambda: [(i, 0) for i in items], num_pages=lambda: pages
        )
        fake = SimpleNamespace(convert=lambda stream, max_num_pages: SimpleNamespace(document=doc))
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
