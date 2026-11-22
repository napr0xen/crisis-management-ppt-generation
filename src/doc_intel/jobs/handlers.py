"""Job handlers by `jobs.kind`. Each is idempotent: a retry may find a crashed attempt's output."""

import asyncio
import hashlib
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import func

from doc_intel import writers
from doc_intel.core.config import Settings
from doc_intel.core.errors import Conflict, NotFound
from doc_intel.core.tenancy import TenantContext
from doc_intel.db import repo
from doc_intel.db.models import Job
from doc_intel.embeddings.schemas import Embedder
from doc_intel.ingestion.pipeline import parse_and_chunk
from doc_intel.ingestion.upload import artifact_key, parsed_key
from doc_intel.jobs.worker import Handler, OnFailed, Sessionmaker, set_progress
from doc_intel.llm.gateway import LLMClient
from doc_intel.mapping.engine import map_fields, summarize
from doc_intel.mapping.retriever import Retriever
from doc_intel.mapping.schemas import FieldMappingResult
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
_RENDERABLE = ("verifying", "rendering")
_OPEN_RUN = (*_ACTIVE_RUN, *_RENDERABLE)  # not yet completed or failed
# Fixed namespace: a retry derives the same artifact id (and key); run ids are random, so the
# ids stay unguessable.
_WRITER_CODES = frozenset({"anchor_unresolved", "token_not_found"})
_ARTIFACT_NS = uuid.UUID("5d0f2a52-3c1e-4c55-9a0e-7b1c6f3d8e21")


class NotRenderable(Conflict):
    retry = False  # the run's state will not change by retrying


