"""Job handlers by `jobs.kind`. Each is idempotent: a retry may find a crashed attempt's output."""

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

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
from doc_intel.storage.base import StorageBackend
from doc_intel.templates.anchors import build_anchor_map
from doc_intel.templates.ir_builder import build_ir

if TYPE_CHECKING:
    from doc_intel.vector.qdrant_store import QdrantStore

# Chunks embedded and upserted per step: only one batch of vectors (~32 KB each) is in memory.
INDEX_BATCH = 256


def make_handlers(
    sessionmaker: Sessionmaker,
    storage: StorageBackend,
    settings: Settings,
    embedder: Embedder,
    vector_store: "QdrantStore",
    llm: LLMClient,
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
        # shortcut: two concurrent extract_template jobs for one document: the second fails on
        # templates.document_id UNIQUE; fine at JOB_CONCURRENCY=1.
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

    return {"ingest_document": ingest_document, "extract_template": extract_template}


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

    return {"ingest_document": ingest_failed}
