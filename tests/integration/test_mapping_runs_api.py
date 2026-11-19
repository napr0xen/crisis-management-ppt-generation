"""`POST/GET /v1/projects/{p}/mapping-runs` against real Postgres, with seeded rows."""

import uuid

import pytest
from sqlalchemy import select

from doc_intel.core.tenancy import TenantContext
from doc_intel.db import repo
from doc_intel.db.models import Job, MappingRun
from doc_intel.templates.schemas import TemplateIR
from tests.conftest import headers

pytestmark = pytest.mark.integration

ANCHOR_MAP = {"schema_version": "1.0", "format": "pptx", "anchors": []}


def ctx_of(user: str, project: str) -> TenantContext:
    return TenantContext(user, uuid.UUID(project))


async def seed_document(
    session, ctx, *, status="indexed", role="source", name="doc.txt"
) -> uuid.UUID:
    doc_id = uuid.uuid4()
    await repo.create_document(
        session,
        ctx,
        doc_id,
        filename=name,
        mime_type="text/plain",
        size_bytes=1,
        sha256=uuid.uuid4().hex,
        storage_key=f"{ctx.user_id}/{doc_id}.txt",
    )
    await repo.update_document(session, ctx, doc_id, status=status, role=role)
    await session.commit()
    return doc_id


def ir_json(template_id: uuid.UUID, fields: list[dict]) -> dict:
    return TemplateIR(template_id=template_id, version=1, format="pptx", fields=fields).model_dump(
        mode="json", exclude_none=True
    )


def field(field_id: str, expected_type: str = "text", **constraints) -> dict:
    return {
        "field_id": field_id,
        "anchor_id": f"a_{field_id}",
        "section_path": ["Slide 1"],
        "label": field_id.replace("_", " ").title(),
        "description": f"The {field_id}.",
        "expected_type": expected_type,
        "required": True,
        "write_mode": "replace_text",
        "constraints": constraints,
    }


async def seed_template_ir(session, ctx, fields=None) -> uuid.UUID:
    """A template document, its template row and IR version 1; returns the IR id."""
    doc_id = await seed_document(session, ctx, role="template", name="t.pptx")
    template_id = uuid.uuid4()
    await repo.create_template(
        session,
        ctx,
        template_id=template_id,
        document_id=doc_id,
        format="pptx",
        anchor_map=ANCHOR_MAP,
        anchor_count=0,
    )
    row = await repo.create_template_ir(
        session,
        ctx,
        template_id=template_id,
        version=1,
        ir=ir_json(template_id, fields or [field("incident_summary")]),
    )
    await session.commit()
    return row.id


def without_rid(response) -> dict:
    return {k: v for k, v in response.json()["error"].items() if k != "request_id"}


def post(client, user, project, **body):
    return client.post(f"/v1/projects/{project}/mapping-runs", json=body, headers=headers(user))


async def test_post_queues_run_and_job_and_versions_increment(client, session, user_a, project):
    ctx = ctx_of(user_a, project)
    ir_id = await seed_template_ir(session, ctx)
    source = await seed_document(session, ctx)

    r = post(client, user_a, project, template_ir_id=str(ir_id), document_ids=None)
    assert r.status_code == 202, r.text
    assert set(r.json()) == {"mapping_run_id", "job_id"}

    session.expire_all()
    run = await session.get(MappingRun, uuid.UUID(r.json()["mapping_run_id"]))
    assert (run.trigger, run.status, run.version) == ("initial", "queued", 1)
    assert (run.user_id, run.project_id) == (user_a, uuid.UUID(project))
    assert run.template_ir_id == ir_id
    assert run.document_ids == [source]
    job = await session.get(Job, uuid.UUID(r.json()["job_id"]))
    assert job.kind == "run_mapping"
    assert job.payload == {"mapping_run_id": str(run.id)}

    second = post(client, user_a, project, template_ir_id=str(ir_id))
    assert second.status_code == 202
    run2 = await session.get(MappingRun, uuid.UUID(second.json()["mapping_run_id"]))
    assert run2.version == 2


async def test_null_ids_resolve_to_indexed_sources_only(client, session, user_a, project):
    ctx = ctx_of(user_a, project)
    ir_id = await seed_template_ir(session, ctx)  # a role=template document
    good = await seed_document(session, ctx)
    await seed_document(session, ctx, status="failed")
    await seed_document(session, ctx, status="uploaded")

    r = post(client, user_a, project, template_ir_id=str(ir_id), document_ids=None)
    assert r.status_code == 202
    run = await session.get(MappingRun, uuid.UUID(r.json()["mapping_run_id"]))
    assert run.document_ids == [good]


async def test_explicit_ids_are_deduplicated_in_order(client, session, user_a, project):
    ctx = ctx_of(user_a, project)
    ir_id = await seed_template_ir(session, ctx)
    first, second = await seed_document(session, ctx), await seed_document(session, ctx)

    r = post(
        client,
        user_a,
        project,
        template_ir_id=str(ir_id),
        document_ids=[str(second), str(first), str(second)],
    )
    assert r.status_code == 202
    run = await session.get(MappingRun, uuid.UUID(r.json()["mapping_run_id"]))
    assert run.document_ids == [second, first]


async def test_unknown_template_ir_is_404(client, session, user_a, project):
    await seed_document(session, ctx_of(user_a, project))
    r = post(client, user_a, project, template_ir_id=str(uuid.uuid4()))
    assert r.status_code == 404


