import uuid

import pytest

from doc_intel.core.errors import Conflict
from doc_intel.core.tenancy import TenantContext
from doc_intel.db import repo
from doc_intel.db.models import Document

pytestmark = pytest.mark.integration


async def seed_ir(session, user, version=1):
    """Project + template document + template + IR; returns (ctx, ir_row)."""
    project = await repo.create_project(session, user, "P")
    ctx = TenantContext(user, project.id)
    doc = await repo.create_document(
        session,
        ctx,
        uuid.uuid4(),
        filename="t.pptx",
        mime_type="x",
        size_bytes=1,
        sha256=uuid.uuid4().hex * 2,
        storage_key="k",
    )
    template = await repo.create_template(
        session,
        ctx,
        template_id=uuid.uuid4(),
        document_id=doc.id,
        format="pptx",
        anchor_map={},
        anchor_count=0,
    )
    ir = await repo.create_template_ir(
        session, ctx, template_id=template.id, version=version, ir={}
    )
    return ctx, ir


async def add_doc(session, ctx, *, role="source", status="indexed"):
    doc = await repo.create_document(
        session,
        ctx,
        uuid.uuid4(),
        filename="d.pdf",
        mime_type="x",
        size_bytes=1,
        sha256=uuid.uuid4().hex * 2,
        storage_key="k",
    )
    await repo.update_document(session, ctx, doc.id, role=role, status=status)
    return doc.id


async def test_versions_increment_per_template_ir(session, user_a):
    ctx, ir1 = await seed_ir(session, user_a)
    ir2 = await repo.create_template_ir(session, ctx, template_id=ir1.template_id, version=2, ir={})
    docs = [uuid.uuid4()]
    runs = [
        await repo.create_mapping_run(session, ctx, template_ir_id=i, document_ids=docs)
        for i in (ir1.id, ir1.id, ir2.id)
    ]
    assert [r.version for r in runs] == [1, 2, 1]
    assert all((r.trigger, r.status, r.document_ids) == ("initial", "queued", docs) for r in runs)
    assert runs[0].results == {} and runs[0].finished_at is None


class StaleMax:
    """Makes the first `n` version lookups see max=0: the lost race against another insert."""

    def __init__(self, session, n):
        self.real, self.n = session.scalar, n
        session.scalar = self

    async def __call__(self, stmt, *a, **kw):
        if self.n > 0 and "max" in str(stmt):
            self.n -= 1
            return 1
        return await self.real(stmt, *a, **kw)


async def test_version_race_retries_once(session, user_a):
    ctx, ir = await seed_ir(session, user_a)
    await repo.create_mapping_run(session, ctx, template_ir_id=ir.id, document_ids=[])
    StaleMax(session, 1)
    run = await repo.create_mapping_run(session, ctx, template_ir_id=ir.id, document_ids=[])
    assert run.version == 2


async def test_version_race_twice_is_conflict_and_session_stays_usable(session, user_a):
    ctx, ir = await seed_ir(session, user_a)
    await repo.create_mapping_run(session, ctx, template_ir_id=ir.id, document_ids=[])
    StaleMax(session, 2)
    with pytest.raises(Conflict):
        await repo.create_mapping_run(session, ctx, template_ir_id=ir.id, document_ids=[])
    # the savepoint rolled back: the first run is intact and the session still works
    assert (await repo.indexed_source_document_ids(session, ctx)) == []


async def test_get_and_update_mapping_run(session, user_a):
    ctx, ir = await seed_ir(session, user_a)
    run = await repo.create_mapping_run(session, ctx, template_ir_id=ir.id, document_ids=[])
    run_id, ir_id = run.id, ir.id
    await repo.update_mapping_run(session, ctx, run_id, status="extracting")
    session.expire_all()
    assert (await repo.get_mapping_run(session, ctx, run_id)).status == "extracting"
    assert (await repo.get_template_ir_by_id(session, ctx, ir_id)).id == ir_id


async def test_indexed_source_document_ids_filters_and_orders(session, user_a):
    ctx, _ = await seed_ir(session, user_a)
    first = await add_doc(session, ctx)
    await add_doc(session, ctx, status="uploaded")
    await add_doc(session, ctx, status="failed")
    template_doc = await add_doc(session, ctx, role="template")
    second = await add_doc(session, ctx)
    ids = await repo.indexed_source_document_ids(session, ctx)
    assert template_doc not in ids
    assert ids == sorted([first, second])  # same created_at within one transaction


async def test_get_documents_returns_owned_subset(session, user_a):
    ctx, _ = await seed_ir(session, user_a)
    a = await add_doc(session, ctx)
    got = await repo.get_documents(session, ctx, [a, uuid.uuid4()])
    assert [d.id for d in got] == [a]
    assert all(isinstance(d, Document) for d in got)
    assert await repo.get_documents(session, ctx, []) == []
