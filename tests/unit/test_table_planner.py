"""Phase 30 (A): the LLM plans the table query (verbatim headers, any op), code executes it."""

import json
import uuid

from doc_intel.embeddings.schemas import Hit
from doc_intel.ingestion.schemas import TableRow
from doc_intel.mapping.derived import resolve_columns
from doc_intel.mapping.engine import map_field, map_fields
from doc_intel.mapping.verifier import verify_quote
from tests.fakes import FakeLLM
from tests.unit.test_derived import _d, _table
from tests.unit.test_derived_mapping import CTX, DOC, Retr, _field, _hit
from tests.unit.test_derived_resolution import _named


def _plan(**kw):
    return json.dumps({"table": "t1", "feasible": True, "reason": "", "derivation": kw})


def _slot(expected_type="currency", fid="result"):
    return _field(None, fid, expected_type).model_copy(
        update={"label": "Result", "description": "A figure from the table"}
    )


async def _run(plan, expected_type="currency"):
    hit, llm = _hit(), FakeLLM(plan)
    r = await map_field(CTX, _slot(expected_type), [DOC], Retr([hit]), llm)
    return r, hit, llm


async def test_planner_sum_is_executed_with_row_evidence():
    r, hit, llm = await _run(_plan(op="sum", column="Amount"))
    assert (r.status, r.value, len(llm.calls)) == ("populated", 1299.75, 1)
    assert [e.row_id for e in r.evidence] == ["T1-R01", "T1-R02", "T1-R03"]
    for e in r.evidence:
        assert verify_quote([hit], str(e.chunk_id), e.quote) is not None


async def test_planner_count_is_executed():
    r, _, _ = await _run(_plan(op="count", column="Item"), "number")
    assert (r.status, r.value) == ("populated", 3)
    assert len(r.evidence) == 3


async def test_planner_argmax_takes_the_picked_cell_of_the_extreme_row():
    r, _, _ = await _run(_plan(op="argmax", column="Amount", pick="Item"), "text")
    assert (r.status, r.value) == ("populated", "Rent")
    assert [e.row_id for e in r.evidence] == ["T1-R01"]


async def test_infeasible_plan_is_missing_with_one_call():
    reply = json.dumps({"feasible": False, "reason": "no table holds this"})
    llm = FakeLLM(reply)
    r = await map_field(CTX, _slot(), [DOC], Retr([_hit()]), llm)
    assert (r.status, r.missing_reason, len(llm.calls)) == ("missing", "not_found_by_llm", 1)


def _two_numeric_hit():
    heads = ("Item", "Cost", "Tax")
    rows = [
        TableRow(id=f"T1-R0{n}", cells=dict(zip(heads, (i, c, t), strict=True)))
        for n, (i, c, t) in enumerate((("a", "1,00", "5,50"), ("b", "2,50", "0,25")), 1)
    ]
    return Hit(
        chunk_id=uuid.uuid4(),
        document_id=DOC,
        filename="s.csv",
        page_number=None,
        section_heading=None,
        element_type="table",
        text="| Item | Cost | Tax |\n| --- | --- | --- |\n| a | 1,00 | 5,50 |\n| b | 2,50 | 0,25 |",
        score=0.0,
        chunk_index=0,
        rows=rows,
    )


async def test_ambiguous_column_gets_one_correction_then_fails_closed():
    llm = FakeLLM(_plan(op="sum", column="Nope"), _plan(op="sum", column="Nope"))
    r = await map_field(CTX, _slot(), [DOC], Retr([_two_numeric_hit()]), llm)
    assert (r.status, len(llm.calls)) == ("missing", 2)
    note = llm.calls[1][0][-1]["content"]
    assert "column_ambiguous" in note and '["Cost", "Tax"]' in note and "'Nope'" in note


async def test_ambiguous_column_corrected_by_the_second_binding():
    llm = FakeLLM(_plan(op="sum", column="Nope"), _plan(op="sum", column="Tax"))
    r = await map_field(CTX, _slot(), [DOC], Retr([_two_numeric_hit()]), llm)
    assert (r.status, r.value, len(llm.calls)) == ("populated", 5.75, 2)


async def test_a_failed_binding_is_memoized_for_a_ranked_series():
    path = ("Slide 5", "PAYMENTS")
    fields = []
    for n in (1, 2, 3):
        fields += [
            _named(f"spend_label_{n}", f"Category {n}", path, "text"),
            _named(f"spend_value_{n}", f"Value {n}", path),
        ]
    wrong = _plan(op="group_sum", column="Nope", group_by="Item")
    llm = FakeLLM(wrong, wrong)  # a third call would exhaust the script
    out = await map_fields(CTX, fields, [DOC], Retr([_two_numeric_hit()]), llm)
    assert len(llm.calls) == 2
    assert len(out) == 6 and {r.status for r in out.values()} == {"missing"}


def test_a_verbatim_planner_header_decides_when_structure_cannot():
    t = _table([{"Detail": "x", "Cost": "1,00", "Tax": "2,00"}])
    assert resolve_columns(t, _d(op="sum", column="Tax"), None).column == "Tax"


def test_currency_word_cells_are_used_when_the_planner_names_them():
    t = _table([{"Detail": "x", "Cost": "USD 5.00"}, {"Detail": "y", "Cost": "USD 7.25"}])
    assert resolve_columns(t, _d(op="sum", column="Cost"), None).column == "Cost"
    assert resolve_columns(t, _d(op="sum", column="Detail"), None) == "column_ambiguous"


def test_a_code_column_named_by_the_planner_is_never_summed():
    # "INV 12" parses as a word plus a number but is a reference, not an amount
    t = _table(
        [
            {"Ref": "INV 12", "Cost": "1,00", "Tax": "2,00"},
            {"Ref": "INV 13", "Cost": "3,00", "Tax": "4,00"},
        ]
    )
    assert resolve_columns(t, _d(op="sum", column="Ref"), None) == "column_ambiguous"


def test_a_text_column_named_by_the_planner_is_never_a_number():
    t = _table([{"Description": "Rent", "Cost": "1,00", "Tax": "2,00"}])
    assert resolve_columns(t, _d(op="sum", column="Description"), None) == "column_ambiguous"
