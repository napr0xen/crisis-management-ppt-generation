"""Phase 28 (a, b): bound headers resolve to the table's real headers, the amount column from the
table structure, ranked group_sum series bind once, and ambiguity fails closed."""

import json

from doc_intel.mapping.derived import Computed, compute, resolve_columns
from doc_intel.mapping.engine import map_fields
from doc_intel.templates.schemas import Derivation
from tests.fakes import FakeLLM
from tests.unit.test_derived import _d, _table
from tests.unit.test_derived_mapping import CTX, DOC, Retr, _bind, _field, _stmt_hit

FLOW = [  # a tagged amount column, an untagged numeric one, a text one
    {"When": "01.05", "Detail": "Alpha · one", "Flow": "10,00 CR", "Left": "5,00"},
    {"When": "02.05", "Detail": "Beta · two", "Flow": "4,00 DR", "Left": "9,00"},
]
SPLIT = [
    {"Detail": "x", "Inflow": "3,00", "Outflow": "1,00"},
    {"Detail": "y", "Inflow": "", "Outflow": "2,00"},
]


def _res(t, tag=None, **kw):
    return resolve_columns(t, _d(**{"op": "sum", "column": "Amt", **kw}), tag)


def test_named_headers_resolve_by_normalized_equality():
    t = _table(FLOW)
    out = _res(t, "CR", op="count", column=" flow ", where={"DETAIL": "x"})
    assert (out.column, list(out.where)) == ("Flow", ["Detail"])
    assert _res(t, op="count", column="Nope") == "column_missing"
    assert _res(t, "CR", where={"Nope": "x"}) == "column_missing"


def test_the_amount_column_comes_from_the_structure_not_the_llm_string():
    t = _table(FLOW)
    assert _res(t, "CR").column == "Flow"  # the one tagged numeric column
    assert _res(t, "CR", column="Left").column == "Flow"  # whatever the LLM named
    inout = _table(SPLIT)
    assert _res(inout, "DR").column == "Outflow"  # the header names the direction
    assert _res(inout, "CR").column == "Inflow"


def test_without_a_structural_answer_only_a_numeric_llm_column_is_kept():
    inout = _table(SPLIT)
    assert _res(inout, column="outflow").column == "Outflow"
    assert _res(inout, column="Detail") == "column_ambiguous"
    assert _res(inout, column="Nope") == "column_ambiguous"
    assert _res(_table([{"Detail": "x", "Amt2": "1,00"}])).column == "Amt2"  # the only numeric one


def test_a_text_column_with_a_digit_is_not_numeric():
    t = _table([{"Memo": "INV-1 paid", "Sum": "1,00"}, {"Memo": "none", "Sum": "2,00"}])
    assert _res(t).column == "Sum"


GROUPS = [
    {"Kind": k, "Detail": d, "Amt": a}
    for k, d, a in (("A", "p · 1", "5,00"), ("B", "q · 2", "7,00"), ("A", "r · 3", "1,00"))
]


def test_group_by_resolution_and_delimiter():
    t = _table(GROUPS)
    out = _res(t, op="group_sum", column="Amt", group_by="kind")
    assert (out.group_by, out.group_sep) == ("Kind", None)  # repeating values: no delimiter
    out = _res(t, op="group_sum", column="Amt", group_by="detail", group_sep="x")
    assert (out.group_by, out.group_sep) == ("Detail", "·")  # all distinct, one delimiter
    out = _res(t, op="group_sum", column="Amt", group_by="nope")
    assert out.group_by == "Kind"  # the one non-numeric column whose values repeat


def test_ambiguous_grouping_fails_closed():
    t = _table(
        [
            {"X": "a", "Y": "a", "Amt": "1,00"},
            {"X": "b", "Y": "b", "Amt": "2,00"},
            {"X": "a", "Y": "a", "Amt": "3,00"},
        ]
    )
    assert _res(t, op="group_sum", column="Amt", group_by="nope") == "group_ambiguous"
    plain = _table([{"X": "a", "Amt": "1,00"}, {"X": "b", "Amt": "2,00"}])
    assert _res(plain, op="group_sum", column="Amt", group_by="nope") == "group_ambiguous"


