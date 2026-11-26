"""Deterministic aggregations over structured table rows (ADR-018)."""

import uuid

import pytest

from doc_intel.embeddings.schemas import Hit
from doc_intel.ingestion.schemas import TableRow
from doc_intel.mapping.derived import Computed, Table, compute, row_line, tables
from doc_intel.templates.schemas import Derivation

DOC = uuid.uuid4()
HEAD = ("Date", "Description", "Debit", "Credit")


def _hit(rows, index=0, element_type="table", doc=DOC, with_rows=True, text=None):
    lines = ["| a | b |", "| --- | --- |"] + [f"| line {r[0]} |" for r in rows]
    return Hit(
        chunk_id=uuid.uuid4(),
        document_id=doc,
        filename="s.pdf",
        page_number=1,
        section_heading=None,
        element_type=element_type,
        text=text or "\n".join(lines),
        score=0.0,
        chunk_index=index,
        rows=[TableRow(id=i, cells=c) for i, c in rows] if with_rows else None,
    )


def _rows(prefix, cells, start=1):
    return [(f"{prefix}-R{n:02d}", c) for n, c in enumerate(cells, start)]


def _table(cells, headers=None):
    hit = _hit(_rows("P1-T1", cells))
    (t,) = tables([hit])
    return t


def _d(**kw):
    return Derivation(**kw)


def val(t, expected="currency", **kw):
    out = compute(t, _d(**kw), expected)
    assert isinstance(out, Computed), out
    return out.value


def cells(*pairs):
    return [{"Description": d, "Amount": a} for d, a in pairs]


# ---- tables -------------------------------------------------------------------------------------
def test_tables_groups_dedupes_and_sorts():
    a = _hit(_rows("P1-T1", [{"X": "1"}, {"X": "2"}]), 0)
    b = _hit(_rows("P1-T1", [{"X": "2"}, {"X": "3"}], 2), 1)  # neighbor repeats R02
    other = _hit(_rows("P1-T2", [{"Y": "9"}]), 2)
    ts = tables([b, other, a])
    t1 = next(t for t in ts if t.table_id == "P1-T1")
    assert [r.id for _, r in t1.rows] == ["P1-T1-R01", "P1-T1-R02", "P1-T1-R03"]
    assert t1.headers == ("X",) and t1.complete and t1.document_id == DOC
    assert len(ts) == 2


def test_tables_without_page_prefix():
    (t,) = tables([_hit(_rows("T2", [{"X": "1"}]))])
    assert t.table_id == "T2" and t.complete


def test_gap_in_row_numbers_is_incomplete():
    h = _hit([("P1-T1-R01", {"X": "1"}), ("P1-T1-R03", {"X": "3"})])
    assert not tables([h])[0].complete


def test_missing_first_row_is_incomplete():
    assert not tables([_hit(_rows("P1-T1", [{"X": "1"}], 2))])[0].complete


def test_adjacent_plain_text_table_chunk_is_incomplete():
    rows = _hit(_rows("P1-T1", [{"X": "1"}]), index=3)
    plain = _hit([], index=4, with_rows=False)
    assert not tables([rows, plain])[0].complete
    far = _hit([], index=9, with_rows=False)
    assert tables([rows, far])[0].complete
    other_doc = _hit([], index=4, with_rows=False, doc=uuid.uuid4())
    assert tables([rows, other_doc])[0].complete


def test_hits_without_rows_make_no_table():
    assert tables([_hit([], with_rows=False)]) == []


# ---- number parsing and ops ---------------------------------------------------------------------
EU = cells(("a", "1.829.914,84 $"), ("b", "402.031,45 $"), ("c", "752,76 $"), ("d", "-"), ("e", ""))


def test_sum_european_with_affix_and_empty_cells():
    assert val(_table(EU), op="sum", column="Amount") == "2.232.699,05 $"


def test_sum_us_format():
    t = _table(cells(("a", "1,829,914.84"), ("b", "0.16"), ("c", "5")))
    assert val(t, op="sum", column="Amount") == "1,829,920.00"


def test_plain_digits_sum_has_no_separators():
    assert (
        val(_table(cells(("a", "1200"), ("b", "34"))), "number", op="sum", column="Amount")
        == "1234"
    )


def test_ambiguous_cell_resolved_by_unambiguous_cell():
    t = _table(cells(("a", "12.000"), ("b", "1.829.914,84")))
    assert val(t, op="sum", column="Amount") == "1.841.914,84"


