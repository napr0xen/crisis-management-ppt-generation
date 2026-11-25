"""Template library: registration (CLI and HTTP) and per-project adoption (copy on use).

`library_templates` is global; chat users only read it, the operator (CLI, `/v1/library`
ADR-014, or the `templates/` folder sync ADR-017) writes it (data-model.md tenancy exception).
`adopt` copies one into the caller's project so every later step runs on tenant-scoped rows.
"""

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_intel.core.errors import AppError, Conflict, UnsupportedMediaType, ValidationFailed
from doc_intel.core.tenancy import TenantContext
from doc_intel.db import repo
from doc_intel.db.models import LibraryTemplate
from doc_intel.ingestion.pipeline import ParseError
from doc_intel.ingestion.upload import (
    MIME_TYPES,
    ValidatedUpload,
    library_key,
    upload_key,
    validate_upload,
)
from doc_intel.llm.gateway import LLMClient
from doc_intel.storage.base import StorageBackend
from doc_intel.templates.anchors import build_anchor_map
from doc_intel.templates.ir_builder import IR_BUILDER_VERSION, build_ir
from doc_intel.templates.schemas import AnchorMap

log = structlog.get_logger()

FORMATS = ("pptx", "docx", "xlsx")
OnBatch = Callable[[int, int, str | None], Awaitable[None]]
# Called with the row name a synced file will get; the context manager yields the progress
# callback and records the outcome (the API layer wires it to its scan list).
OnScan = Callable[[str], AbstractContextManager[OnBatch | None]]


def _text(value: str | None, limit: int, label: str, required: bool = False) -> str | None:
    value = (value or "").strip()
    if len(value) > limit or (required and not value):
        raise ValidationFailed(f"The {label} must be {1 if required else 0} to {limit} characters.")
    return value or None


@dataclass(frozen=True)
class Prepared:
    """An upload that passed every check that needs no LLM (see `prepare`)."""

    name: str
    description: str
    category: str | None
    filename: str
    upload: ValidatedUpload
    template_id: uuid.UUID
    amap: AnchorMap
    replacing: uuid.UUID | None = None  # the row `store` swaps out in its transaction


async def prepare(
    sessionmaker: async_sessionmaker[AsyncSession],
    filename: str | None,
    data: bytes,
    *,
    name: str,
    description: str = "",
    category: str | None = None,
    max_bytes: int,
    replacing: uuid.UUID | None = None,
) -> Prepared:
    """Validate the fields and bytes, check name/hash uniqueness and build the AnchorMap."""
    name = _text(name, 100, "name", required=True)
    description = _text(description, 500, "description") or ""
    category = _text(category, 50, "category")
    upload = validate_upload(filename, data, max_bytes)
    if upload.ext not in FORMATS:
        raise UnsupportedMediaType()
    async with sessionmaker() as session:
        hit = await repo.find_library_template(session, name=name, sha256=upload.sha256)
        if hit and hit.id != replacing:  # the database unique keys still guard the swap
            raise Conflict("A library template with this name or file already exists.")
    try:
        amap = await asyncio.to_thread(build_anchor_map, upload.ext, data)
    except ParseError as exc:
        raise ValidationFailed(str(exc)) from exc
    return Prepared(
        name, description, category, filename or "", upload, uuid.uuid4(), amap, replacing
    )


async def store(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: StorageBackend,
    llm: LLMClient,
    p: Prepared,
    on_batch: OnBatch | None = None,
) -> LibraryTemplate:
    """Build the IR (the slow LLM step), then store the file and the row.

    With `p.replacing` the old row is deleted in the same transaction as the insert; the caller
    removes the old blob afterwards.
    """
    ir = await build_ir(llm, p.amap, p.template_id, on_batch=on_batch)
    key = library_key(p.template_id, p.upload.ext)
    await asyncio.to_thread(storage.put_bytes, key, p.upload.data)
    try:
        async with sessionmaker.begin() as session:
            if p.replacing:
                await repo.delete_library_template(session, p.replacing)
            return await repo.create_library_template(
                session,
                id=p.template_id,
                name=p.name,
                description=p.description,
                category=p.category,
                format=p.upload.ext,
                filename=p.filename,
                sha256=p.upload.sha256,
                size_bytes=p.upload.size_bytes,
                storage_key=key,
                anchor_map=p.amap.model_dump(mode="json"),
                anchor_count=len(p.amap.anchors),
                ir=ir.model_dump(mode="json", exclude_none=True),
                ir_builder_version=IR_BUILDER_VERSION,
            )
    except BaseException:
        await asyncio.to_thread(storage.delete, key)
        raise


