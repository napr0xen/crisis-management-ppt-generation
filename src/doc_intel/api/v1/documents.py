import asyncio
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict

from doc_intel.core.tenancy import Tenant
from doc_intel.db import repo
from doc_intel.db.session import Session
from doc_intel.ingestion.upload import parsed_key, upload_key, validate_upload

router = APIRouter(prefix="/projects/{project_id}/documents")


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_id: uuid.UUID
    role: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    status: str
    page_count: int | None
    chunk_count: int | None
    error_code: str | None
    created_at: datetime


class DocumentList(BaseModel):
    items: list[DocumentOut]


class UploadOut(BaseModel):
    document_id: uuid.UUID
    job_id: uuid.UUID | None
    deduplicated: bool


@router.post("", status_code=201)
async def upload_document(
    file: UploadFile, ctx: Tenant, session: Session, request: Request
) -> UploadOut:
    # shortcut: the multipart body is spooled (to disk past 1 MB) before auth and the size check
    # run, upgrade to a streaming size cap if the API is ever exposed beyond localhost.
    max_bytes = request.app.state.settings.max_upload_mb * 1024 * 1024
    data = await file.read(max_bytes + 1)
    upload = await asyncio.to_thread(validate_upload, file.filename, data, max_bytes)

    existing = await repo.find_document_by_sha256(session, ctx, upload.sha256)
    if existing is not None and existing.status == "failed":
        # Same bytes after a failure (e.g. a model download that failed offline): retry ingestion.
        await repo.update_document(
            session,
            ctx,
            existing.id,
            status="uploaded",
            error_code=None,
            page_count=None,
            chunk_count=None,
        )
        job = await repo.create_job(
            session, ctx, "ingest_document", {"document_id": str(existing.id)}
        )
        return UploadOut(document_id=existing.id, job_id=job.id, deduplicated=True)
    if existing is not None:
        return UploadOut(document_id=existing.id, job_id=None, deduplicated=True)

    # shortcut: two concurrent identical uploads race on UNIQUE (project_id, sha256) and the
    # loser gets a 500; a retry deduplicates. Add a savepoint + re-select if clients upload in
    # parallel.
    document_id = uuid.uuid4()
    key = upload_key(ctx, document_id, upload.ext)
    await repo.create_document(
        session,
        ctx,
        document_id,
        filename=file.filename,
        mime_type=upload.mime_type,
        size_bytes=upload.size_bytes,
        sha256=upload.sha256,
        storage_key=key,
    )
    job = await repo.create_job(session, ctx, "ingest_document", {"document_id": str(document_id)})
    # Rows are flushed first, so a DB error never leaves a file behind.
    # shortcut: a failed commit after this write orphans the file, sweep uploads/ without a row
    # if that ever happens.
    await asyncio.to_thread(request.app.state.storage.put_bytes, key, upload.data)
    return UploadOut(document_id=document_id, job_id=job.id, deduplicated=False)


@router.get("")
async def list_documents(
    ctx: Tenant, session: Session, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> DocumentList:
    return DocumentList(items=await repo.list_documents(session, ctx, limit))


@router.get("/{document_id}")
async def get_document(document_id: uuid.UUID, ctx: Tenant, session: Session) -> DocumentOut:
    return DocumentOut.model_validate(await repo.get_document(session, ctx, document_id))


@router.delete("/{document_id}", status_code=204)
async def delete_document(
    document_id: uuid.UUID,
    ctx: Tenant,
    session: Session,
    request: Request,
    background: BackgroundTasks,
) -> None:
    document = await repo.delete_document(session, ctx, document_id)
    # Background tasks run after the response, i.e. after the commit: a rolled-back delete
    # never loses its files.
    storage = request.app.state.storage
    background.add_task(storage.delete, document.storage_key)
    background.add_task(storage.delete, parsed_key(ctx, document_id))
