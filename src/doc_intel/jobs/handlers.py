"""Job handlers by `jobs.kind`. Each is idempotent: a retry may find a crashed attempt's output."""

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import func

from doc_intel.core.config import Settings
from doc_intel.core.errors import NotFound
from doc_intel.core.tenancy import TenantContext
from doc_intel.db import repo
from doc_intel.db.models import Job
from doc_intel.embeddings.schemas import Embedder
from doc_intel.ingestion.pipeline import parse_and_chunk
from doc_intel.ingestion.upload import parsed_key
from doc_intel.jobs.worker import Handler, OnFailed, Sessionmaker, set_progress
from doc_intel.llm.gateway import LLMClient
from doc_intel.mapping.engine import map_field, summarize
from doc_intel.mapping.retriever import Retriever
from doc_intel.storage.base import StorageBackend
from doc_intel.templates.anchors import build_anchor_map
from doc_intel.templates.ir_builder import build_ir
from doc_intel.templates.schemas import TemplateIR

if TYPE_CHECKING:
    from doc_intel.vector.qdrant_store import QdrantStore

log = structlog.get_logger()

# Chunks embedded and upserted per step: only one batch of vectors (~32 KB each) is in memory.
INDEX_BATCH = 256

_ACTIVE_RUN = ("queued", "retrieving", "extracting")  # statuses a run_mapping job may still change


