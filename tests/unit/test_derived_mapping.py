"""Derived slots in the engine (ADR-018): preset or bound derivation, fail closed, mapped alone."""

import json
import uuid

import pytest

from doc_intel.core.tenancy import TenantContext
from doc_intel.embeddings.schemas import Hit
from doc_intel.ingestion.schemas import TableRow
from doc_intel.mapping.engine import map_field, map_fields
from doc_intel.mapping.verifier import verify_quote
from doc_intel.templates.schemas import Constraints, Derivation, IRField
from tests.fakes import FakeLLM

CTX = TenantContext("u", uuid.uuid4())
DOC = uuid.uuid4()
TEXT = "| Item | Amount |\n| --- | --- |\n| Rent | 1.000,50 |\n| Fees | 200,25 |\n| Tax | 99,00 |"


def _hit(text=TEXT, rows=("R01", "R02", "R03"), index=0, doc=DOC, headers=("Item", "Amount")):
    cells = [("Rent", "1.000,50"), ("Fees", "200,25"), ("Tax", "99,00")]
    return Hit(
        chunk_id=uuid.uuid4(),
        document_id=doc,
        filename="s.csv",
        page_number=None,
        section_heading=None,
        element_type="table",
        text=text,
        score=0.0,
        chunk_index=index,
        rows=[
            TableRow(id=f"T1-{r}", cells=dict(zip(headers, c, strict=True)))
            for r, c in zip(rows, cells, strict=False)
        ],
    )


class Retr:
    def __init__(self, hits):
        self.hits, self.searches = hits, 0

    async def table_chunks(self, ctx, document_ids):
        return self.hits

    async def search(self, *a, **k):
        self.searches += 1
        return [_hit().model_copy(update={"element_type": "text", "rows": None})]

    async def with_neighbors(self, ctx, hits, budget):
        return hits

    async def opening(self, ctx, document_ids, n=2):
        return []


def _field(derivation=None, fid="total", expected_type="currency"):
    return IRField(
        field_id=fid,
        anchor_id="a",
        section_path=["Slide 1"],
        label="Total",
        description="Total amount",
        expected_type=expected_type,
        required=True,
        write_mode="replace_text",
        constraints=Constraints(),
        mode="derived",
        derivation=derivation,
    )


def _bind(table="t1", **kw):
    spec = {"op": "sum", "column": "Amount", **kw}
    return json.dumps({"table": table, "not_found": False, "derivation": spec})


SUM = Derivation(op="sum", column="Amount")


async def test_preset_derivation_makes_no_llm_call():
    llm = FakeLLM()
    r = await map_field(CTX, _field(SUM), [DOC], Retr([_hit()]), llm)
    assert (r.status, r.value, llm.calls) == ("populated", 1299.75, [])
    assert r.confidence == 0.9 and r.checked_documents == [DOC]


async def test_llm_binding_and_verbatim_row_evidence():
    hit, llm = _hit(), FakeLLM(_bind())
    r = await map_field(CTX, _field(), [DOC], Retr([hit]), llm)
    assert (r.status, r.value, len(llm.calls)) == ("populated", 1299.75, 1)
    assert [e.row_id for e in r.evidence] == ["T1-R01", "T1-R02", "T1-R03"]
    for e in r.evidence:
        assert verify_quote([hit], str(e.chunk_id), e.quote) is not None
    assert r.evidence[0].quote == "| Rent | 1.000,50 |"
    assert "t1" in llm.calls[0][0][1]["content"] and "Amount" in llm.calls[0][0][1]["content"]


async def test_list_value_for_group_sum():
    spec = Derivation(op="group_sum", column="Amount", group_by="Item", top_n=1)
    r = await map_field(
        CTX, _field(spec, expected_type="bullet_list"), [DOC], Retr([_hit()]), FakeLLM()
    )
    assert (r.status, r.value) == ("populated", ["Rent: 1.000,50"])
    assert [e.item for e in r.evidence] == [0]


async def test_not_found_and_unknown_table_are_missing():
    for reply in (json.dumps({"not_found": True}), _bind(table="t9")):
        r = await map_field(CTX, _field(), [DOC], Retr([_hit()]), FakeLLM(reply))
        assert (r.status, r.missing_reason, r.value, r.evidence) == (
            "missing",
            "not_found_by_llm",
            None,
            [],
        )


