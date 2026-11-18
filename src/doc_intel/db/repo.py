"""Tenant-scoped queries: every function filters on `user_id` (+ `project_id` for children).

Functions flush but never commit; the caller's transaction (one per request) decides.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from doc_intel.core.errors import NotFound
from doc_intel.db.models import Document, Project


def _document_count():
    return (
        select(func.count())
        .where(Document.project_id == Project.id, Document.user_id == Project.user_id)
        .correlate(Project)
        .scalar_subquery()
    )


async def create_project(
    session: AsyncSession, user_id: str, name: str, openwebui_chat_id: str | None = None
) -> Project:
    project = Project(user_id=user_id, name=name, openwebui_chat_id=openwebui_chat_id)
    session.add(project)
    await session.flush()
    await session.refresh(project)
    return project


async def list_projects(
    session: AsyncSession, user_id: str, limit: int
) -> list[tuple[Project, int]]:
    rows = await session.execute(
        select(Project, _document_count())
        .where(Project.user_id == user_id)
        .order_by(Project.created_at.desc())
        .limit(limit)
    )
    return [(p, n) for p, n in rows.all()]


async def get_project(
    session: AsyncSession, user_id: str, project_id: uuid.UUID
) -> tuple[Project, int]:
    row = (
        await session.execute(
            select(Project, _document_count()).where(
                Project.id == project_id, Project.user_id == user_id
            )
        )
    ).first()
    if row is None:
        raise NotFound()
    return row[0], row[1]