async def register(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: StorageBackend,
    llm: LLMClient,
    path: Path,
    *,
    name: str,
    description: str = "",
    category: str | None = None,
    max_bytes: int,
    on_batch: OnBatch | None = None,
) -> LibraryTemplate:
    """Analyze `path` once (anchors + IR) and store it in the library (CLI)."""
    data = await asyncio.to_thread(path.read_bytes)
    p = await prepare(
        sessionmaker,
        path.name,
        data,
        name=name,
        description=description,
        category=category,
        max_bytes=max_bytes,
    )
    return await store(sessionmaker, storage, llm, p, on_batch)


async def adopt(
    session: AsyncSession,
    ctx: TenantContext,
    storage: StorageBackend,
    library_template_id: uuid.UUID,
) -> uuid.UUID:
    """Copy a library template into the caller's project; returns the `template_irs.id`.

    Idempotent per project (keyed by file hash). Never calls the LLM or touches Qdrant.
    """
    lib = await repo.get_library_template(session, library_template_id)
    existing = await repo.find_document_by_sha256(session, ctx, lib.sha256)
    if existing is not None:
        if existing.role != "template":
            raise Conflict(
                "This template file is already attached to the chat; "
                "ask me to fill it by its filename."
            )
        template = await repo.find_template_by_document(session, ctx, existing.id)
        latest = await repo.latest_template_ir(session, ctx, template.id) if template else None
        if latest is None:
            raise Conflict("The template is still being prepared in this chat.")
        return latest.id
    # shortcut: the file is copied before the rows commit, so a failed commit leaves an orphan
    # upload, and two concurrent picks in one chat can hit UNIQUE (project_id, sha256) (generic
    # error for the loser); add a cleanup sweep or a per-project lock if either shows up.
    document_id, template_id = uuid.uuid4(), uuid.uuid4()
    key = upload_key(ctx, document_id, lib.format)

    def copy() -> None:
        with storage.open_read(lib.storage_key) as f:
            storage.put_bytes(key, f.read())

    await asyncio.to_thread(copy)
    await repo.create_document(
        session,
        ctx,
        document_id,
        filename=lib.filename,
        mime_type=MIME_TYPES[lib.format],
        size_bytes=lib.size_bytes,
        sha256=lib.sha256,
        storage_key=key,
        role="template",
        status="indexed",
    )
    await repo.create_template(
        session,
        ctx,
        template_id=template_id,
        document_id=document_id,
        format=lib.format,
        anchor_map=lib.anchor_map,
        anchor_count=lib.anchor_count,
    )
    row = await repo.create_template_ir(
        session,
        ctx,
        template_id=template_id,
        version=1,
        ir={**lib.ir, "template_id": str(template_id)},
    )
    return row.id


# ---------------------------------------------------------------- templates/ folder sync (ADR-017)


@dataclass(frozen=True)
class SyncPlan:
    add: list[str] = field(default_factory=list)  # filenames with no row
    update: list[tuple[str, Any]] = field(default_factory=list)  # (filename, row id) to rebuild
    prune: list[Any] = field(default_factory=list)  # row ids with no file
    skipped: list[str] = field(default_factory=list)  # filenames whose bytes another row owns


