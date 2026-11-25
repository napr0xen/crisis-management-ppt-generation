import asyncio
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from doc_intel.api.v1.artifacts import ArtifactOut, artifact_out
from doc_intel.chat.edits import ChooseCandidate, EditOp, EditsIn, SetValue, apply_edits
from doc_intel.core.errors import Conflict, NotFound, ValidationFailed
from doc_intel.core.tenancy import Tenant, TenantContext
from doc_intel.db import repo
from doc_intel.db.models import Job, MappingRun
from doc_intel.db.session import Session
from doc_intel.mapping.engine import shared_facts, summarize
from doc_intel.mapping.schemas import FieldMappingResult, Value
from doc_intel.templates.schemas import IRField, TemplateIR

router = APIRouter(prefix="/projects/{project_id}/mapping-runs")


class MappingRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_ir_id: uuid.UUID
    document_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=200)] | None = None


class MappingRunJobOut(BaseModel):
    mapping_run_id: uuid.UUID
    job_id: uuid.UUID


class EditRunOut(BaseModel):
    mapping_run_id: uuid.UUID
    version: int
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
    session: AsyncSession, ctx: TenantContext, requested: list[uuid.UUID] | None
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
    run, job = await start_mapping_run(session, ctx, body.template_ir_id, body.document_ids)
    return MappingRunJobOut(mapping_run_id=run.id, job_id=job.id)


async def start_mapping_run(
    session: AsyncSession,
    ctx: TenantContext,
    template_ir_id: uuid.UUID,
    document_ids: list[uuid.UUID] | None,
) -> tuple[MappingRun, Job]:
    """Queue a mapping run (shared with the Pipe); `None` = every indexed source document."""
    ir = await repo.get_template_ir_by_id(session, ctx, template_ir_id)
    ids = await _resolve_documents(session, ctx, document_ids)
    run = await repo.create_mapping_run(session, ctx, template_ir_id=ir.id, document_ids=ids)
    job = await repo.create_job(session, ctx, "run_mapping", {"mapping_run_id": str(run.id)})
    return run, job


def _results(run: MappingRun) -> dict[str, FieldMappingResult]:
    return {k: FieldMappingResult.model_validate(v) for k, v in run.results.items()}


async def start_edit_run(
    session: AsyncSession, ctx: TenantContext, run_id: uuid.UUID, ops: list[EditOp]
) -> tuple[MappingRun, Job]:
    """Apply `ops` on top of a completed run as a new version and queue its render (shared with
    the Pipe). The parent row is never updated."""
    parent = await repo.get_mapping_run(session, ctx, run_id)
    if parent.status != "completed":
        raise Conflict("Only a completed mapping run can be edited.")
    ir = TemplateIR.model_validate(
        (await repo.get_template_ir_by_id(session, ctx, parent.template_ir_id)).ir
    )
    edited = apply_edits(ir, _results(parent), ops)
    run = await repo.create_mapping_run(
        session,
        ctx,
        template_ir_id=parent.template_ir_id,
        document_ids=list(parent.document_ids),
        trigger="edit",
        parent_run_id=parent.id,
        status="rendering",
        results={k: r.model_dump(mode="json") for k, r in edited.items()},
        summary=summarize(ir.fields, edited).model_dump(mode="json"),
        edits=[op.model_dump(mode="json") for op in ops],
    )
    job = await repo.create_job(session, ctx, "render_artifact", {"mapping_run_id": str(run.id)})
    return run, job


@router.post("/{run_id}/edits", status_code=202)
async def create_edit_run(
    run_id: uuid.UUID, body: EditsIn, ctx: Tenant, session: Session
) -> EditRunOut:
    run, job = await start_edit_run(session, ctx, run_id, body.edits)
    return EditRunOut(mapping_run_id=run.id, version=run.version, job_id=job.id)


@router.get("/{run_id}")
async def get_mapping_run(
    run_id: uuid.UUID, ctx: Tenant, session: Session, request: Request
) -> MappingRunOut:
    out = MappingRunOut.model_validate(await repo.get_mapping_run(session, ctx, run_id))
    artifact = await repo.get_artifact_by_run(session, ctx, run_id)
    if artifact is not None:
        out.artifact = artifact_out(artifact, request.app.state.settings)
    return out


class ConflictCandidate(BaseModel):
    index: int
    value: Value
    source_filename: str
    page_number: int | None
    verbatim_quote: str


class ConflictOut(BaseModel):
    field_key: str
    field_label: str
    slide_number: int | None
    section_title: str | None
    candidates: list[ConflictCandidate]


