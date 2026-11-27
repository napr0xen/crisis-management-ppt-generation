"""Phase 29 item 2: a category breakdown over a statement table with no category column. A
running-balance column is never the amount, an untagged breakdown over a CR/DR column groups the
outflows, and when no column holds the group the LLM sorts the rows into categories, which keeps
row traceability but is never `populated` (the labels are the model's, not quoted)."""

import json

from doc_intel.mapping.derived import resolve_columns
from doc_intel.mapping.engine import map_fields
from tests.fakes import FakeLLM
from tests.unit.test_derived import _d, _table
from tests.unit.test_derived_mapping import CTX, DOC, Retr, _amount, _named, _stmt_hit

BAL = [  # an untagged amount column beside a running balance
    {"Detail": "x", "Amt": "3,00", "Balance": "10,00"},
    {"Detail": "y", "Amt": "2,00", "Balance": "12,00"},
]


def test_a_running_balance_column_is_never_the_amount_of_a_sum():
    t = _table(BAL)
    assert resolve_columns(t, _d(op="sum", column="Nope"), None).column == "Amt"
    assert resolve_columns(t, _d(op="sum", column="Balance"), None).column == "Amt"
    assert resolve_columns(t, _d(op="max", column="Balance"), None).column == "Balance"


def _grid(n=3):
    return [
        _named(f"category_{i}{s}", f"Category {i}{lbl}", "", ("Slide 5", "CATEGORY BREAKDOWN"), t)
        for i in range(1, n + 1)
        for s, lbl, t in (("", "", "text"), ("_amount", " Amount", "currency"))
    ]


SORTED = {  # STMT DR rows: R01 card 1183,51, R03 utilities 700, R05 rent 2000, R06 card 500
    "T1-R01": "Shopping",
    "T1-R02": "Income",
    "T1-R03": "Bills",
    "T1-R04": "Income",
    "T1-R05": "Housing",
    "T1-R06": "Shopping",
    "T1-R07": "Income",
}


def _plain_hit():
    """The statement with plain descriptions: no column or delimiter holds a category."""
    hit = _stmt_hit()
    rows = [
        r.model_copy(
            update={"cells": r.cells | {"Description": r.cells["Description"].replace(" · ", " ")}}
        )
        for r in hit.rows
    ]
    return hit.model_copy(update={"text": hit.text.replace(" · ", " "), "rows": rows})


def _sorted(rows=SORTED):
    return json.dumps({"rows": [{"id": k, "category": v} for k, v in rows.items()]})


async def test_categories_without_a_category_column_are_sorted_by_the_llm_and_partial():
    llm = FakeLLM(
        _amount(op="group_sum", group_by="Description"), _sorted()
    )  # one binding and one sort for the series
    out = await map_fields(CTX, _grid(), [DOC], Retr([_plain_hit()]), llm)
    assert len(llm.calls) == 2
    assert [out[f"category_{i}"].value for i in (1, 2, 3)] == ["Housing", "Shopping", "Bills"]
    assert [out[f"category_{i}_amount"].value for i in (1, 2, 3)] == [2000, 1683.51, 700]
    assert [e.row_id for e in out["category_2_amount"].evidence] == ["T1-R01", "T1-R06"]
    for r in out.values():
        assert (r.status, r.missing_reason) == ("partial", "insufficient_evidence")


async def test_a_row_the_llm_left_unsorted_fails_the_breakdown_closed():
    rows = {k: v for k, v in SORTED.items() if k != "T1-R03"}
    bind = _amount(op="group_sum", group_by="Description")
    llm = FakeLLM(bind, _sorted(rows), bind, _sorted(rows))  # a failed binding is not shared
    out = await map_fields(CTX, _grid(1), [DOC], Retr([_plain_hit()]), llm)
    assert {r.status for r in out.values()} == {"missing"}


async def test_a_real_category_column_never_asks_the_llm_to_sort():
    llm = FakeLLM(_amount(op="group_sum", group_by="Description", group_sep=" · "))
    out = await map_fields(CTX, _grid(1), [DOC], Retr([_stmt_hit()]), llm)
    assert len(llm.calls) == 1
    assert (out["category_1"].value, out["category_1"].status) == ("Rent", "populated")


def test_a_printed_category_summary_keeps_its_own_distinct_rows():
    """Review fix: a summary table has one row per category, all distinct: no LLM sort."""
    t = _table([{"Category": "Rent", "Amt": "5,00"}, {"Category": "Food", "Amt": "2,00"}])
    assert resolve_columns(
        t, _d(op="group_sum", column="Amt", group_by="category"), None
    ).group_by == ("Category")
    t = _table([{"Detail": "Rent", "Amt": "5,00"}, {"Detail": "Food", "Amt": "2,00"}])
    assert resolve_columns(t, _d(op="group_sum", column="Amt", group_by="Detail"), None) == (
        "group_ambiguous"
    )


def test_a_count_never_counts_a_running_balance():
    """Review fix: with text amounts the balance would be the only numeric column."""
    t = _table([{"Detail": "x", "Amt": "USD 3 CR", "Balance": "10,00 CR"}])
    assert resolve_columns(t, _d(op="count", column="Balance"), "CR") == "column_ambiguous"
