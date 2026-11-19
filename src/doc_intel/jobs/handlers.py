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
from doc_intel.storage.base import StorageBackend

if TYPE_CHECKING:
    from doc_intel.vector.qdrant_store import QdrantStore


def make_handlers(
    sessionmaker: Sessionmaker,
    storage: StorageBackend,
    settings: Settings,
    embedder: Embedder,
    vector_store: "QdrantStore",
) -> dict[str, Handler]:
    async def document_exists(ctx: TenantContext, document_id: uuid.UUID) -> bool:
        async with sessionmaker() as session:
            try:
                await repo.get_document(session, ctx, document_id)
            except NotFound:
                return False
        return True

    async def ingest_document(job: Job, ctx: TenantContext) -> dict[str, Any]:
        document_id = uuid.UUID(job.payload["document_id"])
        key = parsed_key(ctx, document_id)
        try:
            async with sessionmaker.begin() as session:
                doc = await repo.get_document(session, ctx, document_id)
                await repo.update_document(session, ctx, document_id, status="parsing")
            await set_progress(sessionmaker, ctx, job.id, {"stage": "parsing"})
            with storage.open_read(doc.storage_key) as f:
                data = f.read()
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
            storage.put_bytes(key, parsed.model_dump_json().encode(), overwrite=True)
            result = {"page_count": parsed.page_count, "chunk_count": len(chunks)}
            texts = [c.text for c in chunks]
            await set_progress(
                sessionmaker, ctx, job.id, {"stage": "embedding", "total": len(texts)}
            )
            embeddings = await embedder.embed_documents(texts) if texts else []
            await set_progress(sessionmaker, ctx, job.id, {"stage": "indexing"})
            await vector_store.ensure_collection()  # Qdrant may have been down at app startup
            # A retry or re-ingest may yield fewer chunks: overwriting by id would leave the old
            # higher chunk_index points searchable.
            await vector_store.delete_document_points(ctx, document_id)
            if chunks:
                await vector_store.upsert_chunks(ctx, chunks, embeddings)
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
            storage.delete(key)
            return {}

    return {"ingest_document": ingest_document}


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