def plan_sync(files: list[tuple[str, str]], rows: list[Any], version: int) -> SyncPlan:
    """Decide the sync from (filename, sha256) files sorted by name and the library rows.

    Rows match files by filename. `sha256` is UNIQUE, so a file whose bytes an earlier file or a
    kept row of another name already owns is skipped.
    """
    sha_of = dict(files)
    by_name: dict[str, Any] = {}
    prune = []
    for r in rows:
        if r.filename in sha_of and r.filename not in by_name:
            by_name[r.filename] = r
        else:
            prune.append(r.id)
    owner = {
        r.sha256: r.filename
        for r in by_name.values()
        if r.sha256 == sha_of[r.filename] and r.ir_builder_version == version
    }
    plan = SyncPlan(prune=prune)
    for name, sha in files:
        if owner.get(sha, name) != name:
            plan.skipped.append(name)
            continue
        owner[sha] = name
        row = by_name.get(name)
        if row is None:
            plan.add.append(name)
        elif row.sha256 != sha or row.ir_builder_version != version:
            plan.update.append((name, row.id))
    return plan


def file_path(templates_dir: Path, filename: str | None) -> Path | None:
    """`templates_dir / basename`, or None for an empty or dotfile name or a path that leaves it."""
    name = Path(filename or "").name
    path = templates_dir / name
    if not name or name.startswith(".") or path.resolve().parent != templates_dir.resolve():
        return None
    return path


def _read_files(templates_dir: Path) -> dict[str, tuple[str, bytes]]:
    found = {}
    for path in sorted(templates_dir.iterdir()):
        ext = path.suffix.lower().lstrip(".")
        if path.name.startswith((".", "~$")) or ext not in FORMATS or not path.is_file():
            continue
        data = path.read_bytes()
        found[path.name] = (hashlib.sha256(data).hexdigest(), data)
    return found


async def sync_templates_dir(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: StorageBackend,
    llm: LLMClient,
    templates_dir: Path,
    *,
    max_bytes: int,
    lock: asyncio.Lock,
    on_scan: OnScan | None = None,
) -> SyncPlan:
    """Make the library match the files in `templates_dir` (not recursive).

    A missing folder disables the sync: a wrong path must never prune the whole library. One
    failed file is logged and skipped; its old row stays. Runs under `lock` (shared with uploads).
    """
    if not await asyncio.to_thread(templates_dir.is_dir):
        log.warning("template_sync_skipped", reason="templates_dir_missing")
        return SyncPlan()
    failed = 0
    async with lock:
        found = await asyncio.to_thread(_read_files, templates_dir)
        async with sessionmaker() as session:
            rows = {r.id: r for r in await repo.list_library_templates(session)}
        plan = plan_sync(
            [(n, sha) for n, (sha, _) in found.items()], list(rows.values()), IR_BUILDER_VERSION
        )

        async def guarded(op: Awaitable[None]) -> None:
            nonlocal failed
            try:
                await op
            except Exception as exc:  # error type and code only: the text may carry document text
                failed += 1
                code = exc.code if isinstance(exc, AppError) else "internal"
                log.warning("template_sync_failed", error_type=type(exc).__name__, code=code)

        async def prune(row_id: Any) -> None:
            async with sessionmaker.begin() as session:
                old = await repo.delete_library_template(session, row_id)
            await asyncio.to_thread(storage.delete, old.storage_key)

        async def extract(filename: str, old: LibraryTemplate | None) -> None:
            name = old.name if old else Path(filename).stem[:100]
            with on_scan(name) if on_scan else nullcontext() as on_batch:
                p = await prepare(
                    sessionmaker,
                    filename,
                    found[filename][1],
                    name=name,
                    description=old.description if old else "",
                    category=old.category if old else None,
                    max_bytes=max_bytes,
                    replacing=old.id if old else None,
                )
                await store(sessionmaker, storage, llm, p, on_batch)
            if old:
                await asyncio.to_thread(storage.delete, old.storage_key)

        # prune first: it frees the names and hashes a renamed file is about to reuse
        for row_id in plan.prune:
            await guarded(prune(row_id))
        for filename in plan.add:
            await guarded(extract(filename, None))
        for filename, row_id in plan.update:
            await guarded(extract(filename, rows[row_id]))
    log.info(
        "template_sync",
        added=len(plan.add),
        updated=len(plan.update),
        pruned=len(plan.prune),
        skipped=len(plan.skipped),
        failed=failed,
    )
    return plan
