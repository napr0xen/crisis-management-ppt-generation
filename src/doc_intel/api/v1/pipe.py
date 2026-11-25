"""Open WebUI Pipe endpoints (api-specification.md § Open WebUI Pipe, system-design § 9)."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any, Literal

import httpx
import structlog
from fastapi import APIRouter, Depends, Form, Header, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from doc_intel.api.v1.artifacts import artifact_out
from doc_intel.api.v1.documents import store_upload
from doc_intel.api.v1.mapping_runs import start_edit_run, start_mapping_run
from doc_intel.api.v1.templates import start_template
from doc_intel.chat.edits import parse_edits
from doc_intel.chat.library import library_choice
from doc_intel.chat.qa import answer
from doc_intel.chat.router import TEMPLATE_FORMATS, ChatMessage, doc_format, route
from doc_intel.chat.summary import (
    HELP_TEXT,
    md,
    render_answer,
    render_candidates,
    render_edits,
    render_library,
    render_summary,
)
from doc_intel.core.errors import AppError, UpstreamUnavailable, ValidationFailed
from doc_intel.core.security import USER_ID_RE, UserId
from doc_intel.core.tenancy import TenantContext
from doc_intel.db import repo
from doc_intel.db.models import Job, LibraryTemplate
from doc_intel.db.session import Session
from doc_intel.mapping.schemas import FieldMappingResult
from doc_intel.templates.library import adopt
from doc_intel.templates.schemas import TemplateIR

log = structlog.get_logger()
router = APIRouter(prefix="/pipe")

POLL_INTERVAL_S = 1.0
QUESTION_CHARS = 2000

SseEvent = Literal["status", "delta", "error", "done"]


class PipeFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    openwebui_file_id: str = Field(min_length=1, max_length=128)
    filename: str = Field(max_length=255)


class PipeChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: Annotated[list[ChatMessage], Field(min_length=1, max_length=50)]
    files: Annotated[list[PipeFile], Field(max_length=200)] = []
    # a UI that already knows the template skips the router LLM (at most one of the two)
    library_template_id: uuid.UUID | None = None
    template_document_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one_template(self) -> "PipeChatIn":
        if self.library_template_id and self.template_document_id:
            raise ValueError("give at most one of library_template_id and template_document_id")
        return self


class PipeFileOut(BaseModel):
    project_id: uuid.UUID
    document_id: uuid.UUID
    job_id: uuid.UUID | None
    deduplicated: bool


def sse(event: SseEvent, data: dict[str, Any]) -> str:
    """One SSE frame: `event: <name>` + `data: <json>` + blank line."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def require_chat_project(
    user_id: UserId, session: Session, x_owui_chat_id: Annotated[str | None, Header()] = None
) -> TenantContext:
    """The caller's project for this Open WebUI chat, created on first use (ADR-009)."""
    if not x_owui_chat_id or not USER_ID_RE.fullmatch(x_owui_chat_id):
        raise ValidationFailed("Invalid X-OWUI-Chat-Id.")
    project = await repo.get_or_create_chat_project(session, user_id, x_owui_chat_id)
    return TenantContext(user_id, project.id)


ChatTenant = Annotated[TenantContext, Depends(require_chat_project)]


@router.post("/files", status_code=201)
async def pipe_files(
    file: UploadFile,
    openwebui_file_id: Annotated[str, Form(min_length=1, max_length=128)],
    ctx: ChatTenant,
    session: Session,
    request: Request,
) -> PipeFileOut:
    # Open WebUI re-sends every file each turn; a known id must not re-queue a failed ingest.
    known = await repo.find_documents_by_openwebui_file_ids(session, ctx, [openwebui_file_id])
    if known:
        return PipeFileOut(
            project_id=ctx.project_id, document_id=known[0].id, job_id=None, deduplicated=True
        )
    out = await store_upload(session, ctx, request, file)
    await repo.add_openwebui_file_id(session, ctx, out.document_id, openwebui_file_id)
    return PipeFileOut(project_id=ctx.project_id, **out.model_dump())


