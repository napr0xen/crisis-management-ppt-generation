import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request
from pydantic import BaseModel, ConfigDict

from doc_intel.core.errors import Conflict, UnsupportedMediaType, UpstreamUnavailable
from doc_intel.core.tenancy import Tenant
from doc_intel.db import repo
from doc_intel.db.session import Session

router = APIRouter(prefix="/projects/{project_id}/templates")

TEMPLATE_EXTENSIONS = {"pptx", "docx", "xlsx"}


class TemplateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: uuid.UUID


class TemplateJobOut(BaseModel):
    job_id: uuid.UUID


class TemplateOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    format: str
    anchor_count: int
    latest_ir_version: int | None
    anchor_map: dict[str, Any]


class TemplateIROut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    version: int
    ir: dict[str, Any]
    created_at: datetime


@router.post("", status_code=202)
async def create_template(
    body: TemplateIn, ctx: Tenant, session: Session, request: Request
) -> TemplateJobOut:
    doc = await repo.get_document(session, ctx, body.document_id)
    # The extension was validated at upload; the filename is never trusted.
    if doc.storage_key.rsplit(".", 1)[1] not in TEMPLATE_EXTENSIONS:
        raise UnsupportedMediaType("Only PPTX, DOCX and XLSX documents can be templates.")
    if doc.status in ("uploaded", "parsing"):
        # A running ingest job would re-add the template's text to Qdrant after the purge below.
        raise Conflict("The document is still being ingested.")
    await repo.update_document(session, ctx, doc.id, role="template")
    job = await repo.create_job(session, ctx, "extract_template", {"document_id": str(doc.id)})
    # Template text must not be searchable; if Qdrant is down the transaction rolls back.
    try:
        await request.app.state.vector_store.delete_document_points(ctx, doc.id)
    except Exception as exc:
        raise UpstreamUnavailable() from exc
    return TemplateJobOut(job_id=job.id)


@router.get("/{template_id}")
async def get_template(template_id: uuid.UUID, ctx: Tenant, session: Session) -> TemplateOut:
    template = await repo.get_template(session, ctx, template_id)
    latest = await repo.latest_template_ir(session, ctx, template_id)
    return TemplateOut(
        id=template.id,
        document_id=template.document_id,
        format=template.format,
        anchor_count=template.anchor_count,
        latest_ir_version=latest.version if latest else None,
        anchor_map=template.anchor_map,
    )


@router.get("/{template_id}/irs/{version}")
async def get_template_ir(
    template_id: uuid.UUID, version: Annotated[int, Path(ge=1)], ctx: Tenant, session: Session
) -> TemplateIROut:
    return TemplateIROut.model_validate(
        await repo.get_template_ir(session, ctx, template_id, version)
    )
