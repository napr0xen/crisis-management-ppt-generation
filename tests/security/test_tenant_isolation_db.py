import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from doc_intel.core.errors import NotFound
from doc_intel.db import repo
from doc_intel.db.models import Document, Job

pytestmark = pytest.mark.integration


async def test_repo_functions_are_user_scoped(session, user_a, user_b):
    project = await repo.create_project(session, user_a, "A")
    with pytest.raises(NotFound):
        await repo.get_project(session, user_b, project.id)
    assert await repo.list_projects(session, user_b, 50) == []
    assert (await repo.get_project(session, user_a, project.id))[0].id == project.id


@pytest.mark.parametrize(
    "make_row",
    [
        lambda pid, uid: Job(project_id=pid, user_id=uid, kind="ingest_document", payload={}),
        lambda pid, uid: Document(
            project_id=pid,
            user_id=uid,
            filename="f.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            sha256="0" * 64,
            storage_key="k",
            status="uploaded",
        ),
    ],
)
async def test_composite_fk_rejects_child_row_on_other_users_project(
    session, user_a, user_b, make_row
):
    project = await repo.create_project(session, user_a, "A")
    session.add(make_row(project.id, user_b))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_child_row_with_matching_tenant_is_accepted(session, user_a):
    project = await repo.create_project(session, user_a, "A")
    session.add(Job(project_id=project.id, user_id=user_a, kind="ingest_document", payload={}))
    await session.commit()


async def test_project_delete_cascades_to_children(session, user_a):
    project = await repo.create_project(session, user_a, "A")
    job = Job(project_id=project.id, user_id=user_a, kind="ingest_document", payload={})
    session.add(job)
    await session.commit()
    await session.delete(project)
    await session.commit()
    assert await session.get(Job, job.id, populate_existing=True) is None


async def test_unknown_project_id_is_not_found(session, user_a):
    with pytest.raises(NotFound):
        await repo.get_project(session, user_a, uuid.uuid4())
