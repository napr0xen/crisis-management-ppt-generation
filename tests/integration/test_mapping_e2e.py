"""Template -> extract_template -> source indexed -> POST mapping-run -> run_mapping job -> GET."""

import json
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from doc_intel.core.config import get_settings
from doc_intel.core.tenancy import TenantContext
from doc_intel.db.models import Job
from doc_intel.db.session import make_engine
from doc_intel.ingestion.schemas import ChunkPayload
from doc_intel.jobs.handlers import make_failure_hooks, make_handlers
from doc_intel.jobs.worker import run_one
from doc_intel.mapping import engine as engine_mod
from doc_intel.mapping.retriever import Retriever
from doc_intel.storage.local import LocalStorage
from doc_intel.templates.anchors import build_anchor_map
from doc_intel.templates.schemas import TEXT_KINDS
from doc_intel.vector.qdrant_store import chunk_id
from tests.conftest import headers
from tests.fakes import FakeEmbedder, FakeLLM
from tests.integration.test_extract_template_job import BUILDERS, queue_job
from tests.integration.test_mapping_runs_api import seed_document

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("empty_queue", "one_ir_batch")]

CHUNKS = [
    "The substation fire on Main Street caused a widespread outage on 3 March.",
    "Control room log A: 12,000 customers lost power after the fire.",
    "Control room log B: 15,000 customers lost power after the fire.",
]
SUMMARY_QUOTE = "substation fire on Main Street caused a widespread outage"


def draft_reply() -> str:
    anchors = [
        a for a in build_anchor_map("pptx", BUILDERS["pptx"]()).anchors if a.kind in TEXT_KINDS
    ]
    assert len(anchors) >= 3
    specs = [("incident_summary", "text"), ("spokesperson", "text"), ("customers", "number")]
    fields = [
        {
            "field_id": fid,
            "anchor_id": anchor.anchor_id,
            "section_path": anchor.path,
            "label": fid.replace("_", " ").title(),
            "description": f"The {fid}.",
            "expected_type": etype,
            "mode": "extractive",
            "required": True,
            "write_mode": "replace_text",
            "constraints": {},
        }
        for (fid, etype), anchor in zip(specs, anchors, strict=False)
    ]
    return json.dumps({"fields": fields})


def candidate(value, chunk, quote) -> dict:
    return {"value": value, "quotes": [{"chunk_id": str(chunk), "quote": quote}]}


def extraction(candidates, not_found=False) -> str:
    return json.dumps({"candidates": candidates, "not_found": not_found, "incomplete": False})


def batch(*per_slot) -> str:
    slots = [
        {"n": n, "candidates": c, "not_found": not c, "incomplete": False}
        for n, c in enumerate(per_slot, start=1)
    ]
    return json.dumps({"slots": slots})


def slot_count(user_message: str) -> int:
    """How many slots a `slot_values` user message asks for."""
    return len(json.loads(user_message.split("Slots:\n", 1)[1].split("\n\nChunks:", 1)[0])["slots"])