async def test_no_tables_and_absent_column_and_row_gap_are_missing():
    r = await map_field(CTX, _field(SUM), [DOC], Retr([]), FakeLLM())
    assert r.missing_reason == "no_relevant_chunks"
    r = await map_field(CTX, _field(), [DOC], Retr([_hit()]), FakeLLM(_bind(where={"Nope": "x"})))
    assert (r.status, r.missing_reason) == ("missing", "no_relevant_chunks")
    gap = _hit(rows=("R01", "R03", "R04"))
    r = await map_field(CTX, _field(SUM), [DOC], Retr([gap]), FakeLLM())
    assert (r.status, r.missing_reason) == ("missing", "evidence_failed_verification")


async def test_preset_column_in_no_table_is_missing_and_in_two_is_ambiguous():
    r = await map_field(
        CTX, _field(Derivation(op="sum", column="Nope")), [DOC], Retr([_hit()]), FakeLLM()
    )
    assert r.missing_reason == "no_relevant_chunks"
    two = [_hit(), _hit(doc=uuid.uuid4())]
    r = await map_field(CTX, _field(SUM), [DOC], Retr(two), FakeLLM())
    assert r.missing_reason == "evidence_failed_verification"


async def test_a_derived_field_is_mapped_alone_never_in_a_batch():
    loose = [
        _field(fid="a", expected_type="text").model_copy(update={"mode": "extractive"}),
        _field(SUM),
        _field(fid="b", expected_type="text").model_copy(update={"mode": "extractive"}),
    ]
    seen, retr = [], Retr([_hit()])
    llm = FakeLLM(
        json.dumps(
            {
                "slots": [
                    {"n": n, "candidates": [], "not_found": True, "incomplete": False}
                    for n in (1, 2)
                ]
            }
        )
    )
    progress = []

    async def on_field(done, fid):
        progress.append(fid)

    results = await map_fields(CTX, loose, [DOC], retr, llm, on_field)
    for messages, _ in llm.calls:
        seen.append(messages[1]["content"])
    assert results["total"].value == 1299.75 and results["total"].status == "populated"
    assert not any("Total amount" in s and "Amount" in s and "t1" in s for s in seen)
    assert len(llm.calls) == 1 and "total" in progress


def _eu_hit(amounts, headers=("Item", "Amount")):
    rows = [
        (f"T1-R{n:02d}", dict(zip(headers, (f"r{n}", a), strict=True)))
        for n, a in enumerate(amounts, 1)
    ]
    text = "| Item | Amount |\n| --- | --- |\n" + "\n".join(
        f"| r{n} | {a} |" for n, (_, c) in enumerate(rows, 1) for a in [c["Amount"]]
    )
    return Hit(
        chunk_id=uuid.uuid4(),
        document_id=DOC,
        filename="s.csv",
        page_number=None,
        section_heading=None,
        element_type="table",
        text=text,
        score=0.0,
        chunk_index=0,
        rows=[TableRow(id=i, cells=c) for i, c in rows],
    )


async def test_european_thousands_result_is_stored_exactly_not_reparsed():
    # rendered "12.000" must not be re-read as twelve
    mn = Derivation(op="min", column="Amount")
    r = await map_field(CTX, _field(mn), [DOC], Retr([_eu_hit(["1.829.914", "12.000"])]), FakeLLM())
    assert (r.status, r.value) == ("populated", 12000)
    df = Derivation(op="diff", column="Amount", minus="Amount2")
    hit = _eu_hit(["1.500.000"])
    hit.rows[0].cells["Amount2"] = "1.498.000"
    r = await map_field(CTX, _field(df), [DOC], Retr([hit]), FakeLLM())
    assert r.value == 2000


async def test_llm_binding_that_breaks_derivation_rules_is_missing_not_a_crash():
    llm = FakeLLM(_bind(op="diff", tag="CR"), _bind(op="diff", tag="CR"))
    r = await map_field(CTX, _field(), [DOC], Retr([_hit()]), llm)
    assert r.status == "missing" and r.missing_reason in (
        "llm_error",
        "evidence_failed_verification",
    )


def _total_hit():
    """Rent 1.000,50 + Fees 200,25 + Tax 99,00 and a printed total row 1.299,75."""
    hit = _hit()
    hit.rows.append(TableRow(id="T1-R04", cells={"Item": "Total", "Amount": "1.299,75"}))
    hit.text += "\n| Total | 1.299,75 |"
    return hit


