import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from doc_intel.core.tenancy import Tenant
from doc_intel.db import repo
from doc_intel.db.session import Session

router = APIRouter(prefix="/projects/{project_id}/jobs")


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_id: uuid.UUID
    kind: str
    status: str
    progress: dict[str, Any]
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@router.get("/{job_id}")
async def get_job(job_id: uuid.UUID, ctx: Tenant, session: Session) -> JobOut:
    return JobOut.model_validate(await repo.get_job(session, ctx, job_id))
