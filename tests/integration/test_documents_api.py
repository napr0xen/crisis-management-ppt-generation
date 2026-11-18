import hashlib
import uuid

import pytest
from sqlalchemy import func, select

from doc_intel.db.models import Job, MappingRun, Template, TemplateIRVersion
from tests.conftest import headers

pytestmark = pytest.mark.integration

PDF = b"%PDF-1.7 quarterly report"
DOCUMENT_KEYS = {
    "id",
    "project_id",
    "role",
    "filename",
    "mime_type",
    "size_bytes",
    "sha256",
    "status",
    "page_count",
    "chunk_count",
    "error_code",
    "created_at",
}
JOB_KEYS = {
    "id",
    "project_id",
    "kind",
    "status",
    "progress",
    "result",
    "error_code",
    "error_message",
    "created_at",
    "started_at",
    "finished_at",
}


def upload(client, user_id, project_id, name="report.pdf", data=PDF):
    return client.post(
        f"/v1/projects/{project_id}/documents",
        files={"file": (name, data, "application/octet-stream")},
        headers=headers(user_id),
    )


def read_key(client, key: str) -> bytes:
    with client.app.state.storage.open_read(key) as f:
        return f.read()


async def test_upload_stores_file_and_enqueues_ingest(client, session, user_a, project):
    r = upload(client, user_a, project)
    assert r.status_code == 201
    body = r.json()
    assert body["deduplicated"] is False
    assert set(body) == {"document_id", "job_id", "deduplicated"}

    doc = client.get(
        f"/v1/projects/{project}/documents/{body['document_id']}", headers=headers(user_a)
    ).json()
    assert set(doc) == DOCUMENT_KEYS
    assert doc["project_id"] == project
    assert doc["status"] == "uploaded"
    assert doc["role"] == "source"
    assert doc["filename"] == "report.pdf"
    assert doc["mime_type"] == "application/pdf"  # from the extension, not the client
    assert doc["size_bytes"] == len(PDF)
    assert doc["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert doc["page_count"] is doc["chunk_count"] is doc["error_code"] is None

    job = client.get(f"/v1/projects/{project}/jobs/{body['job_id']}", headers=headers(user_a))
    assert job.status_code == 200
    job = job.json()
    assert set(job) == JOB_KEYS
    assert job["kind"] == "ingest_document"
    assert job["status"] == "queued"
    assert job["progress"] == {}
    assert job["result"] is job["started_at"] is job["finished_at"] is None

    row = await session.get(Job, uuid.UUID(body["job_id"]))
    assert row.payload == {"document_id": body["document_id"]}
    key = f"users/{user_a}/projects/{project}/uploads/{body['document_id']}/original.pdf"
    assert read_key(client, key) == PDF


async def test_duplicate_upload_is_deduplicated(client, session, user_a, project):
    first = upload(client, user_a, project).json()
    again = upload(client, user_a, project, name="renamed.pdf")
    assert again.status_code == 201
    assert again.json() == {
        "document_id": first["document_id"],
        "job_id": None,
        "deduplicated": True,
    }
    docs = client.get(f"/v1/projects/{project}/documents", headers=headers(user_a)).json()
    assert [d["id"] for d in docs["items"]] == [first["document_id"]]
    jobs = await session.scalar(
        select(func.count()).where(Job.project_id == uuid.UUID(project), Job.user_id == user_a)
    )
    assert jobs == 1


def test_list_is_newest_first_and_limited(client, user_a, project):
    ids = [
        upload(client, user_a, project, data=PDF + bytes([i])).json()["document_id"]
        for i in range(3)
    ]
    r = client.get(f"/v1/projects/{project}/documents?limit=2", headers=headers(user_a))
    assert [d["id"] for d in r.json()["items"]] == ids[::-1][:2]
    over = client.get(f"/v1/projects/{project}/documents?limit=201", headers=headers(user_a))
    assert over.status_code == 422


def test_delete_removes_row_and_files(client, user_a, project):
    doc_id = upload(client, user_a, project).json()["document_id"]
    base = f"users/{user_a}/projects/{project}"
    client.app.state.storage.put_bytes(f"{base}/parsed/{doc_id}.json", b"{}", overwrite=True)

    r = client.delete(f"/v1/projects/{project}/documents/{doc_id}", headers=headers(user_a))
    assert r.status_code == 204
    assert r.content == b""
    got = client.get(f"/v1/projects/{project}/documents/{doc_id}", headers=headers(user_a))
    assert got.status_code == 404
    for key in (f"{base}/uploads/{doc_id}/original.pdf", f"{base}/parsed/{doc_id}.json"):
        with pytest.raises(FileNotFoundError):
            read_key(client, key)


async def test_delete_template_with_mapping_runs_is_409(client, session, user_a, project):
    doc_id = uuid.UUID(upload(client, user_a, project).json()["document_id"])
    pid = uuid.UUID(project)
    template = Template(
        user_id=user_a,
        project_id=pid,
        document_id=doc_id,
        format="pptx",
        anchor_map={},
        anchor_count=0,
    )
    session.add(template)
    await session.flush()
    ir = TemplateIRVersion(
        user_id=user_a, project_id=pid, template_id=template.id, version=1, ir={}
    )
    session.add(ir)
    await session.flush()
    session.add(
        MappingRun(
            user_id=user_a,
            project_id=pid,
            template_ir_id=ir.id,
            version=1,
            trigger="initial",
            status="completed",
            document_ids=[],
        )
    )
    await session.commit()

    r = client.delete(f"/v1/projects/{project}/documents/{doc_id}", headers=headers(user_a))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"
    got = client.get(f"/v1/projects/{project}/documents/{doc_id}", headers=headers(user_a))
    assert got.status_code == 200


def test_unsupported_upload_is_415_and_writes_nothing(client, user_a, project):
    r = upload(client, user_a, project, name="legacy.doc", data=b"\xd0\xcf\x11\xe0")
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "unsupported_media_type"
    r = upload(client, user_a, project, name="fake.pdf", data=b"not a pdf")
    assert r.status_code == 415
    docs = client.get(f"/v1/projects/{project}/documents", headers=headers(user_a)).json()
    assert docs["items"] == []


def test_oversize_upload_is_413(client, user_a, project, monkeypatch):
    monkeypatch.setattr(client.app.state.settings, "max_upload_mb", 1)
    r = upload(client, user_a, project, name="big.txt", data=b"x" * (1024 * 1024 + 1))
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"
    assert (
        upload(client, user_a, project, name="ok.txt", data=b"x" * 1024 * 1024).status_code == 201
    )


def test_missing_file_is_422(client, user_a, project):
    r = client.post(f"/v1/projects/{project}/documents", headers=headers(user_a))
    assert r.status_code == 422


def test_error_responses_do_not_echo_the_filename(client, user_a, project):
    secret = "SENTINEL_layoffs_plan"
    r = upload(client, user_a, project, name=f"{secret}.exe", data=b"MZ")
    assert r.status_code == 415
    assert secret not in r.text