async def test_summary_row_binding_is_corrected_by_one_rebinding():
    llm = FakeLLM(_bind(), _bind(exclude_rows=["T1-R04"]))
    r = await map_field(CTX, _field(), [DOC], Retr([_total_hit()]), llm)
    assert (r.status, r.value, len(llm.calls)) == ("populated", 1299.75, 2)
    assert [e.row_id for e in r.evidence] == ["T1-R01", "T1-R02", "T1-R03"]
    retry = llm.calls[1][0]
    assert retry[-2]["role"] == "assistant" and "T1-R04" in retry[-1]["content"]


async def test_summary_row_binding_still_wrong_after_retry_is_missing():
    llm = FakeLLM(_bind(), _bind())
    r = await map_field(CTX, _field(), [DOC], Retr([_total_hit()]), llm)
    assert (r.status, r.value, r.missing_reason, len(llm.calls)) == (
        "missing",
        None,
        "evidence_failed_verification",
        2,
    )


async def test_summary_row_in_a_preset_derivation_is_missing_without_llm():
    llm = FakeLLM()
    r = await map_field(CTX, _field(SUM), [DOC], Retr([_total_hit()]), llm)
    assert (r.status, r.missing_reason, llm.calls) == (
        "missing",
        "evidence_failed_verification",
        [],
    )


# ---- ranked slot grids (largest receipts/payments, category breakdown) ---------------------------
STMT_HEADERS = ("Booking date", "Value", "Description", "Transaction amount", "Balance")
STMT = [  # (date, description, amount): no Category column
    ("2026-01-02", "Card purchase · Fennstead", "1.183,51 DR"),
    ("2026-01-03", "Transfer received · Gilwick INV-1", "45.015,15 CR"),
    ("2026-01-04", "Utilities · Powerco", "700,00 DR"),
    ("2026-01-05", "Transfer received · Acme", "3.000,00 CR"),
    ("2026-01-06", "Rent · Landlord", "2.000,00 DR"),
    ("2026-01-07", "Card purchase · Cafe", "500,00 DR"),
    ("2026-01-08", "Transfer received · Beta", "3.000,00 CR"),
]


def _stmt_hit():
    rows, lines = [], []
    for n, (d, desc, amt) in enumerate(STMT, 1):
        cells = dict(zip(STMT_HEADERS, (d, d, desc, amt, "0,00"), strict=True))
        rows.append(TableRow(id=f"T1-R{n:02d}", cells=cells))
        lines.append("| " + " | ".join(cells.values()) + " |")
    text = (
        "| " + " | ".join(STMT_HEADERS) + " |\n| --- | --- | --- | --- | --- |\n" + "\n".join(lines)
    )
    return Hit(
        chunk_id=uuid.uuid4(),
        document_id=DOC,
        filename="s.csv",
        page_number=None,
        section_heading=None,
        element_type="table",
        text=text,
        score=0.0,
        chunk_index=0,
        rows=rows,
    )


def _top(tag, pick, n, kind, heading):
    spec = Derivation(op="top", column="Transaction amount", tag=tag, pick=pick)
    f = _field(
        spec,
        f"{heading}_{kind}_{n}",
        "currency" if kind == "amount" else ("date" if kind == "date" else "text"),
    )
    # as the live IR: the heading names the ranking, the context is the column header
    return f.model_copy(
        update={"context": kind.title(), "section_path": ["Slide 6", f"LARGEST {heading.upper()}S"]}
    )


def _grid(tag, heading, party_kind, n=3):
    return [
        _top(tag, pick, i, kind, heading)
        for i in range(1, n + 1)
        for kind, pick in (("date", "Booking date"), (party_kind, "Description"), ("amount", None))
    ]


async def test_largest_receipts_and_payments_take_their_own_ranked_rows():
    fields = _grid("CR", "receipt", "source") + _grid("DR", "payment", "payee")
    llm = FakeLLM()
    out = await map_fields(CTX, fields, [DOC], Retr([_stmt_hit()]), llm)
    assert llm.calls == []
    got = {k: v.value for k, v in out.items()}
    assert [got[f"receipt_amount_{i}"] for i in (1, 2, 3)] == [45015.15, 3000, 3000]
    assert got["receipt_date_2"] == "2026-01-05" and got["receipt_date_3"] == "2026-01-08"  # ties
    assert got["receipt_source_1"] == "Transfer received · Gilwick INV-1"
    assert [got[f"payment_amount_{i}"] for i in (1, 2, 3)] == [2000, 1183.51, 700]
    assert got["payment_payee_1"] == "Rent · Landlord"
    for k, v in out.items():
        assert v.status == "populated" and len(v.evidence) == 1, k
    assert out["payment_date_2"].evidence[0].row_id == out["payment_amount_2"].evidence[0].row_id
    assert out["receipt_date_1"].evidence[0].row_id == "T1-R02"