def test_a_tie_at_the_ranked_group_fails_closed():
    t = _table([{"K": "a", "Amt": "5,00"}, {"K": "b", "Amt": "5,00"}, {"K": "c", "Amt": "1,00"}])
    spec = Derivation(op="group_sum", column="Amt", group_by="K")

    def at(rank):
        return compute(t, spec.model_copy(update={"rank": rank}), "text")

    assert at(1) == at(2) == "rank_tied"
    assert isinstance(at(3), Computed) and at(3).value == "c"
    assert at(4) == "rank_out_of_range"


# ---- engine: one binding per intent and per ranked series ----------------------------------------
def _named(fid, label, path=("Slide 1",), kind="currency"):
    return _field(None, fid, kind).model_copy(update={"label": label, "section_path": list(path)})


async def test_slots_with_the_same_intent_share_one_binding_and_citations():
    a, b = _named("total_deposits", "Total Deposits"), _named("total_inflow", "Total Inflow")
    llm = FakeLLM(_bind(column="Transaction amount"))  # a second call would exhaust the script
    out = await map_fields(CTX, [a, b], [DOC], Retr([_stmt_hit()]), llm)
    assert len(llm.calls) == 1
    assert out["total_deposits"].value == out["total_inflow"].value == 51015.15
    assert out["total_deposits"].evidence == out["total_inflow"].evidence


async def test_a_failed_first_binding_is_reused_without_a_call():
    a, b = _named("total_deposits", "Total Deposits"), _named("total_inflow", "Total Inflow")
    llm = FakeLLM(json.dumps({"feasible": False}), _bind(column="Transaction amount"))
    out = await map_fields(CTX, [a, b], [DOC], Retr([_stmt_hit()]), llm)
    # a failed binding is the intent's too: the second slot makes no call (Phase 30)
    assert (out["total_deposits"].status, out["total_inflow"].status) == ("missing", "missing")
    assert len(llm.calls) == 1


async def test_a_ranked_pair_binds_one_group_sum_once_per_series():
    path = ("Slide 5", "PAYMENTS")
    fields = []
    for n in (1, 2, 3):  # only the label sibling names the group
        fields += [
            _named(f"spend_label_{n}", f"Category {n}", path, "text"),
            _named(f"spend_value_{n}", f"Value {n}", path),
        ]
    reply = _bind(op="group_sum", column="x", group_by="description")
    llm = FakeLLM(reply)
    out = await map_fields(CTX, fields, [DOC], Retr([_stmt_hit()]), llm)
    assert len(llm.calls) == 1
    assert [out[f"spend_label_{n}"].value for n in (1, 2, 3)] == [
        "Rent",
        "Card purchase",
        "Utilities",
    ]
    assert [out[f"spend_value_{n}"].value for n in (1, 2, 3)] == [2000, 1683.51, 700]
    assert [e.row_id for e in out["spend_value_2"].evidence] == ["T1-R01", "T1-R06"]
    assert all(r.status == "populated" for r in out.values())


async def test_a_ranked_pair_with_fewer_groups_than_ranks_is_missing_for_that_rank():
    path = ("Slide 5", "PAYMENTS")
    fields = [_named(f"spend_label_{n}", f"Category {n}", path, "text") for n in (1, 4)]
    fields += [_named(f"spend_value_{n}", f"Value {n}", path) for n in (1, 4)]
    out = await map_fields(
        CTX,
        fields,
        [DOC],
        Retr([_stmt_hit()]),
        FakeLLM(_bind(op="group_sum", group_by="Description")),
    )
    assert out["spend_label_1"].status == "populated"
    assert (out["spend_label_4"].status, out["spend_value_4"].status) == ("missing", "missing")


