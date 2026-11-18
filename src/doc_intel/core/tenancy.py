"""`TenantContext`, built once per request after the project ownership check (ADR-009)."""

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends

from doc_intel.core.security import UserId
from doc_intel.db import repo
from doc_intel.db.session import Session


@dataclass(frozen=True)
class TenantContext:
    user_id: str
    project_id: uuid.UUID


async def require_project(
    project_id: uuid.UUID, user_id: UserId, session: Session
) -> TenantContext:
    """Another user's project is `NotFound`, identical to an unknown id."""
    await repo.ensure_project(session, user_id, project_id)
    return TenantContext(user_id, project_id)


Tenant = Annotated[TenantContext, Depends(require_project)]