async def test_a_record_citing_different_rows_is_demoted_as_rank_mixed():
    fields = _grid("DR", "payment", "payee", 2)
    bad = Derivation(op="top", column="Transaction amount", tag="CR")  # amount bound to receipts
    fields = [
        f.model_copy(update={"derivation": bad}) if f.field_id == "payment_amount_1" else f
        for f in fields
    ]
    out = await map_fields(CTX, fields, [DOC], Retr([_stmt_hit()]), FakeLLM())
    for k in ("payment_date_1", "payment_payee_1", "payment_amount_1"):
        assert (out[k].status, out[k].missing_reason) == ("missing", "evidence_failed_verification")
    assert out["payment_date_2"].status == "populated"


async def test_rank_beyond_the_rows_is_missing():
    f = _top("CR", None, 4, "amount", "receipt")  # only three receipts
    r = await map_field(CTX, f, [DOC], Retr([_stmt_hit()]), FakeLLM(), rank=4)
    assert (r.status, r.missing_reason) == ("missing", "evidence_failed_verification")


async def test_category_slots_rank_description_prefixes_with_exact_sums():
    spec = Derivation(
        op="group_sum",
        column="Transaction amount",
        group_by="Description",
        group_sep=" · ",
        tag="DR",
    )
    fields = []
    for i in (1, 2, 3):
        fields.append(_field(spec, f"category_{i}", "text"))
        fields.append(_field(spec, f"category_{i}_amount", "currency"))
    out = await map_fields(CTX, fields, [DOC], Retr([_stmt_hit()]), FakeLLM())
    assert [out[f"category_{i}"].value for i in (1, 2, 3)] == ["Rent", "Card purchase", "Utilities"]
    assert [out[f"category_{i}_amount"].value for i in (1, 2, 3)] == [2000, 1683.51, 700]
    assert [e.row_id for e in out["category_2_amount"].evidence] == ["T1-R01", "T1-R06"]
    assert all(r.status == "populated" for r in out.values())


async def test_two_ranks_citing_the_same_rows_are_demoted_as_rank_mixed():
    total = Derivation(op="sum", column="Transaction amount", tag="DR")  # not rank-aware
    fields = [_field(total, f"category_{i}_amount", "currency") for i in (1, 2)]
    out = await map_fields(CTX, fields, [DOC], Retr([_stmt_hit()]), FakeLLM())
    for r in out.values():
        assert (r.status, r.missing_reason) == ("missing", "evidence_failed_verification")


async def test_numbered_copies_of_other_derived_fields_are_not_ranked():
    sums = [
        _field(Derivation(op="sum", column="Transaction amount", tag=t), fid, "currency")
        for fid, t in (("total_money_in_value_2", "CR"), ("total_money_out_value_2", "DR"))
    ]
    table = _field(
        Derivation(
            op="group_sum", column="Transaction amount", group_by="Description", top_n=2, tag="DR"
        ),
        "category_breakdown_2",
        "table",
    )
    out = await map_fields(CTX, [*sums, table], [DOC], Retr([_stmt_hit()]), FakeLLM())
    assert all(r.status == "populated" for r in out.values())
    assert out["total_money_in_value_2"].value == 51015.15


# ---- the slot's names fix op and tag, the binder only picks table and columns -------------------
def _named(fid, label, context="", path=("Slide 1",), expected_type="currency"):
    return _field(None, fid, expected_type).model_copy(
        update={"label": label, "context": context or None, "section_path": list(path)}
    )


def _amount(**kw):
    return _bind(column="Transaction amount", **kw)


async def _intent(field, reply, rank=None):
    llm = FakeLLM(reply)
    out = await map_field(CTX, field, [DOC], Retr([_stmt_hit()]), llm, rank=rank)
    return out.value


async def test_intent_overrides_a_wrong_op_or_tag_from_the_binder():
    cr, dr = 51015.15, 4383.51
    dep = _named("total_deposits", "Total Deposits", "TOTAL DEPOSITS")
    wd = _named("total_withdrawals", "Total Withdrawals", "TOTAL WITHDRAWALS")
    net = _named("net_cash_movement", "Net Cash Movement", "NET MOVEMENT")
    assert await _intent(dep, _amount(tag="DR")) == cr
    assert await _intent(dep, _amount()) == cr
    assert await _intent(wd, _amount(tag="CR")) == dr
    assert await _intent(wd, _amount(op="max")) == dr
    assert await _intent(net, _amount(tag="CR")) == pytest.approx(cr - dr)
    assert await _intent(net, _amount(op="sum", tag="DR")) == pytest.approx(cr - dr)