async def drain_one(migrated_db, vector_store, llm) -> None:
    engine = make_engine(migrated_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    settings = get_settings()
    embedder = FakeEmbedder()
    handlers = make_handlers(
        sm,
        LocalStorage(settings.storage_root),
        settings,
        embedder,
        vector_store,
        llm,
        Retriever(embedder, vector_store),
    )
    try:
        await run_one(sm, handlers, make_failure_hooks(sm))
    finally:
        await engine.dispose()


async def test_mapping_run_populates_misses_and_conflicts(
    client, session, migrated_db, vector_store, user_a, project, monkeypatch
):
    seen: dict[str, list[str]] = {}
    real_map_batch, real_map_field = engine_mod.map_batch, engine_mod.map_field

    async def spy_field(ctx, field, docs, retriever, llm, other_headings=()):
        seen[field.field_id] = list(other_headings)
        return await real_map_field(ctx, field, docs, retriever, llm, other_headings)

    async def spy(ctx, fields, docs, retriever, llm, siblings, opening=False):
        seen.update({f.field_id: list(siblings[f.field_id]) for f in fields})
        return await real_map_batch(ctx, fields, docs, retriever, llm, siblings, opening)

    monkeypatch.setattr(engine_mod, "map_batch", spy)
    monkeypatch.setattr(engine_mod, "map_field", spy_field)
    monkeypatch.setattr(
        engine_mod,
        "sibling_headings",
        lambda fields: {f.field_id: [f"Sib {f.field_id}"] for f in fields},
    )
    ctx = TenantContext(user_a, uuid.UUID(project))
    _, extract_job = await queue_job(client, session, user_a, project, "pptx")
    await drain_one(migrated_db, vector_store, FakeLLM(draft_reply()))
    session.expire_all()
    ir_id = (await session.get(Job, uuid.UUID(extract_job))).result["template_ir_id"]

    source = await seed_document(session, ctx, name="sitrep.txt")
    chunks = [
        ChunkPayload(
            document_id=source,
            filename="sitrep.txt",
            page_number=None,
            section_heading=None,
            element_type="text",
            chunk_index=i,
            text=text,
        )
        for i, text in enumerate(CHUNKS)
    ]
    await vector_store.upsert_chunks(ctx, chunks, await FakeEmbedder().embed_documents(CHUNKS))

    posted = client.post(
        f"/v1/projects/{project}/mapping-runs",
        json={"template_ir_id": ir_id, "document_ids": None},
        headers=headers(user_a),
    )
    assert posted.status_code == 202, posted.text
    run_id = posted.json()["mapping_run_id"]

    llm = FakeLLM(
        # slide 1 holds two loose fields: one batch call; slide 2's `customers` shares a batch
        # with the code-made field for its uncovered `{{incident_date}}` token
        batch([candidate("Substation fire", chunk_id(source, 0), SUMMARY_QUOTE)], []),
        batch(
            [
                candidate("12,000", chunk_id(source, 1), "12,000 customers lost power"),
                candidate("15,000", chunk_id(source, 2), "15,000 customers lost power"),
            ],
            [],
        ),
        # the fixture's empty stakeholder table gets a fill_table_rows field from code
        json.dumps({"rows": [], "not_found": True}),
        extraction([], not_found=True),  # slide 3's code-made `[note]` field, mapped alone
    )
    await drain_one(migrated_db, vector_store, llm)

    body = client.get(
        f"/v1/projects/{project}/mapping-runs/{run_id}", headers=headers(user_a)
    ).json()
    assert body["status"] == "completed"  # Phase 7 renders the artifact after verifying
    assert body["artifact"]["mapping_run_id"] == run_id
    assert body["finished_at"] is not None
    assert body["summary"] == {
        "populated": 1,
        "partial": 0,
        "missing": 4,  # + the code-made incident_date and note fields
        "conflict": 1,
        "warnings": [],
    }
    assert len(llm.calls) == 4
    assert seen == {fid: [f"Sib {fid}"] for fid in seen} and len(seen) == 5  # handler wiring

    summary = body["results"]["incident_summary"]
    assert summary["status"] == "populated"
    assert summary["value"] == "Substation fire"
    assert summary["checked_documents"] == [str(source)]
    (evidence,) = summary["evidence"]
    assert evidence["quote"] == SUMMARY_QUOTE
    assert (evidence["document_id"], evidence["chunk_id"]) == (
        str(source),
        str(chunk_id(source, 0)),
    )
    assert evidence["filename"] == "sitrep.txt"

    missing = body["results"]["spokesperson"]
    assert (missing["status"], missing["missing_reason"]) == ("missing", "not_found_by_llm")
    assert missing["value"] is None and missing["evidence"] == []

    conflict = body["results"]["customers"]
    assert conflict["status"] == "conflict"
    assert conflict["value"] is None
    assert [c["value"] for c in conflict["conflict_details"]["candidates"]] == [12000, 15000]
