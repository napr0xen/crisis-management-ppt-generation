import asyncio
import os
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, BinaryIO

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from doc_intel.core.config import Settings
from doc_intel.core.errors import NotFound
from doc_intel.core.security import sign_artifact, verify_artifact_signature
from doc_intel.core.tenancy import Tenant
from doc_intel.db import repo
from doc_intel.db.models import Artifact
from doc_intel.db.session import Session
from doc_intel.ingestion.upload import MIME_TYPES

log = structlog.get_logger()
router = APIRouter()

_CHUNK = 64 * 1024


class ArtifactOut(BaseModel):
    id: uuid.UUID
    mapping_run_id: uuid.UUID
    format: str
    filename: str
    size_bytes: int
    download_url: str
    created_at: datetime


class ArtifactList(BaseModel):
    items: list[ArtifactOut]


def artifact_out(artifact: Artifact, settings: Settings) -> ArtifactOut:
    exp, sig = sign_artifact(artifact.id, artifact.user_id)
    base = settings.public_base_url.rstrip("/")
    url = f"{base}/v1/artifacts/{artifact.id}/download?exp={exp}&sig={sig}"
    return ArtifactOut(
        id=artifact.id,
        mapping_run_id=artifact.mapping_run_id,
        format=artifact.format,
        filename=artifact.filename,
        size_bytes=artifact.size_bytes,
        download_url=url,
        created_at=artifact.created_at,
    )


def _chunks(f: BinaryIO) -> Iterator[bytes]:
    with f:
        while chunk := f.read(_CHUNK):
            yield chunk


def _header_filename(name: str) -> str:
    return "".join("_" if c in '"\\\r\n' or not c.isprintable() else c for c in name)


@router.get("/projects/{project_id}/artifacts")
async def list_artifacts(
    ctx: Tenant,
    session: Session,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ArtifactList:
    settings = request.app.state.settings
    rows = await repo.list_artifacts(session, ctx, limit)
    return ArtifactList(items=[artifact_out(a, settings) for a in rows])


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    artifact_id: uuid.UUID,
    session: Session,
    request: Request,
    exp: int = 0,
    sig: Annotated[str, Query(max_length=64)] = "",
) -> StreamingResponse:
    # The signature is the credential: the only unscoped lookup, verified before any byte is read.
    artifact = await repo.get_artifact_for_download(session, artifact_id)
    verify_artifact_signature(artifact.id, artifact.user_id, exp, sig)
    # The key comes from the DB row only; the session commits before streaming starts.
    try:
        f = await asyncio.to_thread(request.app.state.storage.open_read, artifact.storage_key)
    except (FileNotFoundError, ValueError):
        log.warning("artifact_file_unavailable", artifact_id=str(artifact.id))
        raise NotFound() from None
    try:
        headers = {
            "Content-Disposition": f'attachment; filename="{_header_filename(artifact.filename)}"',
            "Content-Length": str(os.fstat(f.fileno()).st_size),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        }
        return StreamingResponse(
            _chunks(f), media_type=MIME_TYPES[artifact.format], headers=headers
        )
    except BaseException:
        f.close()
        raise
