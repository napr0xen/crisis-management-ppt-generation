"""Phase 24: derived slots over real ingestion into real Qdrant. Expected values are literals
computed by hand from the fixture rows below, never by the code under test."""

import csv
import io
import json
import uuid

import pytest

from doc_intel.core.tenancy import TenantContext
from doc_intel.ingestion.pipeline import parse_and_chunk
from doc_intel.mapping.engine import map_fields
from doc_intel.mapping.retriever import Retriever
from doc_intel.templates.schemas import Constraints, IRField
from tests.fakes import FakeEmbedder, FakeLLM

pytestmark = pytest.mark.integration

HEADERS = ["Date", "Description", "Category", "Money In", "Money Out", "Balance"]
EU_ROWS = [  # in, out
    ["2024-01-02", "Salary", "Income", "3.000,00 $", "", "3.000,00 $"],
    ["2024-01-03", "Rent", "Housing", "", "1.200,50 $", "1.799,50 $"],
    ["2024-01-04", "Groceries", "Food", "", "310,25 $", "1.489,25 $"],
    ["2024-01-05", "Refund", "Income", "45,10 $", "", "1.534,35 $"],
    ["2024-01-06", "Restaurant", "Food", "", "89,75 $", "1.444,60 $"],
    ["2024-01-07", "Utilities", "Housing", "", "150,00 $", "1.294,60 $"],
    ["2024-01-08", "Freelance", "Income", "1.250,40 $", "", "2.545,00 $"],
    ["2024-01-09", "Cinema", "Leisure", "", "40,00 $", "2.505,00 $"],
]
CR_DR_ROWS = [
    ["2024-02-01", "Deposit", "402.031,45 CR"],
    ["2024-02-02", "Card", "181.223,68 DR"],
    ["2024-02-03", "Transfer", "1.000,00 CR"],
    ["2024-02-04", "Fee", "20,32 DR"],
]


def csv_bytes(headers, rows) -> bytes:
    out = io.StringIO()
    csv.writer(out).writerows([headers, *rows])
    return out.getvalue().encode()


def field(fid, expected_type="currency") -> IRField:
    return IRField(
        field_id=fid,
        anchor_id=f"pptx:s1:{fid}",
        section_path=["Slide 1"],
        label=fid.replace("_", " "),
        description="A figure computed from the statement.",
        expected_type=expected_type,
        required=True,
        write_mode="replace_text",
        constraints=Constraints(),
        mode="derived",
    )


def bind(**kw) -> str:
    return json.dumps({"table": "t1", "feasible": True, "derivation": kw})


async def ingest(vector_store, headers, rows):
    ctx, doc = TenantContext(f"user-{uuid.uuid4()}", uuid.uuid4()), uuid.uuid4()
    _, chunks = parse_and_chunk(
        csv_bytes(headers, rows),
        "csv",
        document_id=doc,
        filename="statement.csv",
        max_tokens=200,
        tokenizer_model="Qwen/Qwen3-Embedding-0.6B",
    )
    embedder = FakeEmbedder()
    await vector_store.upsert_chunks(
        ctx, chunks, await embedder.embed_documents([c.text for c in chunks])
    )
    return (
        ctx,
        doc,
        {str(c.chunk_id): c.text for c in await vector_store.table_chunks(ctx, [doc])},
        (Retriever(embedder, vector_store)),
    )


def assert_cited(result, texts, rows_used):
    assert len(result.evidence) == rows_used
    for e in result.evidence:
        assert e.row_id and e.quote in texts[str(e.chunk_id)]  # verbatim in its chunk


async def test_european_dollar_statement(vector_store):
    ctx, doc, texts, retriever = await ingest(vector_store, HEADERS, EU_ROWS)
    fields = [
        field("total_money_in"),
        field("total_money_out"),
        field("net_movement"),
        field("category_breakdown", "table"),
    ]
    llm = FakeLLM(
        bind(op="sum", column="Money In"),
        bind(op="sum", column="Money Out"),
        bind(op="diff", column="Money In", minus="Money Out"),
        bind(op="group_sum", column="Money Out", group_by="Category", top_n=3),
    )
    r = await map_fields(ctx, fields, [doc], retriever, llm)
    assert len(llm.calls) == 4
    assert all(x.status == "populated" and x.provenance == "rag" for x in r.values())
    assert r["total_money_in"].value == 4295.5  # 3000.00 + 45.10 + 1250.40
    assert r["total_money_out"].value == 1790.5  # 1200.50 + 310.25 + 89.75 + 150.00 + 40.00
    assert r["net_movement"].value == 2505  # 4295.50 - 1790.50
    assert r["category_breakdown"].value == [
        ["Housing", "1.350,50 $"],  # 1200.50 + 150.00
        ["Food", "400,00 $"],  # 310.25 + 89.75
        ["Leisure", "40,00 $"],
    ]
    assert_cited(r["total_money_in"], texts, 3)
    assert_cited(r["total_money_out"], texts, 5)
    assert_cited(r["net_movement"], texts, 8)
    assert_cited(r["category_breakdown"], texts, 5)
    assert [e.item for e in r["category_breakdown"].evidence] == [0, 0, 1, 1, 2]


