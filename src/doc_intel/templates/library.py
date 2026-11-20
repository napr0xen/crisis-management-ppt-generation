"""Template library: operator registration and per-project adoption (copy on use).

`library_templates` is global and read-only for users (data-model.md tenancy exception). `adopt`
copies one into the caller's project so every later step runs on tenant-scoped rows.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_intel.core.errors import Conflict, UnsupportedMediaType, ValidationFailed
from doc_intel.core.tenancy import TenantContext
from doc_intel.db import repo
from doc_intel.db.models import LibraryTemplate
from doc_intel.ingestion.upload import MIME_TYPES, library_key, upload_key, validate_upload
from doc_intel.llm.gateway import LLMClient
from doc_intel.storage.base import StorageBackend
from doc_intel.templates.anchors import build_anchor_map
from doc_intel.templates.ir_builder import build_ir

FORMATS = ("pptx", "docx", "xlsx")
OnBatch = Callable[[int, int, str | None], Awaitable[None]]


def _text(value: str | None, limit: int, label: str, required: bool = False) -> str | None:
    value = (value or "").strip()
    if len(value) > limit or (required and not value):
        raise ValidationFailed(f"The {label} must be {1 if required else 0} to {limit} characters.")
    return value or None


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
    """Analyze `path` once (anchors + IR) and store it in the library."""
    name = _text(name, 100, "name", required=True)
    description = _text(description, 500, "description") or ""
    category = _text(category, 50, "category")
    data = await asyncio.to_thread(path.read_bytes)
    upload = validate_upload(path.name, data, max_bytes)
    if upload.ext not in FORMATS:
        raise UnsupportedMediaType()
    async with sessionmaker() as session:
        if await repo.find_library_template(session, name=name, sha256=upload.sha256):
            raise Conflict("A library template with this name or file already exists.")
    template_id = uuid.uuid4()
    amap = await asyncio.to_thread(build_anchor_map, upload.ext, data)
    ir = await build_ir(llm, amap, template_id, on_batch=on_batch)
    key = library_key(template_id, upload.ext)
    await asyncio.to_thread(storage.put_bytes, key, data)
    try:
        async with sessionmaker.begin() as session:
            return await repo.create_library_template(
                session,
                id=template_id,
                name=name,
                description=description,
                category=category,
                format=upload.ext,
                filename=path.name,
                sha256=upload.sha256,
                size_bytes=upload.size_bytes,
                storage_key=key,
                anchor_map=amap.model_dump(mode="json"),
                anchor_count=len(amap.anchors),
                ir=ir.model_dump(mode="json", exclude_none=True),
            )
    except BaseException:
        await asyncio.to_thread(storage.delete, key)
        raise


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
