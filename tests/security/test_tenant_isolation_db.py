import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from doc_intel.core.errors import NotFound
from doc_intel.core.tenancy import TenantContext
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


async def _project_with_document_and_job(session, user_id):
    project = await repo.create_project(session, user_id, "A")
    ctx = TenantContext(user_id, project.id)
    document = await repo.create_document(
        session,
        ctx,
        uuid.uuid4(),
        filename="f.pdf",
        mime_type="application/pdf",
        size_bytes=1,
        sha256="a" * 64,
        storage_key="k",
    )
    job = await repo.create_job(session, ctx, "ingest_document", {"document_id": str(document.id)})
    return ctx, document, job


@pytest.mark.parametrize("mismatch", ["user_id", "project_id"])
async def test_document_and_job_functions_are_tenant_scoped(session, user_a, user_b, mismatch):
    ctx, document, job = await _project_with_document_and_job(session, user_a)
    other_project = await repo.create_project(session, user_a, "A2")
    wrong = (
        TenantContext(user_b, ctx.project_id)
        if mismatch == "user_id"
        else TenantContext(user_a, other_project.id)
    )

    assert await repo.list_documents(session, wrong, 50) == []
    assert await repo.find_document_by_sha256(session, wrong, document.sha256) is None
    for call in (
        repo.get_document(session, wrong, document.id),
        repo.update_document(session, wrong, document.id, status="failed"),
        repo.delete_document(session, wrong, document.id),
        repo.get_job(session, wrong, job.id),
        repo.update_job(session, wrong, job.id, status="failed"),
    ):
        with pytest.raises(NotFound):
            await call

    # the right tenant still sees both, unchanged
    assert (await repo.get_document(session, ctx, document.id)).status == "uploaded"
    assert (await repo.get_job(session, ctx, job.id)).status == "queued"
    assert [d.id for d in await repo.list_documents(session, ctx, 50)] == [document.id]
    assert (await repo.find_document_by_sha256(session, ctx, document.sha256)).id == document.id


async def test_ensure_project_is_user_scoped(session, user_a, user_b):
    project = await repo.create_project(session, user_a, "A")
    await repo.ensure_project(session, user_a, project.id)
    for user_id, project_id in ((user_b, project.id), (user_a, uuid.uuid4())):
        with pytest.raises(NotFound):
            await repo.ensure_project(session, user_id, project_id)


async def test_mapping_functions_are_tenant_scoped(session, user_a, user_b):
    from tests.integration.test_mapping_repo import add_doc, seed_ir

    ctx, ir = await seed_ir(session, user_a)
    run = await repo.create_mapping_run(session, ctx, template_ir_id=ir.id, document_ids=[])
    doc_id = await add_doc(session, ctx)
    other_project = await repo.create_project(session, user_a, "A2")
    for wrong in (
        TenantContext(user_b, ctx.project_id),
        TenantContext(user_a, other_project.id),
    ):
        for call in (
            repo.get_mapping_run(session, wrong, run.id),
            repo.get_template_ir_by_id(session, wrong, ir.id),
            repo.update_mapping_run(session, wrong, run.id, status="failed"),
        ):
            with pytest.raises(NotFound):
                await call
        assert await repo.get_documents(session, wrong, [doc_id]) == []
        assert await repo.indexed_source_document_ids(session, wrong) == []
    assert (await repo.get_mapping_run(session, ctx, run.id)).status == "queued"
    assert await repo.indexed_source_document_ids(session, ctx) == [doc_id]
