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
from doc_intel.db.models import (
    Artifact,
    Document,
    Job,
    MappingRun,
    Project,
    Template,
    TemplateIRVersion,
)

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


async def get_template(
    session: AsyncSession, ctx: TenantContext, template_id: uuid.UUID
) -> Template:
    template = await session.scalar(select(Template).where(*_scoped(Template, ctx, template_id)))
    if template is None:
        raise NotFound()
    return template


async def find_template_by_document(
    session: AsyncSession, ctx: TenantContext, document_id: uuid.UUID
) -> Template | None:
    return await session.scalar(
        select(Template).where(
            Template.user_id == ctx.user_id,
            Template.project_id == ctx.project_id,
            Template.document_id == document_id,
        )
    )


async def create_template(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    template_id: uuid.UUID,
    document_id: uuid.UUID,
    format: str,
    anchor_map: dict[str, Any],
    anchor_count: int,
) -> Template:
    template = Template(
        id=template_id,
        user_id=ctx.user_id,
        project_id=ctx.project_id,
        document_id=document_id,
        format=format,
        anchor_map=anchor_map,
        anchor_count=anchor_count,
    )
    session.add(template)
    await session.flush()
    await session.refresh(template)
    return template


async def create_template_ir(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    template_id: uuid.UUID,
    version: int,
    ir: dict[str, Any],
) -> TemplateIRVersion:
    row = TemplateIRVersion(
        user_id=ctx.user_id,
        project_id=ctx.project_id,
        template_id=template_id,
        version=version,
        ir=ir,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def latest_template_ir(
    session: AsyncSession, ctx: TenantContext, template_id: uuid.UUID
) -> TemplateIRVersion | None:
    return await session.scalar(
        select(TemplateIRVersion)
        .where(
            TemplateIRVersion.user_id == ctx.user_id,
            TemplateIRVersion.project_id == ctx.project_id,
            TemplateIRVersion.template_id == template_id,
        )
        .order_by(TemplateIRVersion.version.desc())
        .limit(1)
    )


async def get_template_ir(
    session: AsyncSession, ctx: TenantContext, template_id: uuid.UUID, version: int
) -> TemplateIRVersion:
    row = await session.scalar(
        select(TemplateIRVersion).where(
            TemplateIRVersion.user_id == ctx.user_id,
            TemplateIRVersion.project_id == ctx.project_id,
            TemplateIRVersion.template_id == template_id,
            TemplateIRVersion.version == version,
        )
    )
    if row is None:
        raise NotFound()
    return row


async def get_template_ir_by_id(
    session: AsyncSession, ctx: TenantContext, template_ir_id: uuid.UUID
) -> TemplateIRVersion:
    row = await session.scalar(
        select(TemplateIRVersion).where(*_scoped(TemplateIRVersion, ctx, template_ir_id))
    )
    if row is None:
        raise NotFound()
    return row


async def get_documents(
    session: AsyncSession, ctx: TenantContext, document_ids: list[uuid.UUID]
) -> list[Document]:
    """The subset of `document_ids` this tenant owns, in no particular order."""
    rows = await session.scalars(
        select(Document).where(
            Document.user_id == ctx.user_id,
            Document.project_id == ctx.project_id,
            Document.id.in_(document_ids),
        )
    )
    return list(rows)


async def indexed_source_document_ids(session: AsyncSession, ctx: TenantContext) -> list[uuid.UUID]:
    rows = await session.scalars(
        select(Document.id)
        .where(
            Document.user_id == ctx.user_id,
            Document.project_id == ctx.project_id,
            Document.role == "source",
            Document.status == "indexed",
        )
        .order_by(Document.created_at, Document.id)
    )
    return list(rows)


async def create_mapping_run(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    template_ir_id: uuid.UUID,
    document_ids: list[uuid.UUID],
) -> MappingRun:
    """Next version for this IR. A concurrent insert of the same version loses the UNIQUE race:
    retry once with a fresh max, then `Conflict`."""
    for _ in range(2):
        version = await session.scalar(
            select(func.coalesce(func.max(MappingRun.version), 0) + 1).where(
                MappingRun.user_id == ctx.user_id,
                MappingRun.project_id == ctx.project_id,
                MappingRun.template_ir_id == template_ir_id,
            )
        )
        run = MappingRun(
            user_id=ctx.user_id,
            project_id=ctx.project_id,
            template_ir_id=template_ir_id,
            version=version,
            trigger="initial",
            status="queued",
            document_ids=document_ids,
        )
        try:
            async with session.begin_nested():
                session.add(run)
                await session.flush()
        except IntegrityError:
            continue
        await session.refresh(run)
        return run
    raise Conflict("Could not allocate a mapping run version.")


async def get_mapping_run(
    session: AsyncSession, ctx: TenantContext, run_id: uuid.UUID
) -> MappingRun:
    run = await session.scalar(select(MappingRun).where(*_scoped(MappingRun, ctx, run_id)))
    if run is None:
        raise NotFound()
    return run


async def update_mapping_run(
    session: AsyncSession, ctx: TenantContext, run_id: uuid.UUID, **values: Any
) -> None:
    result = await session.execute(
        update(MappingRun).where(*_scoped(MappingRun, ctx, run_id)).values(**values)
    )
    if result.rowcount == 0:
        raise NotFound()


async def create_artifact(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    artifact_id: uuid.UUID,
    mapping_run_id: uuid.UUID,
    format: str,
    filename: str,
    storage_key: str,
    sha256: str,
    size_bytes: int,
) -> Artifact:
    artifact = Artifact(
        id=artifact_id,
        user_id=ctx.user_id,
        project_id=ctx.project_id,
        mapping_run_id=mapping_run_id,
        format=format,
        filename=filename,
        storage_key=storage_key,
        sha256=sha256,
        size_bytes=size_bytes,
    )
    session.add(artifact)
    await session.flush()
    await session.refresh(artifact)
    return artifact


async def get_artifact_by_run(
    session: AsyncSession, ctx: TenantContext, run_id: uuid.UUID
) -> Artifact | None:
    return await session.scalar(
        select(Artifact).where(
            Artifact.user_id == ctx.user_id,
            Artifact.project_id == ctx.project_id,
            Artifact.mapping_run_id == run_id,
        )
    )


async def list_artifacts(session: AsyncSession, ctx: TenantContext, limit: int) -> list[Artifact]:
    rows = await session.scalars(
        select(Artifact)
        .where(Artifact.user_id == ctx.user_id, Artifact.project_id == ctx.project_id)
        .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        .limit(limit)
    )
    return list(rows)


async def get_artifact_for_download(session: AsyncSession, artifact_id: uuid.UUID) -> Artifact:
    """The one unscoped lookup (data-model.md tenancy exception 1): the caller must verify the
    HMAC against the returned row's `user_id` before returning anything from it."""
    artifact = await session.get(Artifact, artifact_id)
    if artifact is None:
        raise NotFound()
    return artifact