def make_handlers(
    sessionmaker: Sessionmaker,
    storage: StorageBackend,
    settings: Settings,
    embedder: Embedder,
    vector_store: "QdrantStore",
    llm: LLMClient,
    retriever: Retriever,
) -> dict[str, Handler]:
    async def document_exists(ctx: TenantContext, document_id: uuid.UUID) -> bool:
        async with sessionmaker() as session:
            try:
                await repo.get_document(session, ctx, document_id)
            except NotFound:
                return False
        return True

    def read(key: str) -> bytes:
        with storage.open_read(key) as f:
            return f.read()

    async def ingest_document(job: Job, ctx: TenantContext) -> dict[str, Any]:
        document_id = uuid.UUID(job.payload["document_id"])
        key = parsed_key(ctx, document_id)
        try:
            async with sessionmaker.begin() as session:
                doc = await repo.get_document(session, ctx, document_id)
                if doc.role == "template":  # template text must never be evidence (data-model.md)
                    return {}
                await repo.update_document(session, ctx, document_id, status="parsing")
            await set_progress(sessionmaker, ctx, job.id, {"stage": "parsing"})
            # File I/O in threads: the worker shares its event loop with the API.
            data = await asyncio.to_thread(read, doc.storage_key)
            parsed, chunks = await asyncio.to_thread(
                parse_and_chunk,
                data,
                doc.storage_key.rsplit(".", 1)[1],  # uploads/{id}/original.{ext}
                document_id=document_id,
                filename=doc.filename,
                max_tokens=settings.chunk_max_tokens,
                tokenizer_model=settings.chunk_tokenizer,
            )
            # overwrite: derived data a crashed attempt may have left behind
            await asyncio.to_thread(
                storage.put_bytes, key, parsed.model_dump_json().encode(), overwrite=True
            )
            total = len(chunks)
            result = {"page_count": parsed.page_count, "chunk_count": total}
            progress = {"stage": "embedding", "total": total}
            await set_progress(sessionmaker, ctx, job.id, {**progress, "done": 0})
            await vector_store.ensure_collection()  # Qdrant may have been down at app startup
            # A retry or re-ingest may yield fewer chunks: overwriting by id would leave the old
            # higher chunk_index points searchable.
            await vector_store.delete_document_points(ctx, document_id)
            for start in range(0, total, INDEX_BATCH):
                batch = chunks[start : start + INDEX_BATCH]
                embeddings = await embedder.embed_documents([c.text for c in batch])
                await vector_store.upsert_chunks(ctx, batch, embeddings)
                done = start + len(batch)
                await set_progress(sessionmaker, ctx, job.id, {**progress, "done": done})
            async with sessionmaker.begin() as session:
                await repo.update_document(session, ctx, document_id, status="indexed", **result)
            return result
        except Exception:
            if await document_exists(ctx, document_id):
                raise
            # Deleted by the user before or during this job: nothing to retry, and none of its
            # text may stay searchable or on disk (FR-16).
            # shortcut: if Qdrant stays down through every attempt, points upserted after the
            # user's delete outlive the row; add a sweep for orphan document_ids if that happens.
            await vector_store.delete_document_points(ctx, document_id)
            await asyncio.to_thread(storage.delete, key)
            return {}

    async def extract_template(job: Job, ctx: TenantContext) -> dict[str, Any]:
        document_id = uuid.UUID(job.payload["document_id"])
        async with sessionmaker.begin() as session:
            doc = await repo.get_document(session, ctx, document_id)
            existing = await repo.find_template_by_document(session, ctx, document_id)
            latest = await repo.latest_template_ir(session, ctx, existing.id) if existing else None
        if existing and latest:  # a re-POST or a retry after commit: reuse, no LLM call
            return {
                "template_id": str(existing.id),
                "template_ir_id": str(latest.id),
                "ir_version": latest.version,
            }
        fmt = doc.storage_key.rsplit(".", 1)[1]
        await set_progress(sessionmaker, ctx, job.id, {"stage": "anchors"})
        data = await asyncio.to_thread(read, doc.storage_key)
        amap = await asyncio.to_thread(build_anchor_map, fmt, data)
        await set_progress(sessionmaker, ctx, job.id, {"stage": "ir"})
        template_id = existing.id if existing else uuid.uuid4()
        ir = await build_ir(llm, amap, template_id)
        # shortcut: two concurrent extract_template jobs for one document: the loser hits
        # templates.document_id UNIQUE, is retried by the worker and then reuses the winner's row
        # (one wasted LLM call); fine at JOB_CONCURRENCY=1.
        async with sessionmaker.begin() as session:
            if existing is None:
                await repo.create_template(
                    session,
                    ctx,
                    template_id=template_id,
                    document_id=document_id,
                    format=fmt,
                    anchor_map=amap.model_dump(mode="json"),
                    anchor_count=len(amap.anchors),
                )
            row = await repo.create_template_ir(
                session,
                ctx,
                template_id=template_id,
                version=1,
                ir=ir.model_dump(mode="json", exclude_none=True),
            )
        return {"template_id": str(template_id), "template_ir_id": str(row.id), "ir_version": 1}

    async def run_mapping(job: Job, ctx: TenantContext) -> dict[str, Any]:
        run_id = uuid.UUID(job.payload["mapping_run_id"])
        result = {"mapping_run_id": str(run_id), "artifact_id": None}
        async with sessionmaker.begin() as session:
            run = await repo.get_mapping_run(session, ctx, run_id)
            if run.status not in _ACTIVE_RUN:  # retry after the final commit
                return result
            ir = TemplateIR.model_validate(
                (await repo.get_template_ir_by_id(session, ctx, run.template_ir_id)).ir
            )
            document_ids = list(run.document_ids)
            await repo.update_mapping_run(session, ctx, run_id, status="extracting")
            total = len(ir.fields)
            progress = {"stage": "mapping", "total": total}
            await repo.update_job(session, ctx, job.id, progress={**progress, "done": 0})
        # shortcut: fields run one at a time (one Ollama), and a crash redoes them all; add
        # per-field checkpoints if runs get long.
        results = {}
        for done, field in enumerate(ir.fields, start=1):
            results[field.field_id] = await map_field(ctx, field, document_ids, retriever, llm)
            await set_progress(sessionmaker, ctx, job.id, {**progress, "done": done})
        summary = summarize(ir.fields, results)
        async with sessionmaker.begin() as session:
            await repo.update_mapping_run(
                session,
                ctx,
                run_id,
                results={fid: r.model_dump(mode="json") for fid, r in results.items()},
                summary=summary.model_dump(mode="json"),
                status="verifying",
            )
        log.info(
            "mapping_run_done",
            mapping_run_id=str(run_id),
            populated=summary.populated,
            partial=summary.partial,
            missing=summary.missing,
            conflict=summary.conflict,
        )
        return result

    return {
        "ingest_document": ingest_document,
        "extract_template": extract_template,
        "run_mapping": run_mapping,
    }


def make_failure_hooks(sessionmaker: Sessionmaker) -> dict[str, OnFailed]:
    async def ingest_failed(job: Job, ctx: TenantContext, code: str) -> None:
        async with sessionmaker.begin() as session:
            try:
                await repo.update_document(
                    session,
                    ctx,
                    uuid.UUID(job.payload["document_id"]),
                    status="failed",
                    error_code=code,
                )
            except NotFound:
                pass  # deleted meanwhile: nothing to mark

    async def mapping_failed(job: Job, ctx: TenantContext, code: str) -> None:
        async with sessionmaker.begin() as session:
            run_id = uuid.UUID(job.payload["mapping_run_id"])
            try:
                run = await repo.get_mapping_run(session, ctx, run_id)
                if run.status in _ACTIVE_RUN:  # a committed verifying/completed run stays
                    await repo.update_mapping_run(
                        session, ctx, run_id, status="failed", finished_at=func.now()
                    )
            except NotFound:
                pass  # hooks also run at startup recovery: never raise

    return {"ingest_document": ingest_failed, "run_mapping": mapping_failed}