def test_negative_leading_and_trailing_minus():
    t = _table(cells(("a", "-5,50"), ("b", "2,25-"), ("c", "1,00")))
    assert val(t, op="sum", column="Amount") == "-6,75"


def test_mixed_conventions_fail():
    t = _table(cells(("a", "1.829.914,84"), ("b", "1,829,914.84")))
    assert compute(t, _d(op="sum", column="Amount"), "currency") == "ambiguous_number_format"


def test_only_ambiguous_cells_fail():
    t = _table(cells(("a", "12.000"), ("b", "3,000")))
    assert compute(t, _d(op="sum", column="Amount"), "currency") == "ambiguous_number_format"


def test_two_numbers_in_a_cell_is_unparseable():
    t = _table(cells(("a", "10 20"), ("b", "5")))
    assert compute(t, _d(op="sum", column="Amount"), "number") == "cell_unparseable"


def test_diff_two_columns_and_min_max():
    t = _table([{"In": "10,50", "Out": "3,25"}, {"In": "1,00", "Out": ""}])
    assert val(t, op="diff", column="In", minus="Out") == "8,25"
    assert val(t, op="min", column="In") == "1,00"
    assert val(t, op="max", column="In") == "10,50"


def test_diff_by_tags():
    t = _table(cells(("a", "402.031,45 CR"), ("b", "181.223,68 DR"), ("c", "1,00 CR")))
    assert val(t, op="diff", column="Amount") == "220.808,77"
    assert val(t, op="diff", column="Amount") == val(t, op="diff", column="Amount")


def test_diff_negative_result():
    t = _table(cells(("a", "1,00 CR"), ("b", "3,50 DR")))
    assert val(t, op="diff", column="Amount") == "-2,50"


def test_diff_untagged_cell_fails():
    t = _table(cells(("a", "1,00 CR"), ("b", "3,50")))
    assert compute(t, _d(op="diff", column="Amount"), "currency") == "untagged_cell"


def test_tag_filter_keeps_one_side():
    t = _table(cells(("a", "10,00 CR"), ("b", "3,00 DR"), ("c", "2,00 CR")))
    assert val(t, op="sum", column="Amount", tag="CR") == "12,00"
    out = compute(t, _d(op="sum", column="Amount", tag="DR"), "currency")
    assert isinstance(out, Computed) and out.value == "3,00"
    assert [r.id for _, r, _ in out.cited] == ["P1-T1-R02"]


def test_count_counts_nonempty_cells_and_cites_them():
    out = compute(_table(EU), _d(op="count", column="Amount"), "number")
    assert isinstance(out, Computed) and out.value == "3" and len(out.cited) == 3


def test_where_and_exclude_rows():
    rows = [
        {"Type": "Fee", "Amount": "1,00"},
        {"Type": "fee ", "Amount": "2,00"},
        {"Type": "Tax", "Amount": "4,00"},
    ]
    t = _table(rows)
    assert val(t, op="sum", column="Amount", where={"Type": "FEE"}) == "3,00"
    assert val(t, op="sum", column="Amount", exclude_rows=["P1-T1-R03"]) == "3,00"
    out = compute(t, _d(op="sum", column="Amount", where={"Type": "Nope"}), "currency")
    assert out == "no_rows"


def test_group_sum_sorted_top_n_with_ties_and_citations():
    rows = [
        {"Cat": "A", "Amount": "1,00"},
        {"Cat": "B", "Amount": "5,00"},
        {"Cat": "A ", "Amount": "1,00"},
        {"Cat": "C", "Amount": "2,00"},
        {"Cat": "D", "Amount": "2,00"},
    ]
    t = _table(rows)
    spec = _d(op="group_sum", column="Amount", group_by="Cat", top_n=3)
    out = compute(t, spec, "table")
    assert isinstance(out, Computed)
    assert out.value == [["B", "5,00"], ["A", "2,00"], ["C", "2,00"]]  # A, C, D tie: A seen first
    assert [(r.id, i) for _, r, i in out.cited] == [
        ("P1-T1-R02", 0),
        ("P1-T1-R01", 1),
        ("P1-T1-R03", 1),
        ("P1-T1-R04", 2),
    ]
    lst = compute(t, spec, "bullet_list")
    assert isinstance(lst, Computed) and lst.value[0] == "B: 5,00"


def test_group_label_missing():
    t = _table([{"Cat": "", "Amount": "1,00"}])
    assert compute(t, _d(op="group_sum", column="Amount", group_by="Cat"), "table") == (
        "group_label_missing"
    )


