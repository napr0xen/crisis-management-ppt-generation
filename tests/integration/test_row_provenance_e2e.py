"""Phase 23: a table document through the real parse, chunk and Qdrant path; a typed slot cited
from the wrong labelled row is rejected and the retry finds the true row."""

import json
import uuid

import pytest

from doc_intel.core.config import get_settings
from doc_intel.core.tenancy import TenantContext
from doc_intel.ingestion.pipeline import parse_and_chunk
from doc_intel.mapping.engine import map_field
from doc_intel.mapping.retriever import Retriever
from doc_intel.templates.schemas import Constraints, IRField
from tests.fakes import FakeEmbedder, FakeLLM

pytestmark = pytest.mark.integration

CSV = b"Item,Amount\nBalance brought forward,4200\nClosing balance,5900\nBank fees,35\n"


def reply(value, chunk, quote) -> str:
    cand = {"value": value, "quotes": [{"chunk_id": str(chunk), "quote": quote}]}
    return json.dumps({"candidates": [cand], "not_found": False, "incomplete": False})


async def test_the_wrong_labelled_row_is_retried_to_the_true_row(vector_store, monkeypatch):
    monkeypatch.setenv("THINK_ON_ESCALATION", "true")
    get_settings.cache_clear()
    ctx, doc = TenantContext(f"user-{uuid.uuid4()}", uuid.uuid4()), uuid.uuid4()
    _, chunks = parse_and_chunk(
        CSV,
        "csv",
        document_id=doc,
        filename="ledger.csv",
        max_tokens=200,
        tokenizer_model="Qwen/Qwen3-Embedding-0.6B",
    )
    assert [r.id for c in chunks for r in c.rows or []] == ["T1-R01", "T1-R02", "T1-R03"]
    embedder = FakeEmbedder()
    await vector_store.upsert_chunks(
        ctx, chunks, await embedder.embed_documents([c.text for c in chunks])
    )
    field = IRField(
        field_id="closing",
        anchor_id="pptx:s1:sp1",
        section_path=["Slide 1"],
        label="Closing balance",
        description="The closing balance.",
        expected_type="currency",
        required=True,
        write_mode="replace_text",
        constraints=Constraints(),
    )
    from doc_intel.vector.qdrant_store import chunk_id

    cid = chunk_id(doc, chunks[0].chunk_index)
    llm = FakeLLM(
        reply("4200", cid, "| Balance brought forward | 4200 |"),
        reply("5900", cid, "| Closing balance | 5900 |"),
    )
    try:
        r = await map_field(ctx, field, [doc], Retriever(embedder, vector_store), llm)
    finally:
        get_settings.cache_clear()
    assert (r.status, r.value, len(llm.calls)) == ("populated", 5900, 2)
    assert [e.quote for e in r.evidence] == ["| Closing balance | 5900 |"]
    assert r.evidence[0].filename == "ledger.csv"
