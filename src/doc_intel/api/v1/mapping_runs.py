import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from doc_intel.api.v1.artifacts import ArtifactOut, artifact_out
from doc_intel.core.errors import NotFound, ValidationFailed
from doc_intel.core.tenancy import Tenant, TenantContext
from doc_intel.db import repo
from doc_intel.db.session import Session
from doc_intel.mapping.schemas import FieldMappingResult

router = APIRouter(prefix="/projects/{project_id}/mapping-runs")


class MappingRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_ir_id: uuid.UUID
    document_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=200)] | None = None


class MappingRunJobOut(BaseModel):
    mapping_run_id: uuid.UUID
    job_id: uuid.UUID


class MappingRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    template_ir_id: uuid.UUID
    parent_run_id: uuid.UUID | None
    version: int
    trigger: Literal["initial", "edit"]
    status: Literal[
        "queued", "retrieving", "extracting", "verifying", "rendering", "completed", "failed"
    ]
    document_ids: list[uuid.UUID]
    summary: dict[str, Any]
    results: dict[str, FieldMappingResult]
    artifact: ArtifactOut | None = None
    created_at: datetime
    finished_at: datetime | None


async def _resolve_documents(
    session: Session, ctx: TenantContext, requested: list[uuid.UUID] | None
) -> list[uuid.UUID]:
    if requested is None:
        ids = await repo.indexed_source_document_ids(session, ctx)
    else:
        ids = list(dict.fromkeys(requested))
        docs = {d.id: d for d in await repo.get_documents(session, ctx, ids)}
        if len(docs) != len(ids):
            raise NotFound()
        if any(d.role != "source" or d.status != "indexed" for d in docs.values()):
            raise ValidationFailed("Every document must be an indexed source document.")
    if not ids:
        raise ValidationFailed("The project has no indexed source documents.")
    return ids


@router.post("", status_code=202)
async def create_mapping_run(body: MappingRunIn, ctx: Tenant, session: Session) -> MappingRunJobOut:
    ir = await repo.get_template_ir_by_id(session, ctx, body.template_ir_id)
    ids = await _resolve_documents(session, ctx, body.document_ids)
    run = await repo.create_mapping_run(session, ctx, template_ir_id=ir.id, document_ids=ids)
    job = await repo.create_job(session, ctx, "run_mapping", {"mapping_run_id": str(run.id)})
    return MappingRunJobOut(mapping_run_id=run.id, job_id=job.id)


@router.get("/{run_id}")
async def get_mapping_run(
    run_id: uuid.UUID, ctx: Tenant, session: Session, request: Request
) -> MappingRunOut:
    out = MappingRunOut.model_validate(await repo.get_mapping_run(session, ctx, run_id))
    artifact = await repo.get_artifact_by_run(session, ctx, run_id)
    if artifact is not None:
        out.artifact = artifact_out(artifact, request.app.state.settings)
    return out