async def test_other_tenants_template_ir_is_404(client, session, user_a, user_b, project):
    b_project = client.post("/v1/projects", json={"name": "B"}, headers=headers(user_b)).json()
    b_ctx = ctx_of(user_b, b_project["id"])
    b_ir = await seed_template_ir(session, b_ctx)
    await seed_document(session, ctx_of(user_a, project))

    r = post(client, user_a, project, template_ir_id=str(b_ir))
    assert r.status_code == 404
    unknown = post(client, user_a, project, template_ir_id=str(uuid.uuid4()))
    assert without_rid(r) == without_rid(unknown)


async def test_unknown_and_other_tenants_documents_are_404(
    client, session, user_a, user_b, project
):
    ctx = ctx_of(user_a, project)
    ir_id = await seed_template_ir(session, ctx)
    mine = await seed_document(session, ctx)
    b_project = client.post("/v1/projects", json={"name": "B"}, headers=headers(user_b)).json()
    theirs = await seed_document(session, ctx_of(user_b, b_project["id"]))

    for other in (theirs, uuid.uuid4()):
        r = post(
            client, user_a, project, template_ir_id=str(ir_id), document_ids=[str(mine), str(other)]
        )
        assert r.status_code == 404


@pytest.mark.parametrize(
    "kwargs", [{"status": "uploaded"}, {"status": "failed"}, {"role": "template"}]
)
async def test_owned_but_unusable_document_is_422(client, session, user_a, project, kwargs):
    ctx = ctx_of(user_a, project)
    ir_id = await seed_template_ir(session, ctx)
    bad = await seed_document(session, ctx, **kwargs)

    r = post(client, user_a, project, template_ir_id=str(ir_id), document_ids=[str(bad)])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


async def test_empty_ids_list_is_422(client, session, user_a, project):
    ir_id = await seed_template_ir(session, ctx_of(user_a, project))
    assert (
        post(client, user_a, project, template_ir_id=str(ir_id), document_ids=[]).status_code == 422
    )


async def test_more_than_200_document_ids_is_422(client, session, user_a, project):
    ir_id = await seed_template_ir(session, ctx_of(user_a, project))
    ids = [str(uuid.uuid4()) for _ in range(201)]
    assert (
        post(client, user_a, project, template_ir_id=str(ir_id), document_ids=ids).status_code
        == 422
    )


async def test_no_indexed_sources_is_422(client, session, user_a, project):
    ir_id = await seed_template_ir(session, ctx_of(user_a, project))  # only a template document
    r = post(client, user_a, project, template_ir_id=str(ir_id))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


async def test_extra_body_key_is_422(client, session, user_a, project):
    ctx = ctx_of(user_a, project)
    ir_id = await seed_template_ir(session, ctx)
    await seed_document(session, ctx)
    assert post(client, user_a, project, template_ir_id=str(ir_id), extra=1).status_code == 422


async def test_rejected_requests_leave_no_run_or_job(client, session, user_a, project):
    ir_id = await seed_template_ir(session, ctx_of(user_a, project))
    post(client, user_a, project, template_ir_id=str(ir_id))  # 422: no sources
    assert await session.scalar(select(MappingRun).where(MappingRun.user_id == user_a)) is None
    kinds = (await session.scalars(select(Job.kind).where(Job.user_id == user_a))).all()
    assert "run_mapping" not in kinds


async def test_get_returns_run_with_null_artifact(client, session, user_a, project):
    ctx = ctx_of(user_a, project)
    ir_id = await seed_template_ir(session, ctx)
    source = await seed_document(session, ctx)
    run_id = post(client, user_a, project, template_ir_id=str(ir_id)).json()["mapping_run_id"]

    r = client.get(f"/v1/projects/{project}/mapping-runs/{run_id}", headers=headers(user_a))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "id", "template_ir_id", "parent_run_id", "version", "trigger", "status", "document_ids",
        "summary", "results", "artifact", "created_at", "finished_at",
    }  # fmt: skip
    assert body["id"] == run_id
    assert body["template_ir_id"] == str(ir_id)
    assert body["parent_run_id"] is None
    assert (body["version"], body["trigger"], body["status"]) == (1, "initial", "queued")
    assert body["document_ids"] == [str(source)]
    assert body["summary"] == {} and body["results"] == {}
    assert body["artifact"] is None and body["finished_at"] is None


async def test_get_other_tenant_and_unknown_run_are_identical_404(
    client, session, user_a, user_b, project
):
    ctx = ctx_of(user_a, project)
    ir_id = await seed_template_ir(session, ctx)
    await seed_document(session, ctx)
    run_id = post(client, user_a, project, template_ir_id=str(ir_id)).json()["mapping_run_id"]
    b_project = client.post("/v1/projects", json={"name": "B"}, headers=headers(user_b)).json()[
        "id"
    ]

    cross = client.get(f"/v1/projects/{b_project}/mapping-runs/{run_id}", headers=headers(user_b))
    unknown = client.get(
        f"/v1/projects/{b_project}/mapping-runs/{uuid.uuid4()}", headers=headers(user_b)
    )
    assert cross.status_code == unknown.status_code == 404
    assert without_rid(cross) == without_rid(unknown)
    missing = client.get(
        f"/v1/projects/{project}/mapping-runs/{uuid.uuid4()}", headers=headers(user_a)
    )
    assert missing.status_code == 404
