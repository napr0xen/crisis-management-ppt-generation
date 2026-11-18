"""Tenant-scoped queries: every function filters on `user_id` (+ `project_id` for children).

Functions flush but never commit; the caller's transaction (one per request) decides.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from doc_intel.core.errors import Conflict, NotFound
from doc_intel.db.models import Document, Job, Project

if TYPE_CHECKING:  # core.tenancy imports this module
    from doc_intel.core.tenancy import TenantContext


def _document_count():
    return (
        select(func.count())
        .where(Document.project_id == Project.id, Document.user_id == Project.user_id)
        .correlate(Project)
        .scalar_subquery()
    )


async def create_project(
    session: AsyncSession, user_id: str, name: str, openwebui_chat_id: str | None = None
) -> Project:
    project = Project(user_id=user_id, name=name, openwebui_chat_id=openwebui_chat_id)
    session.add(project)
    await session.flush()
    await session.refresh(project)
    return project


async def list_projects(
    session: AsyncSession, user_id: str, limit: int
) -> list[tuple[Project, int]]:
    rows = await session.execute(
        select(Project, _document_count())
        .where(Project.user_id == user_id)
        .order_by(Project.created_at.desc())
        .limit(limit)
    )
    return [(p, n) for p, n in rows.all()]


async def get_project(
    session: AsyncSession, user_id: str, project_id: uuid.UUID
) -> tuple[Project, int]:
    row = (
        await session.execute(
            select(Project, _document_count()).where(
                Project.id == project_id, Project.user_id == user_id
            )
        )
    ).first()
    if row is None:
        raise NotFound()
    return row[0], row[1]


async def ensure_project(session: AsyncSession, user_id: str, project_id: uuid.UUID) -> None:
    found = await session.scalar(
        select(Project.id).where(Project.id == project_id, Project.user_id == user_id)
    )
    if found is None:
        raise NotFound()


def _scoped(model, ctx: TenantContext, row_id: uuid.UUID):
    return (model.id == row_id, model.user_id == ctx.user_id, model.project_id == ctx.project_id)


async def create_document(
    session: AsyncSession,
    ctx: TenantContext,
    document_id: uuid.UUID,
    *,
    filename: str,
    mime_type: str,
    size_bytes: int,
    sha256: str,
    storage_key: str,
) -> Document:
    document = Document(
        id=document_id,
        user_id=ctx.user_id,
        project_id=ctx.project_id,
        role="source",
        filename=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        sha256=sha256,
        storage_key=storage_key,
        status="uploaded",
    )
    session.add(document)
    await session.flush()
    await session.refresh(document)
    return document


async def find_document_by_sha256(
    session: AsyncSession, ctx: TenantContext, sha256: str
) -> Document | None:
    return await session.scalar(
        select(Document).where(
            Document.user_id == ctx.user_id,
            Document.project_id == ctx.project_id,
            Document.sha256 == sha256,
        )
    )


async def list_documents(session: AsyncSession, ctx: TenantContext, limit: int) -> list[Document]:
    rows = await session.scalars(
        select(Document)
        .where(Document.user_id == ctx.user_id, Document.project_id == ctx.project_id)
        .order_by(Document.created_at.desc())
        .limit(limit)
    )
    return list(rows)


async def get_document(
    session: AsyncSession, ctx: TenantContext, document_id: uuid.UUID
) -> Document:
    document = await session.scalar(select(Document).where(*_scoped(Document, ctx, document_id)))
    if document is None:
        raise NotFound()
    return document


async def update_document(
    session: AsyncSession, ctx: TenantContext, document_id: uuid.UUID, **values: Any
) -> None:
    result = await session.execute(
        update(Document).where(*_scoped(Document, ctx, document_id)).values(**values)
    )
    if result.rowcount == 0:
        raise NotFound()


async def delete_document(
    session: AsyncSession, ctx: TenantContext, document_id: uuid.UUID
) -> Document:
    """Delete the row and return it. A template that still has mapping runs is `Conflict`."""
    document = await get_document(session, ctx, document_id)
    try:
        await session.execute(delete(Document).where(*_scoped(Document, ctx, document_id)))
    except IntegrityError as exc:
        # templates/template_irs cascade, then mapping_runs.template_ir_id (no ON DELETE) blocks.
        raise Conflict("Document is a template with mapping runs.") from exc
    return document


async def create_job(
    session: AsyncSession, ctx: TenantContext, kind: str, payload: dict[str, Any]
) -> Job:
    job = Job(user_id=ctx.user_id, project_id=ctx.project_id, kind=kind, payload=payload)
    session.add(job)
    await session.flush()
    await session.refresh(job)
    return job


async def get_job(session: AsyncSession, ctx: TenantContext, job_id: uuid.UUID) -> Job:
    job = await session.scalar(select(Job).where(*_scoped(Job, ctx, job_id)))
    if job is None:
        raise NotFound()
    return job


async def update_job(
    session: AsyncSession, ctx: TenantContext, job_id: uuid.UUID, **values: Any
) -> None:
    result = await session.execute(update(Job).where(*_scoped(Job, ctx, job_id)).values(**values))
    if result.rowcount == 0:
        raise NotFound()
