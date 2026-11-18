import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from doc_intel.core.security import require_user
from doc_intel.db import repo
from doc_intel.db.models import Project
from doc_intel.db.session import get_session

router = APIRouter(prefix="/projects")

UserId = Annotated[str, Depends(require_user)]
# scope='function': the request's transaction commits before the response is sent.
Session = Annotated[AsyncSession, Depends(get_session, scope="function")]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)


class ProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    openwebui_chat_id: str | None
    created_at: datetime
    document_count: int


class ProjectList(BaseModel):
    items: list[ProjectOut]


def _out(project: Project, document_count: int) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        openwebui_chat_id=project.openwebui_chat_id,
        created_at=project.created_at,
        document_count=document_count,
    )


@router.post("", status_code=201)
async def create_project(body: ProjectCreate, user_id: UserId, session: Session) -> ProjectOut:
    return _out(await repo.create_project(session, user_id, body.name), 0)


@router.get("")
async def list_projects(
    user_id: UserId, session: Session, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> ProjectList:
    return ProjectList(
        items=[_out(p, n) for p, n in await repo.list_projects(session, user_id, limit)]
    )


@router.get("/{project_id}")
async def get_project(project_id: uuid.UUID, user_id: UserId, session: Session) -> ProjectOut:
    return _out(*await repo.get_project(session, user_id, project_id))