class ConflictList(BaseModel):
    items: list[ConflictOut]


_SLIDE = re.compile(r"^pptx:s(\d+):")
_TABLE_STEP = re.compile(r"Table \d+")


def _section_title(f: IRField, slide: int | None) -> str | None:
    if f.context:
        return f.context
    path = f.section_path
    if slide is not None:  # ["Slide N", title?, shape name]
        return path[1] if len(path) >= 3 else None
    path = path[:-1] if path and _TABLE_STEP.fullmatch(path[-1]) else path
    return path[-1] if path else None


def conflict_items(ir: TemplateIR, results: dict[str, FieldMappingResult]) -> list[ConflictOut]:
    """Conflicts in IR order; fields sharing one fact (one edit) appear once, first in IR order."""
    shared, seen, items = shared_facts(ir.fields), set(), []
    for f in ir.fields:
        r = results.get(f.field_id)
        key = shared.get(f.field_id, f.field_id)
        if r is None or r.status != "conflict" or not r.conflict_details or key in seen:
            continue
        seen.add(key)
        m = _SLIDE.match(f.anchor_id)
        slide = int(m[1]) if m else None
        items.append(
            ConflictOut(
                field_key=f.field_id,
                field_label=f.label,
                slide_number=slide,
                section_title=_section_title(f, slide),
                candidates=[
                    ConflictCandidate(
                        index=i,
                        value=c.value,
                        source_filename=c.evidence[0].filename,
                        page_number=c.evidence[0].page_number,
                        verbatim_quote=c.evidence[0].quote,
                    )
                    for i, c in enumerate(r.conflict_details.candidates)
                ],
            )
        )
    return items


@router.get("/{run_id}/conflicts")
async def list_conflicts(run_id: uuid.UUID, ctx: Tenant, session: Session) -> ConflictList:
    run = await repo.get_mapping_run(session, ctx, run_id)
    ir = TemplateIR.model_validate(
        (await repo.get_template_ir_by_id(session, ctx, run.template_ir_id)).ir
    )
    return ConflictList(items=conflict_items(ir, _results(run)))


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_key: str = Field(max_length=64)
    candidate_index: int | None = Field(None, ge=0)
    custom_value: str | None = Field(None, min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def _exactly_one(self) -> "Resolution":
        if (self.candidate_index is None) == (self.custom_value is None):
            raise ValueError("give exactly one of candidate_index and custom_value")
        return self

    def op(self) -> EditOp:
        if self.custom_value is not None:
            return SetValue(op="set_value", field_id=self.field_key, value=self.custom_value)
        return ChooseCandidate(
            op="choose_candidate", field_id=self.field_key, candidate_index=self.candidate_index
        )


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolutions: list[Resolution] = Field(min_length=1, max_length=50)


class ResolveOut(BaseModel):
    mapping_run_id: uuid.UUID
    version: int
    job_id: uuid.UUID
    status: str
    artifact: ArtifactOut | None
    remaining_conflicts: list[ConflictOut]


# shortcut: the request holds the connection until the render finishes or this cap passes (then
# `artifact` is null and the client polls GET .../mapping-runs/{id}); move to polling or SSE if
# renders routinely take longer than this.
RESOLVE_WAIT_S = 60.0
RESOLVE_POLL_S = 0.25


@router.post("/{run_id}/resolve-conflicts")
async def resolve_conflicts(
    run_id: uuid.UUID, body: ResolveIn, ctx: Tenant, request: Request
) -> ResolveOut:
    """Edit ops (`choose_candidate` / `set_value`) as a new version, rendered before returning."""
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker.begin() as session:  # committed before the wait: the worker sees it
        run, job = await start_edit_run(session, ctx, run_id, [r.op() for r in body.resolutions])
        ir_row = await repo.get_template_ir_by_id(session, ctx, run.template_ir_id)
        remaining = conflict_items(TemplateIR.model_validate(ir_row.ir), _results(run))
    deadline = asyncio.get_running_loop().time() + RESOLVE_WAIT_S
    while True:
        async with sessionmaker.begin() as session:
            job = await repo.get_job(session, ctx, job.id)
            artifact = await repo.get_artifact_by_run(session, ctx, run.id)
        if job.status in ("succeeded", "failed") or asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(RESOLVE_POLL_S)
    ok = job.status == "succeeded" and artifact is not None
    return ResolveOut(
        mapping_run_id=run.id,
        version=run.version,
        job_id=job.id,
        status=job.status,
        artifact=artifact_out(artifact, request.app.state.settings) if ok else None,
        remaining_conflicts=remaining,
    )