async def test_cr_dr_tagged_single_column(vector_store):
    ctx, doc, texts, retriever = await ingest(
        vector_store, ["Date", "Description", "Amount"], CR_DR_ROWS
    )
    fields = [field("money_in"), field("money_out"), field("net")]
    llm = FakeLLM(
        bind(op="sum", column="Amount", tag="CR"),
        bind(op="sum", column="Amount", tag="DR"),
        bind(op="diff", column="Amount"),
    )
    r = await map_fields(ctx, fields, [doc], retriever, llm)
    assert all(x.status == "populated" for x in r.values())
    assert r["money_in"].value == 403031.45  # 402031.45 + 1000.00
    assert r["money_out"].value == 181244  # 181223.68 + 20.32
    assert r["net"].value == 221787.45  # 403031.45 - 181244.00
    assert_cited(r["money_in"], texts, 2)
    assert_cited(r["money_out"], texts, 2)
    assert_cited(r["net"], texts, 4)


async def test_unknown_column_fails_closed(vector_store):
    ctx, doc, _, retriever = await ingest(vector_store, HEADERS, EU_ROWS)
    wrong = bind(op="sum", column="Money Sideways")
    llm = FakeLLM(wrong, wrong)  # the first refusal gets one corrective re-binding
    r = (await map_fields(ctx, [field("total")], [doc], retriever, llm))["total"]
    assert (r.status, r.value, r.evidence) == ("missing", None, [])
    assert len(llm.calls) == 2


async def test_printed_total_row_is_excluded_after_one_rebinding(vector_store):
    # a sum row not named as a total (arithmetic backstop) in Money In
    rows = [*EU_ROWS, ["2024-01-09", "Period sum", "", "4.295,50 $", "", ""]]
    ctx, doc, texts, retriever = await ingest(vector_store, HEADERS, rows)
    llm = FakeLLM(
        bind(op="sum", column="Money In"),  # forgets the printed total
        bind(op="sum", column="Money In", exclude_rows=["T1-R09"]),
    )
    r = (await map_fields(ctx, [field("total_money_in")], [doc], retriever, llm))["total_money_in"]
    assert len(llm.calls) == 2
    assert (r.status, r.value) == ("populated", 4295.5)
    assert_cited(r, texts, 3)


async def test_ranked_slots_without_a_category_column(vector_store):
    rows = [
        ["2024-03-01", "Card · Shop A", "300,00 DR"],
        ["2024-03-02", "Rent · Landlord", "1.200,00 DR"],
        ["2024-03-03", "Card · Shop B", "1.100,00 DR"],
        ["2024-03-04", "Salary · Acme", "5.000,00 CR"],
    ]
    ctx, doc, texts, retriever = await ingest(vector_store, ["Date", "Description", "Amount"], rows)
    cat = {"op": "group_sum", "column": "Amount", "group_by": "Description", "group_sep": " · "}
    fields = [field("category_1", "text"), field("category_1_amount")]
    fields += [field("payment_payee_1", "text"), field("payment_amount_1")]
    llm = FakeLLM(
        bind(**cat, tag="DR"),  # one binding serves the whole category series
        bind(op="top", column="Amount", tag="DR", pick="Description"),
        bind(op="top", column="Amount", tag="DR"),
    )
    r = await map_fields(ctx, fields, [doc], retriever, llm)
    assert all(x.status == "populated" for x in r.values())
    assert (r["category_1"].value, r["category_1_amount"].value) == ("Card", 1400)  # 300 + 1100
    assert (r["payment_payee_1"].value, r["payment_amount_1"].value) == ("Rent · Landlord", 1200)
    assert_cited(r["payment_amount_1"], texts, 1)
