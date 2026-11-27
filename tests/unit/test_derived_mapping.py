"""Derived slots in the engine (ADR-018): preset or bound derivation, fail closed, mapped alone."""

import json
import uuid

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
    r = await map_field(CTX, _field(), [DOC], Retr([_hit()]), FakeLLM(_bind(column="Nope")))
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
