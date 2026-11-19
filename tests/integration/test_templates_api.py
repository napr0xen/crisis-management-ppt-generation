import uuid

import pytest
from sqlalchemy import func, select

from doc_intel.core.tenancy import TenantContext
from doc_intel.db import repo
from doc_intel.db.models import Document, Job
from tests.conftest import headers
from tests.fixtures import sources

pytestmark = pytest.mark.integration

ANCHOR_MAP = {"schema_version": "1.0", "format": "pptx", "anchors": []}


class RecordingStore:
    def __init__(self) -> None:
        self.fail = False
        self.deleted: list[uuid.UUID] = []

    async def delete_document_points(self, ctx, document_id) -> None:
        if self.fail:
            raise ConnectionError("qdrant down")
        self.deleted.append(document_id)


@pytest.fixture(autouse=True)
def store(client):
    client.app.state.vector_store = RecordingStore()
    return client.app.state.vector_store


def upload(client, user, project_id, name="t.pptx", data=None):
    r = client.post(
        f"/v1/projects/{project_id}/documents",
        files={"file": (name, data or sources.pptx())},
        headers=headers(user),
    )
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


async def set_status(session, user, project_id, document_id, status):
    ctx = TenantContext(user, uuid.UUID(project_id))
    await repo.update_document(session, ctx, uuid.UUID(document_id), status=status)
    await session.commit()


def post(client, user, project_id, document_id):
    return client.post(
        f"/v1/projects/{project_id}/templates",
        json={"document_id": document_id},
        headers=headers(user),
    )


async def test_post_marks_template_enqueues_job_and_purges_points(
    client, session, user_a, project, store
):
    doc_id = upload(client, user_a, project)
    await set_status(session, user_a, project, doc_id, "indexed")

    r = post(client, user_a, project, doc_id)
    assert r.status_code == 202
    assert set(r.json()) == {"job_id"}

    session.expire_all()
    job = await session.get(Job, uuid.UUID(r.json()["job_id"]))
    assert job.kind == "extract_template"
    assert job.payload == {"document_id": doc_id}
    doc = await session.get(Document, uuid.UUID(doc_id))
    assert doc.role == "template"
    assert store.deleted == [uuid.UUID(doc_id)]


@pytest.mark.parametrize(
    "name, data",
    [
        ("a.txt", b"hello"),
        ("a.pdf", b"%PDF-1.7 x"),
        ("a.csv", b"a,b\n1,2\n"),
        ("a.md", b"# hi\n"),
    ],
)
async def test_non_office_document_is_415(client, session, user_a, project, name, data):
    doc_id = upload(client, user_a, project, name, data)
    await set_status(session, user_a, project, doc_id, "indexed")
    r = post(client, user_a, project, doc_id)
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "unsupported_media_type"


@pytest.mark.parametrize("status", ["uploaded", "parsing"])
async def test_document_still_ingesting_is_409_and_role_unchanged(
    client, session, user_a, project, status, store
):
    doc_id = upload(client, user_a, project)
    await set_status(session, user_a, project, doc_id, status)
    r = post(client, user_a, project, doc_id)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"
    session.expire_all()
    assert (await session.get(Document, uuid.UUID(doc_id))).role == "source"
    assert store.deleted == []


async def test_qdrant_down_is_503_and_nothing_changes(client, session, user_a, project, store):
    doc_id = upload(client, user_a, project)
    await set_status(session, user_a, project, doc_id, "indexed")
    store.fail = True
    r = post(client, user_a, project, doc_id)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "upstream_unavailable"
    session.expire_all()
    assert (await session.get(Document, uuid.UUID(doc_id))).role == "source"
    jobs = await session.scalar(
        select(func.count()).where(Job.user_id == user_a, Job.kind == "extract_template")
    )
    assert jobs == 0