def test_dollar_prefix_affix_kept_and_differing_affix_dropped():
    assert val(_table(cells(("a", "$1.50"), ("b", "$2.25"))), op="sum", column="Amount") == "$3.75"
    assert val(_table(cells(("a", "$1.50"), ("b", "2.25"))), op="sum", column="Amount") == "3.75"


# ---- failures -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "spec",
    [
        {"op": "sum", "column": "Nope"},
        {"op": "diff", "column": "Amount", "minus": "Nope"},
        {"op": "group_sum", "column": "Amount", "group_by": "Nope"},
        {"op": "sum", "column": "Amount", "where": {"Nope": "x"}},
    ],
)
def test_column_missing(spec):
    expected = "table" if spec["op"] == "group_sum" else "currency"
    assert compute(_table(EU), _d(**spec), expected) == "column_missing"


def test_rows_incomplete_fails_closed():
    t = _table(EU)
    t = Table(t.document_id, t.table_id, t.headers, t.rows, False)
    assert compute(t, _d(op="sum", column="Amount"), "currency") == "rows_incomplete"


def test_op_type_mismatch():
    t = _table(EU)
    assert compute(t, _d(op="sum", column="Amount"), "text") == "op_type_mismatch"
    assert compute(t, _d(op="count", column="Amount"), "currency") == "op_type_mismatch"
    assert compute(t, _d(op="group_sum", column="Amount", group_by="Description"), "text") == (
        "op_type_mismatch"
    )


# ---- Derivation schema --------------------------------------------------------------------------
def test_derivation_validation():
    with pytest.raises(ValueError):
        Derivation(op="group_sum", column="a")
    with pytest.raises(ValueError):
        Derivation(op="sum", column="a", group_by="b")
    with pytest.raises(ValueError):
        Derivation(op="sum", column="a", minus="b")
    with pytest.raises(ValueError):
        Derivation(op="group_sum", column="a", group_by="b", top_n=0)
    Derivation(op="diff", column="a", minus="b")


# ---- row_line -----------------------------------------------------------------------------------
def test_row_line():
    h = _hit(_rows("P1-T1", [{"X": "1"}, {"X": "2"}]))
    assert row_line(h, h.rows[1]) == "| line P1-T1-R02 |"
    broken = h.model_copy(update={"text": "| a |"})
    assert row_line(broken, h.rows[0]) is None


def test_compute_carries_the_exact_decimal():
    t = _table(cells(("a", "1.829.914"), ("b", "12.000")))
    out = compute(t, _d(op="min", column="Amount"), "currency")
    assert isinstance(out, Computed) and out.value == "12.000" and out.number == 12000
    out = compute(t, _d(op="count", column="Amount"), "number")
    assert out.number == 2


def test_parenthesis_negative_fails_closed():
    t = _table(cells(("a", "(1.234,56)"), ("b", "5,00")))
    assert compute(t, _d(op="sum", column="Amount"), "currency") == "cell_unparseable"


def test_mixed_tags_without_a_tag_filter_fail_closed():
    t = _table(cells(("a", "10,00 CR"), ("b", "4,00 DR")))
    assert compute(t, _d(op="sum", column="Amount"), "currency") == "mixed_tags"
    assert compute(t, _d(op="sum", column="Amount", tag="CR"), "currency").value == "10,00"
    assert compute(t, _d(op="diff", column="Amount"), "currency").value == "6,00"


def test_diff_without_minus_rejects_a_tag():
    with pytest.raises(ValueError):
        Derivation(op="diff", column="a", tag="CR")


def test_row_line_matches_every_real_chunk_shape_and_format_hit():
    from doc_intel.ingestion.chunker import chunk
    from doc_intel.ingestion.schemas import ParsedDocument, ParsedElement
    from doc_intel.mapping.retriever import format_hit

    rows = [["Item", "Amount"], *[[f"item {n}", f"{n},00"] for n in range(1, 13)]]
    doc = ParsedDocument(elements=[ParsedElement(element_type="table", rows=rows)])
    chunks = chunk(
        doc, document_id=DOC, filename="f.pdf", max_tokens=30, count_tokens=lambda t: len(t.split())
    )
    assert len(chunks) > 2  # continuation chunks repeat the header line
    seen = 0
    for c in chunks:
        hit = _hit([], index=c.chunk_index, text=c.text).model_copy(update={"rows": c.rows})
        for r in c.rows or []:
            line = row_line(hit, r)
            assert (
                line is not None
                and r.cells["Item"] in line
                and f"[{r.id}] {line}" in format_hit(hit)
            )
            seen += 1
    assert seen == 12