async def test_ambiguous_grouping_is_missing_in_the_engine():
    path = ("Slide 5", "PAYMENTS")
    f = _named("spend_label_1", "Category 1", path, "text")
    v = _named("spend_value_1", "Value 1", path)
    reply = _bind(op="group_sum", group_by="Nope")  # a failed binding is not shared: one each
    bad = ("{}", "{}")  # the row sort (Phase 29) replies invalid twice: still missing
    llm = FakeLLM(reply, *bad, reply, *bad)
    out = await map_fields(CTX, [f, v], [DOC], Retr([_stmt_hit()]), llm)
    assert {r.status for r in out.values()} == {"missing"}


# ---- ponytail review fixes ---------------------------------------------------------------------
async def test_slots_of_one_intent_share_only_when_their_labels_say_the_same():
    inflow = [_named("total_deposits", "Total Deposits"), _named("total_in", "Total money in")]
    llm = FakeLLM(_bind(column="Transaction amount"))
    await map_fields(CTX, inflow, [DOC], Retr([_stmt_hit()]), llm)
    assert len(llm.calls) == 1  # the direction words are not content words
    pay = [
        _named("total_payments", "Total payments"),
        _named("card_payments", "Total card payments"),
    ]
    llm = FakeLLM(
        _bind(column="Transaction amount"),
        _bind(column="Transaction amount", where={"Description": "Rent · Landlord"}),
    )
    out = await map_fields(CTX, pay, [DOC], Retr([_stmt_hit()]), llm)
    assert len(llm.calls) == 2 and out["total_payments"].value == 4383.51
    assert out["card_payments"].value == 2000


async def test_a_binding_with_a_where_filter_is_never_shared():
    a, b = _named("card_payments", "Total payments"), _named("total_payments", "Total payments")
    llm = FakeLLM(
        _bind(column="Transaction amount", where={"Description": "Rent · Landlord"}),
        _bind(column="Transaction amount"),
    )
    out = await map_fields(CTX, [a, b], [DOC], Retr([_stmt_hit()]), llm)
    assert (out["card_payments"].value, out["total_payments"].value) == (2000, 4383.51)


def test_a_direction_named_column_beats_a_one_sided_tagged_balance():
    t = _table(
        [
            {"Withdrawal": "1,00", "Deposit": "3,00", "Balance": "10,00 Cr"},
            {"Withdrawal": "", "Deposit": "4,00", "Balance": "17,00 Cr"},
        ]
    )
    assert _res(t, "CR").column == "Deposit"
    assert _res(t, "DR").column == "Withdrawal"


def test_the_only_numeric_column_does_not_override_a_different_real_llm_header():
    t2 = _table([{"Posted": "01.05", "Money": "x"}, {"Posted": "02.05", "Money": "y"}])
    assert _res(t2, column="Money") == "column_ambiguous"
    assert _res(t2, column="Nope").column == "Posted"  # no real header named: the structure answers


def test_a_series_takes_only_group_or_unclassified_slots():
    from doc_intel.mapping.engine import _series
    from doc_intel.templates.ir_builder import _ranking_slots

    path = ("Slide 5", "PAYMENTS")
    fields = [
        _named("spend_label_2", "Category 2", path, "text"),
        _named("spend_value_2", "Value 2", path),
        _named("spend_largest_2", "Largest 2", path),
        _named("spend_date_2", "Date 2", path, "date"),
    ]
    fields = [f.model_copy(update={"mode": "derived"}) for f in fields]
    got = _series(fields, _ranking_slots(fields))
    assert set(got) == {"spend_label_2", "spend_value_2"}


async def test_a_series_binding_is_kept_when_its_rank_fails():
    path = ("Slide 5", "PAYMENTS")
    fields = []
    for n in (4, 5):
        fields += [
            _named(f"spend_label_{n}", f"Category {n}", path, "text"),
            _named(f"spend_value_{n}", f"Value {n}", path),
        ]
    llm = FakeLLM(_bind(op="group_sum", column="x", group_by="description"))
    out = await map_fields(CTX, fields, [DOC], Retr([_stmt_hit()]), llm)
    assert len(llm.calls) == 1 and {r.status for r in out.values()} == {"missing"}