async def test_other_users_document_is_404(client, session, user_a, user_b, project):
    doc_id = upload(client, user_a, project)
    await set_status(session, user_a, project, doc_id, "indexed")
    b_project = client.post("/v1/projects", json={"name": "B"}, headers=headers(user_b)).json()
    assert post(client, user_b, b_project["id"], doc_id).status_code == 404
    assert post(client, user_b, project, doc_id).status_code == 404
    session.expire_all()
    assert (await session.get(Document, uuid.UUID(doc_id))).role == "source"


def test_unknown_field_and_bad_id_are_422(client, user_a, project):
    url = f"/v1/projects/{project}/templates"
    r = client.post(url, json={"document_id": str(uuid.uuid4()), "x": 1}, headers=headers(user_a))
    assert r.status_code == 422
    assert (
        client.post(url, json={"document_id": "nope"}, headers=headers(user_a)).status_code == 422
    )


async def seed_template(session, user, project_id, doc_id):
    ctx = TenantContext(user, uuid.UUID(project_id))
    t = await repo.create_template(
        session,
        ctx,
        template_id=uuid.uuid4(),
        document_id=uuid.UUID(doc_id),
        format="pptx",
        anchor_map=ANCHOR_MAP,
        anchor_count=0,
    )
    ir = await repo.create_template_ir(session, ctx, template_id=t.id, version=1, ir={"fields": []})
    await session.commit()
    return t, ir


async def test_get_template_and_ir(client, session, user_a, project):
    doc_id = upload(client, user_a, project)
    t, ir = await seed_template(session, user_a, project, doc_id)
    base = f"/v1/projects/{project}/templates/{t.id}"

    r = client.get(base, headers=headers(user_a))
    assert r.status_code == 200
    assert r.json() == {
        "id": str(t.id),
        "document_id": doc_id,
        "format": "pptx",
        "anchor_count": 0,
        "latest_ir_version": 1,
        "anchor_map": ANCHOR_MAP,
    }

    r = client.get(f"{base}/irs/1", headers=headers(user_a))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"id", "version", "ir", "created_at"}
    assert body["id"] == str(ir.id)
    assert body["version"] == 1
    assert body["ir"] == {"fields": []}


async def test_unknown_version_and_template_are_404_and_zero_is_422(
    client, session, user_a, project
):
    doc_id = upload(client, user_a, project)
    t, _ = await seed_template(session, user_a, project, doc_id)
    base = f"/v1/projects/{project}/templates"
    assert client.get(f"{base}/{t.id}/irs/2", headers=headers(user_a)).status_code == 404
    assert client.get(f"{base}/{t.id}/irs/0", headers=headers(user_a)).status_code == 422
    assert client.get(f"{base}/{uuid.uuid4()}", headers=headers(user_a)).status_code == 404


async def test_other_users_template_is_404(client, session, user_a, user_b, project):
    doc_id = upload(client, user_a, project)
    t, _ = await seed_template(session, user_a, project, doc_id)
    b_project = client.post("/v1/projects", json={"name": "B"}, headers=headers(user_b)).json()
    for pid in (project, b_project["id"]):
        base = f"/v1/projects/{pid}/templates/{t.id}"
        assert client.get(base, headers=headers(user_b)).status_code == 404
        assert client.get(f"{base}/irs/1", headers=headers(user_b)).status_code == 404


async def test_reupload_of_failed_template_is_not_reingested(client, session, user_a, project):
    doc_id = upload(client, user_a, project)
    await set_status(session, user_a, project, doc_id, "failed")
    assert post(client, user_a, project, doc_id).status_code == 202

    async def ingest_jobs() -> int:
        return await session.scalar(
            select(func.count()).where(Job.user_id == user_a, Job.kind == "ingest_document")
        )

    before = await ingest_jobs()
    r = client.post(
        f"/v1/projects/{project}/documents",
        files={"file": ("t.pptx", sources.pptx())},
        headers=headers(user_a),
    )
    assert r.status_code == 201
    assert r.json() == {"document_id": doc_id, "job_id": None, "deduplicated": True}

    session.expire_all()
    assert await ingest_jobs() == before
    doc = await session.get(Document, uuid.UUID(doc_id))
    assert (doc.status, doc.role) == ("failed", "template")