async def test_intent_group_sum_and_largest_singletons():
    cat = _named("category_1", "Category 1", "", ("S", "SPENDING BY CATEGORY"), "text")
    cat_bind = _amount(op="group_sum", group_by="Description", group_sep=" · ", tag="CR")
    assert await _intent(cat, cat_bind, rank=1) == "Rent"  # tag DR from the heading
    big_in = _named("largest_credit_amount", "Largest Credit Amount", "LARGEST CREDIT")
    big_out = _named("largest_debit_amount", "Largest Debit Amount", "LARGEST DEBIT")
    assert await _intent(big_in, _amount(op="sum")) == 45015.15
    assert await _intent(big_out, _amount(op="top", tag="CR")) == 2000


async def test_intent_fixes_the_tag_of_paired_ranked_grids():
    rec = _named("receipt_amount_1", "Amount", "Amount", ("Slide 6", "LARGEST RECEIPTS"))
    pay = _named("payment_amount_1", "Amount", "Amount", ("Slide 6", "LARGEST PAYMENTS"))
    llm = FakeLLM(_amount(op="top", tag="DR"), _amount(op="top"))  # wrong tag, no tag
    out = await map_fields(CTX, [rec, pay], [DOC], Retr([_stmt_hit()]), llm)
    assert (out["receipt_amount_1"].value, out["payment_amount_1"].value) == (45015.15, 2000)


async def test_intent_that_breaks_the_binding_fails_closed():
    cat = _named("category_1", "Category 1", "", ("S", "CATEGORY BREAKDOWN"), "text")
    # no group column: the row sort runs (Phase 29) and its invalid reply fails closed
    llm = FakeLLM(_amount(op="sum"), "{}", "{}")
    r = await map_field(CTX, cat, [DOC], Retr([_stmt_hit()]), llm)
    assert r.status == "missing" and r.value is None


def _typed_hit(header):
    return _hit(text=TEXT.replace("Amount", header), headers=("Item", header))


async def test_an_untagged_amount_column_never_sums_both_directions():
    dep = _named("total_deposits", "Total Deposits", "TOTAL DEPOSITS")
    hit = _typed_hit("Amount")
    r = await map_field(CTX, dep, [DOC], Retr([hit]), FakeLLM(_bind()))
    assert (r.status, r.value) == ("missing", None)  # no tag, no filter, no direction column
    kept = _bind(where={"Item": "Rent"})  # a Type-like filter keeps the binding
    r = await map_field(CTX, dep, [DOC], Retr([hit]), FakeLLM(kept))
    assert (r.status, r.value) == ("populated", 1000.5)
    r = await map_field(
        CTX, dep, [DOC], Retr([_typed_hit("Deposits")]), FakeLLM(_bind(column="Deposits"))
    )
    assert (r.status, r.value) == ("populated", 1299.75)  # a column named for the direction


async def test_a_top_intent_without_pick_on_a_text_slot_fails_closed():
    pay = _named("payment_payee_1", "Payee", "Payee", ("Slide 6", "LARGEST PAYMENTS"), "text")
    r = await map_field(CTX, pay, [DOC], Retr([_stmt_hit()]), FakeLLM(_amount(op="sum")), rank=1)
    assert (r.status, r.value) == ("missing", None)


async def test_directional_counts_count_the_rows_of_their_own_direction():
    """Phase 29 item 3b: the binder's column string does not matter, the CR/DR column does."""
    cr = _named("credit_transaction_count", "Credit Transaction Count", expected_type="number")
    dr = _named("debit_transaction_count", "Debit Transaction Count", expected_type="number")
    llm = FakeLLM(_bind(op="count", column="Description"), _bind(op="count", column="Nope"))
    out = await map_fields(CTX, [cr, dr], [DOC], Retr([_stmt_hit()]), llm)
    assert (out["credit_transaction_count"].value, out["debit_transaction_count"].value) == (3, 4)
    assert [e.row_id for e in out["credit_transaction_count"].evidence] == [
        "T1-R02",
        "T1-R04",
        "T1-R07",
    ]