def _artifact_stem(name: str) -> str:
    # shortcut: non-ASCII names become "_"; add RFC 5987 filename* when users need them.
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).stem)[:100].lstrip("._")
    return stem or "artifact"


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
        template_id = existing.id if existing else uuid.uuid4()

        async def on_batch(done: int, total: int, part: str | None) -> None:
            progress = {
                "stage": "ir",
                "done": done,
                "total": total,
                **({"part": part} if part else {}),
            }
            await set_progress(sessionmaker, ctx, job.id, progress)

        ir = await build_ir(llm, amap, template_id, on_batch=on_batch)
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

    async def render(ctx: TenantContext, run_id: uuid.UUID) -> uuid.UUID | None:
        async with sessionmaker.begin() as session:
            run = await repo.get_mapping_run(session, ctx, run_id)
            if run.status == "completed":  # a retry after the final commit
                artifact = await repo.get_artifact_by_run(session, ctx, run_id)
                return artifact.id if artifact else None
            if run.status not in _RENDERABLE:
                raise NotRenderable("The mapping run is not ready to render.")
            ir_row = await repo.get_template_ir_by_id(session, ctx, run.template_ir_id)
            template = await repo.get_template(session, ctx, ir_row.template_id)
            doc = await repo.get_document(session, ctx, template.document_id)
            await repo.update_mapping_run(session, ctx, run_id, status="rendering")
            fields = TemplateIR.model_validate(ir_row.ir).fields
            results = {k: FieldMappingResult.model_validate(v) for k, v in run.results.items()}
            summary, version = dict(run.summary), run.version
        fmt = template.format
        template_bytes = await asyncio.to_thread(read, doc.storage_key)
        try:
            data, warnings = await asyncio.to_thread(
                writers.render,
                fmt,
                template_bytes,
                fields,
                results,
                missing_marker=settings.missing_marker,
                conflict_marker=settings.conflict_marker,
            )
        except (KeyError, TypeError, ValueError) as exc:  # same input fails the same way
            # args are never logged unless they are a fixed writer code
            code = exc.args[0] if isinstance(exc, KeyError) and exc.args else None
            log.warning(
                "render_failed",
                mapping_run_id=str(run_id),
                format=fmt,
                error_type=type(exc).__name__,
                code=code if isinstance(code, str) and code in _WRITER_CODES else None,
            )
            raise NotRenderable("The template could not be rendered.") from None
        artifact_id = uuid.uuid5(_ARTIFACT_NS, str(run_id))
        key = artifact_key(ctx, artifact_id, fmt)
        try:
            await asyncio.to_thread(storage.put_bytes, key, data)
        except FileExistsError:  # an earlier attempt crashed after writing: its bytes win
            data = await asyncio.to_thread(read, key)
        summary["warnings"] = [*summary.get("warnings", []), *(w.model_dump() for w in warnings)]
        # shortcut: a tx2 that fails on every retry leaves the written artifact file unreferenced
        # (run is failed), add a cleanup sweep if storage grows.
        try:
            async with sessionmaker.begin() as session:
                await repo.update_mapping_run(
                    session,
                    ctx,
                    run_id,
                    status="completed",
                    finished_at=func.now(),
                    summary=summary,
                )
                await repo.create_artifact(
                    session,
                    ctx,
                    artifact_id=artifact_id,
                    mapping_run_id=run_id,
                    format=fmt,
                    filename=f"{_artifact_stem(doc.filename)}_v{version}.{fmt}",
                    storage_key=key,
                    sha256=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                )
        except NotFound:  # project deleted meanwhile (cascade): nothing to retry (ADR-010)
            await asyncio.to_thread(storage.delete, key)
            return None
        log.info(
            "artifact_rendered",
            mapping_run_id=str(run_id),
            artifact_id=str(artifact_id),
            format=fmt,
            size_bytes=len(data),
            warnings=len(warnings),
        )
        return artifact_id

    async def render_artifact(job: Job, ctx: TenantContext) -> dict[str, Any]:
        artifact_id = await render(ctx, uuid.UUID(job.payload["mapping_run_id"]))
        return {"artifact_id": str(artifact_id) if artifact_id else None}

    async def map_run(job: Job, ctx: TenantContext, run_id: uuid.UUID) -> None:
        async with sessionmaker.begin() as session:
            run = await repo.get_mapping_run(session, ctx, run_id)
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
        async def on_field(done: int, field_id: str) -> None:
            # field_id is template-derived and goes to jobs.progress only, never to logs
            extra = {"field": field_id} if field_id else {}
            await set_progress(sessionmaker, ctx, job.id, {**progress, "done": done, **extra})

        results = await map_fields(ctx, ir.fields, document_ids, retriever, llm, on_field)
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

    async def run_mapping(job: Job, ctx: TenantContext) -> dict[str, Any]:
        run_id = uuid.UUID(job.payload["mapping_run_id"])
        async with sessionmaker() as session:
            status = (await repo.get_mapping_run(session, ctx, run_id)).status
        if status == "failed":
            return {"mapping_run_id": str(run_id), "artifact_id": None}
        if status in _ACTIVE_RUN:  # else a retry after the mapping commit: only render
            await map_run(job, ctx, run_id)
        artifact_id = await render(ctx, run_id)
        return {
            "mapping_run_id": str(run_id),
            "artifact_id": str(artifact_id) if artifact_id else None,
        }

    return {
        "ingest_document": ingest_document,
        "extract_template": extract_template,
        "run_mapping": run_mapping,
        "render_artifact": render_artifact,
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

    def run_failed(open_statuses: tuple[str, ...]) -> OnFailed:
        async def hook(job: Job, ctx: TenantContext, code: str) -> None:
            async with sessionmaker.begin() as session:
                run_id = uuid.UUID(job.payload["mapping_run_id"])
                try:
                    run = await repo.get_mapping_run(session, ctx, run_id)
                    if run.status in open_statuses:  # a completed run stays
                        await repo.update_mapping_run(
                            session, ctx, run_id, status="failed", finished_at=func.now()
                        )
                except NotFound:
                    pass  # hooks also run at startup recovery: never raise

        return hook

    return {
        "ingest_document": ingest_failed,
        "run_mapping": run_failed(_OPEN_RUN),
        # a render job failing on a still-mapping run must not kill that run
        "render_artifact": run_failed(_RENDERABLE),
    }