@router.post("/chat")
async def pipe_chat(body: PipeChatIn, ctx: ChatTenant, request: Request) -> StreamingResponse:
    # The request-scoped session is already committed here; the stream opens its own short ones.
    return StreamingResponse(
        _stream(request, ctx, body),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class _Disconnected(Exception):
    pass


def _job_failed(job: Job | None) -> AppError:
    err = AppError("Processing failed.")
    err.code = (job.error_code if job else None) or "job_failed"
    return err


async def _watch(
    request: Request,
    load: Callable[[AsyncSession], Awaitable[Job | None]],
    describe: Callable[[Job], str],
    box: list[Job | None],
) -> AsyncIterator[str]:
    """Poll a job, yield a `status` frame whenever its description changes, and leave the
    finished (or missing) job in `box`. Holds no DB session across the sleep."""
    # shortcut: no server-side wait cap; a stuck or disabled worker (JOB_CONCURRENCY=0) holds the
    # stream until the client disconnects or TIMEOUT_S, add a cap if that happens.
    last = None
    while True:
        async with request.app.state.sessionmaker.begin() as session:
            job = await load(session)
        if job is None or job.status in ("succeeded", "failed"):
            box[:] = [job]
            return
        text = describe(job)
        if text != last:
            last = text
            yield sse("status", {"description": text, "done": False})
        else:  # keeps proxies and the Pipe's read timeout alive; a dead client fails this send
            yield ": keep-alive\n\n"
        # shortcut: disconnect is only noticed between polls; route/answer/start_template run to
        # completion, run the flow as a cancellable task if Ollama time is wasted.
        if await request.is_disconnected():
            raise _Disconnected
        await asyncio.sleep(POLL_INTERVAL_S)


def _ingest_text(name: str) -> Callable[[Job], str]:
    def describe(job: Job) -> str:
        p = job.progress
        if p.get("stage") == "embedding" and "done" in p and "total" in p:
            return f"Embedding {name} ({p['done']}/{p['total']})"
        return f"Parsing {name}"

    return describe


def _extract_text(job: Job) -> str:
    p = job.progress
    if p.get("stage") == "ir":
        if not p.get("total"):
            return "Analyzing template fields"
        part = f": {p['part']}" if p.get("part") else ""
        return f"Analyzing template fields{part} ({p['done'] + 1}/{p['total']})"
    return "Reading template structure"


def _mapping_text(job: Job) -> str:
    p = job.progress
    if not p.get("total"):
        return "Mapping fields"
    done = p.get("done")
    if done is None:
        return "Mapping fields"
    if done == p["total"]:  # all fields mapped: the run is verifying and rendering
        return "Rendering document"
    if p.get("field"):
        return f"Mapping field {done + 1}/{p['total']}: {p['field']}"
    return f"Mapping fields ({done}/{p['total']})"


async def _stream(request: Request, ctx: TenantContext, body: PipeChatIn) -> AsyncIterator[str]:
    out: dict[str, str | None] = {
        "project_id": str(ctx.project_id),
        "mapping_run_id": None,
        "artifact_id": None,
    }
    deltas: list[str] = []  # buffered so every `status` precedes every `delta`
    error: AppError | None = None
    try:
        async for frame in _flow(request, ctx, body, deltas, out):
            yield frame
    except _Disconnected:
        return
    except AppError as exc:
        error = exc
    except Exception as exc:
        log.error("pipe_chat_failed", error_type=type(exc).__name__)
        error = UpstreamUnavailable() if isinstance(exc, httpx.HTTPError) else AppError()
    for content in deltas:
        yield sse("delta", {"content": content})
    if error:
        yield sse("error", {"code": error.code, "message": error.message})
    yield sse("done", out)


async def _flow(
    request: Request,
    ctx: TenantContext,
    body: PipeChatIn,
    deltas: list[str],
    out: dict[str, str | None],
) -> AsyncIterator[str]:
    app = request.app
    sessionmaker = app.state.sessionmaker
    names = {f.openwebui_file_id: f.filename for f in body.files}
    async with sessionmaker.begin() as session:
        referenced = await repo.find_documents_by_openwebui_file_ids(session, ctx, list(names))
    known = {fid for d in referenced for fid in d.openwebui_file_ids}
    for fid, name in names.items():
        if fid not in known:
            deltas.append(f"I could not find the uploaded file {md(name)}, so I skipped it.")

    # Wait on every pending document of the project, not only the referenced ones: the router
    # lists them all and a template pick purges Qdrant points.
    # shortcut: only the 200 newest documents are listed (waited on and routed over), page through
    # list_documents if a chat ever holds more.
    async with sessionmaker.begin() as session:
        pending = [
            d
            for d in await repo.list_documents(session, ctx, 200)
            if d.status in ("uploaded", "parsing")
        ]
    for doc in pending:
        box: list[Job | None] = []

        def load(session: AsyncSession, document_id: uuid.UUID = doc.id) -> Awaitable:
            return repo.latest_job_for_document(session, ctx, "ingest_document", document_id)

        async for frame in _watch(request, load, _ingest_text(doc.filename), box):
            yield frame
        if box[0] is not None and box[0].status == "failed":
            deltas.append(f"I could not read {md(doc.filename)}, so I skipped it.")
    for doc in referenced:
        if doc.status == "failed":
            deltas.append(f"I could not read {md(doc.filename)}, so I skipped it.")

    if body.library_template_id or body.template_document_id:
        if body.library_template_id:
            async with sessionmaker.begin() as session:
                lib = await repo.get_library_template(session, body.library_template_id)
            flow = _map_library(request, ctx, lib, deltas, out)
        else:
            flow = _map_template(request, ctx, body.template_document_id, deltas, out)
        async for frame in flow:
            yield frame
        return
    async with sessionmaker.begin() as session:
        documents = await repo.list_documents(session, ctx, 200)
        library = await repo.list_library_templates(session)
    # A library copy is reached only through the library, never as an uploaded template.
    lib_shas = {t.sha256 for t in library}
    visible = [d for d in documents if not (d.role == "template" and d.sha256 in lib_shas)]
    intent = await route(app.state.llm, body.messages, visible)

    if intent.intent == "qa":
        user_messages = [m for m in body.messages if m.role == "user"]
        # the same cap as POST /query: a long paste must not push the prompt past LLM_NUM_CTX
        question = (user_messages or body.messages)[-1].content[-QUESTION_CHARS:]
        result = await answer(ctx, question, None, app.state.retriever, app.state.llm)
        deltas.append(render_answer(result))
    elif intent.intent == "chitchat":
        deltas.append(HELP_TEXT)
    elif intent.intent == "edit":
        async for frame in _edit(request, ctx, body, deltas, out):
            yield frame
    # A number reply to our library list wins; a library name yields to a confirmed upload.
    elif chosen := library_choice(
        library, body.messages, by_name=intent.template_document_id is None
    ):
        async for frame in _map_library(request, ctx, chosen, deltas, out):
            yield frame
    elif intent.template_document_id is not None:
        async for frame in _map_template(request, ctx, intent.template_document_id, deltas, out):
            yield frame
    elif (
        any(
            doc_format(d) in TEMPLATE_FORMATS and intent.format in (None, doc_format(d))
            for d in visible
        )
        or not library
    ):
        deltas.append(render_candidates(visible))
    else:
        matches = [t for t in library if intent.format in (None, t.format)]
        if intent.format and len(matches) == 1:
            async for frame in _map_library(request, ctx, matches[0], deltas, out):
                yield frame
        else:
            deltas.append(render_library(matches or library))


async def _map_template(
    request: Request,
    ctx: TenantContext,
    template_document_id: uuid.UUID,
    deltas: list[str],
    out: dict[str, str | None],
) -> AsyncIterator[str]:
    # shortcut: concurrent turns in one chat (regenerate, double send) can start duplicate
    # extraction/mapping runs, add a per-project lock if that happens.
    app = request.app
    sessionmaker = app.state.sessionmaker
    async with sessionmaker.begin() as session:
        extract = await start_template(session, ctx, app.state.vector_store, template_document_id)
    box: list[Job | None] = []
    async for frame in _watch(
        request, lambda s: repo.get_job(s, ctx, extract.id), _extract_text, box
    ):
        yield frame
    if box[0] is None or box[0].status == "failed":
        raise _job_failed(box[0])
    async for frame in _run_mapping(
        request, ctx, uuid.UUID(box[0].result["template_ir_id"]), deltas, out
    ):
        yield frame


async def _map_library(
    request: Request,
    ctx: TenantContext,
    lib: LibraryTemplate,
    deltas: list[str],
    out: dict[str, str | None],
) -> AsyncIterator[str]:
    """Copy the library template into the project (no LLM call), then map it."""
    app = request.app
    deltas.append(f"Using the library template **{md(lib.name)}**.")
    async with app.state.sessionmaker.begin() as session:
        ir_id = await adopt(session, ctx, app.state.storage, lib.id)
    async for frame in _run_mapping(request, ctx, ir_id, deltas, out):
        yield frame


async def _run_mapping(
    request: Request,
    ctx: TenantContext,
    ir_id: uuid.UUID,
    deltas: list[str],
    out: dict[str, str | None],
) -> AsyncIterator[str]:
    app = request.app
    sessionmaker = app.state.sessionmaker
    box: list[Job | None] = []
    async with sessionmaker.begin() as session:
        run, job = await start_mapping_run(session, ctx, ir_id, None)
    out["mapping_run_id"] = str(run.id)
    async for frame in _watch(request, lambda s: repo.get_job(s, ctx, job.id), _mapping_text, box):
        yield frame
    if box[0] is None or box[0].status == "failed":
        raise _job_failed(box[0])
    await _finish(request, ctx, run.id, ir_id, deltas, out)


async def _edit(
    request: Request,
    ctx: TenantContext,
    body: PipeChatIn,
    deltas: list[str],
    out: dict[str, str | None],
) -> AsyncIterator[str]:
    """Apply the user's edit to the latest completed run as a new version (Phase 11)."""
    app = request.app
    sessionmaker = app.state.sessionmaker
    # shortcut: an edit always targets the latest completed run in the chat; add an explicit
    # version reference ("edit v1") if users ask for it.
    async with sessionmaker.begin() as session:
        parent = await repo.latest_completed_mapping_run(session, ctx)
        ir_row = parent and await repo.get_template_ir_by_id(session, ctx, parent.template_ir_id)
    if parent is None:
        deltas.append(
            "There is no generated document to edit yet. Ask me to fill a template first."
        )
        return
    ir = TemplateIR.model_validate(ir_row.ir)
    results = {k: FieldMappingResult.model_validate(v) for k, v in parent.results.items()}
    try:
        ops = await parse_edits(app.state.llm, body.messages, ir, results)
        if not ops:
            deltas.append("Tell me which field to change and its new value.")
            return
        async with sessionmaker.begin() as session:
            run, job = await start_edit_run(session, ctx, parent.id, ops)
    except ValidationFailed as exc:
        deltas.append(f"I could not apply that edit: {md(exc.message)}")
        return
    out["mapping_run_id"] = str(run.id)
    box: list[Job | None] = []
    async for frame in _watch(
        request, lambda s: repo.get_job(s, ctx, job.id), lambda _: "Rendering document", box
    ):
        yield frame
    if box[0] is None or box[0].status == "failed":
        raise _job_failed(box[0])
    # Every applied op is named, so an edit the user did not ask for (prompt injection through a
    # quoted value) is visible; the parent version stays downloadable.
    # shortcut: choose_candidate and clear have no deterministic check against the user's words
    # (set_value does, ADR-013), add a label match if injected edits show up.
    header = "\n".join(
        [f"Applied {len(ops)} edit(s). This is version {run.version}.", *render_edits(ops, ir)]
    )
    await _finish(request, ctx, run.id, parent.template_ir_id, deltas, out, header)


async def _finish(
    request: Request,
    ctx: TenantContext,
    run_id: uuid.UUID,
    ir_id: uuid.UUID,
    deltas: list[str],
    out: dict[str, str | None],
    header: str | None = None,
) -> None:
    app = request.app
    async with app.state.sessionmaker.begin() as session:
        run = await repo.get_mapping_run(session, ctx, run_id)
        ir_row = await repo.get_template_ir_by_id(session, ctx, ir_id)
        artifact = await repo.get_artifact_by_run(session, ctx, run_id)
    if artifact is None:  # the run itself failed; run_mapping still succeeded as a job
        raise _job_failed(None)
    out["artifact_id"] = str(artifact.id)
    results = {k: FieldMappingResult.model_validate(v) for k, v in run.results.items()}
    ir = TemplateIR.model_validate(ir_row.ir)
    url = artifact_out(artifact, app.state.settings).download_url
    summary = render_summary(results, run.summary, ir, url)
    deltas.append(f"{header}\n\n{summary}" if header else summary)
