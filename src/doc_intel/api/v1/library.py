"""Template library over HTTP (ADR-014): the library is global, so there is no project in the path.

The bearer-secret holder is the operator of this single-operator deployment. Users still get
per-project copies through `templates.library.adopt` (ADR-012).
"""

import asyncio
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, Form, Request, UploadFile
from pydantic import BaseModel

from doc_intel.core.errors import AppError, Conflict, NotFound, ValidationFailed
from doc_intel.core.security import require_user
from doc_intel.db import repo
from doc_intel.db.models import LibraryTemplate
from doc_intel.db.session import Session
from doc_intel.templates import library

log = structlog.get_logger()
router = APIRouter(prefix="/library", dependencies=[Depends(require_user)])
sync_router = APIRouter(dependencies=[Depends(require_user)])  # POST /v1/templates/sync


class ScanProgress(BaseModel):
    done: int
    total: int
    part: str | None


class Scan(BaseModel):
    scan_id: uuid.UUID
    name: str
    status: Literal["scanning", "ready", "failed"]
    progress: ScanProgress | None = None
    template_id: uuid.UUID | None = None
    error_code: str | None = None


class LibraryItem(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    category: str | None
    format: str
    filename: str
    size_bytes: int
    created_at: datetime
    slide_count: int | None
    field_count: int
    table_count: int
    chart_count: int
    anchor_count: int
    scan_status: Literal["ready"] = "ready"


class LibraryList(BaseModel):
    items: list[LibraryItem]
    scans: list[Scan]


class ScanStarted(BaseModel):
    scan_id: uuid.UUID


_SLIDE = re.compile(r"^pptx:s(\d+):")
_CELL = re.compile(r":r\d+c\d+$")  # pptx:s5:sp4:r1c2 -> table pptx:s5:sp4


def _item(t: LibraryTemplate) -> LibraryItem:
    fields = t.ir["fields"]
    slides = [int(m[1]) for a in t.anchor_map["anchors"] if (m := _SLIDE.match(a["anchor_id"]))]
    tables = {_CELL.sub("", f["anchor_id"]) for f in fields if _CELL.search(f["anchor_id"])}
    return LibraryItem(
        id=t.id,
        name=t.name,
        description=t.description,
        category=t.category,
        format=t.format,
        filename=t.filename,
        size_bytes=t.size_bytes,
        created_at=t.created_at,
        # shortcut: trailing slides without anchors are not counted, store the slide count at
        # registration if the number must match the deck exactly.
        slide_count=max(slides, default=0) if t.format == "pptx" else None,
        field_count=len(fields),
        table_count=len(tables),
        chart_count=sum(f["expected_type"] == "chart" for f in fields),
        anchor_count=t.anchor_count,
    )


# shortcut: scan state is in memory, so it is lost on restart (a scan in flight is then simply
# gone and the file must be uploaded again); persist it when scans must survive a restart.
def _scans(request: Request) -> dict[uuid.UUID, Scan]:
    return request.app.state.scans


@contextmanager
def _tracking(scan: Scan) -> Iterator[library.OnBatch]:
    """Yield the progress callback; mark the scan ready, or failed with only an error code."""

    async def on_batch(done: int, total: int, part: str | None) -> None:
        scan.progress = ScanProgress(done=done, total=total, part=part)

    try:
        yield on_batch
        scan.status = "ready"
    except Exception as exc:  # only the code is kept: exception text may carry document text
        scan.status = "failed"
        scan.error_code = exc.code if isinstance(exc, AppError) else "internal"
        log.warning("library_scan_failed", error_type=type(exc).__name__, code=scan.error_code)
        raise
    finally:
        scan.progress = None


async def _run_scan(app, scan: Scan, prepared: library.Prepared) -> None:
    # shortcut: a sync started between the file write and this lock may extract the same file
    # first; the scan then fails with `conflict` and the template is there anyway.
    async with app.state.template_lock:
        try:
            with _tracking(scan) as on_batch:
                lib = await library.store(
                    app.state.sessionmaker, app.state.storage, app.state.llm, prepared, on_batch
                )
                scan.template_id = lib.id
        except Exception:  # `_tracking` already recorded it on the scan
            return


def _track(app, name: str):
    scan = Scan(scan_id=uuid.uuid4(), name=name, status="scanning")
    app.state.scans[scan.scan_id] = scan
    return _tracking(scan)


def _spawn(app, coro) -> None:
    task = asyncio.create_task(coro)
    app.state.scan_tasks.add(task)  # a strong reference: the loop keeps only weak ones
    task.add_done_callback(app.state.scan_tasks.discard)


def start_sync(app) -> None:
    """Sync `TEMPLATES_DIR` into the library in the background (extraction takes minutes)."""

    async def run() -> None:
        try:
            await library.sync_templates_dir(
                app.state.sessionmaker,
                app.state.storage,
                app.state.llm,
                app.state.settings.templates_dir,
                max_bytes=app.state.settings.max_upload_mb * 1024 * 1024,
                lock=app.state.template_lock,
                on_scan=lambda name: _track(app, name),
            )
        except Exception as exc:
            log.error("template_sync_crashed", error_type=type(exc).__name__)

    _spawn(app, run())


@sync_router.post("/templates/sync", status_code=202)
async def sync_templates(request: Request) -> dict[str, str]:
    if request.app.state.template_lock.locked():
        raise Conflict("A template sync or scan is already running.")
    start_sync(request.app)
    return {"status": "started"}


def _save(folder: Path, path: Path, data: bytes) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as f:
            f.write(data)
    except FileExistsError:
        raise Conflict("A file with this name already exists in the templates folder.") from None


@router.get("/templates")
async def list_templates(session: Session, request: Request) -> LibraryList:
    rows = await repo.list_library_templates(session)
    scans = [s for s in _scans(request).values() if s.status != "ready"]
    return LibraryList(items=[_item(t) for t in rows], scans=scans)


@router.post("/templates", status_code=202)
async def create_template(
    file: UploadFile,
    name: Annotated[str, Form()],
    request: Request,
    description: Annotated[str, Form()] = "",
    category: Annotated[str | None, Form()] = None,
) -> ScanStarted:
    app = request.app
    max_bytes = app.state.settings.max_upload_mb * 1024 * 1024
    data = await file.read(max_bytes + 1)
    prepared = await library.prepare(
        app.state.sessionmaker,
        Path(file.filename).name if file.filename else None,  # the row and the file share it
        data,
        name=name,
        description=description,
        category=category,
        max_bytes=max_bytes,
    )
    # the file is the source of truth (ADR-017): keep it so a later sync keeps the row
    folder = app.state.settings.templates_dir
    path = library.file_path(folder, prepared.filename)
    if path is None:
        raise ValidationFailed("The file name is not usable.")
    await asyncio.to_thread(_save, folder, path, data)
    scan = Scan(scan_id=uuid.uuid4(), name=prepared.name, status="scanning")
    _scans(request)[scan.scan_id] = scan
    _spawn(app, _run_scan(app, scan, prepared))
    return ScanStarted(scan_id=scan.scan_id)


@router.get("/scans/{scan_id}")
async def get_scan(scan_id: uuid.UUID, request: Request) -> Scan:
    scan = _scans(request).get(scan_id)
    if scan is None:
        raise NotFound()
    return scan


@router.delete("/templates/{template_id}", status_code=204)
async def delete_template(template_id: uuid.UUID, request: Request) -> None:
    """Row, blob, then the file in `TEMPLATES_DIR` (else the next sync would re-add it); copies
    already adopted into projects are untouched."""
    async with request.app.state.sessionmaker.begin() as session:
        lib = await repo.delete_library_template(session, template_id)
    await asyncio.to_thread(request.app.state.storage.delete, lib.storage_key)
    path = library.file_path(request.app.state.settings.templates_dir, lib.filename)
    if path:
        await asyncio.to_thread(path.unlink, missing_ok=True)
