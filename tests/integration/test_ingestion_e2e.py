"""Upload -> ingest_document job -> parsed JSON + counts (Phase 2 exit gate)."""

import asyncio
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from doc_intel.core.config import Settings, get_settings
from doc_intel.core.tenancy import TenantContext
from doc_intel.db import repo
from doc_intel.db.models import Job
from doc_intel.db.session import make_engine
from doc_intel.ingestion.schemas import ParsedDocument
from doc_intel.ingestion.upload import parsed_key
from doc_intel.jobs import handlers as handlers_mod
from doc_intel.jobs.handlers import make_failure_hooks, make_handlers
from doc_intel.jobs.worker import MAX_JOB_ATTEMPTS, recover_running_jobs, run_one
from doc_intel.main import create_app
from doc_intel.storage.local import LocalStorage
from doc_intel.vector.qdrant_store import QdrantStore, chunk_id
from tests.conftest import headers
from tests.fakes import FakeEmbedder
from tests.fixtures import sources

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("empty_queue")]


def _upload(client, user, project_id, name, data):
    r = client.post(
        f"/v1/projects/{project_id}/documents", files={"file": (name, data)}, headers=headers(user)
    )
    assert r.status_code == 201, r.text
    return r.json()


def _store() -> QdrantStore:
    settings = get_settings()
    return QdrantStore.connect(settings.qdrant_url, settings.qdrant_collection)


async def _process(migrated_db, rounds=1):
    engine = make_engine(migrated_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    settings = get_settings()
    store = _store()
    storage = LocalStorage(settings.storage_root)
    handlers = make_handlers(sm, storage, settings, FakeEmbedder(), store)
    try:
        for _ in range(rounds):
            await run_one(sm, handlers, make_failure_hooks(sm))
    finally:
        await store.close()
        await engine.dispose()


async def _point_count(user, project_id, document_id, max_chunks=200) -> int:
    """Points this tenant can see for the document (chunk ids are deterministic)."""
    store = _store()
    ids = [chunk_id(uuid.UUID(document_id), i) for i in range(max_chunks)]
    try:
        return len(await store.retrieve(TenantContext(user, uuid.UUID(project_id)), ids))
    finally:
        await store.close()


async def _rows(migrated_db, user, project_id, document_id, job_id):
    engine = make_engine(migrated_db)
    async with async_sessionmaker(engine)() as s:
        ctx = TenantContext(user, uuid.UUID(project_id))
        doc = await repo.get_document(s, ctx, uuid.UUID(document_id))
        job = await repo.get_job(s, ctx, uuid.UUID(job_id))
    await engine.dispose()
    return doc, job


@pytest.mark.slow
@pytest.mark.parametrize(
    "name, build, page_count",
    [
        ("notes.txt", sources.txt, None),
        ("report.pdf", sources.text_pdf, 2),
        ("budget.xlsx", sources.xlsx, None),
    ],
)
async def test_upload_is_parsed_and_chunked(
    client, migrated_db, user_a, project, name, build, page_count
):
    up = _upload(client, user_a, project, name, build())
    await _process(migrated_db)
    doc, job = await _rows(migrated_db, user_a, project, up["document_id"], up["job_id"])

    assert job.status == "succeeded"
    assert job.result == {"page_count": page_count, "chunk_count": doc.chunk_count}
    assert doc.chunk_count and doc.chunk_count > 0
    assert doc.page_count == page_count
    assert doc.status == "indexed"
    assert await _point_count(user_a, project, up["document_id"]) == doc.chunk_count
    ctx = TenantContext(user_a, uuid.UUID(project))
    with LocalStorage(get_settings().storage_root).open_read(
        parsed_key(ctx, uuid.UUID(up["document_id"]))
    ) as f:
        parsed = ParsedDocument.model_validate(json.load(f))
    assert parsed.elements


async def test_retry_overwrites_existing_parsed_json(client, migrated_db, user_a, project):
    up = _upload(client, user_a, project, "notes.txt", sources.txt())
    ctx = TenantContext(user_a, uuid.UUID(project))
    key = parsed_key(ctx, uuid.UUID(up["document_id"]))
    LocalStorage(get_settings().storage_root).put_bytes(key, b"stale from a crashed attempt")
    await _process(migrated_db)
    _, job = await _rows(migrated_db, user_a, project, up["document_id"], up["job_id"])
    assert job.status == "succeeded"


@pytest.mark.slow
async def test_unparseable_file_fails_document_after_last_attempt(
    client, migrated_db, user_a, project
):
    up = _upload(client, user_a, project, "broken.pdf", b"%PDF-1.7 not really a pdf")
    await _process(migrated_db, rounds=MAX_JOB_ATTEMPTS)
    doc, job = await _rows(migrated_db, user_a, project, up["document_id"], up["job_id"])
    assert (job.status, job.error_code, job.attempts) == ("failed", "parse_error", MAX_JOB_ATTEMPTS)
    assert (doc.status, doc.error_code) == ("failed", "parse_error")


@pytest.mark.slow  # real Ollama qwen3-embedding via the app lifespan
@pytest.mark.llm
def test_app_lifespan_runs_the_worker(migrated_db, user_a):
    settings = Settings(job_concurrency=1)
    with TestClient(create_app(settings)) as client:
        pid = client.post("/v1/projects", json={"name": "w"}, headers=headers(user_a)).json()["id"]
        up = _upload(client, user_a, pid, "notes.txt", sources.txt())
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            job = client.get(f"/v1/projects/{pid}/jobs/{up['job_id']}", headers=headers(user_a))
            if job.json()["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.2)
    assert job.json()["status"] == "succeeded"
    assert job.json()["result"]["chunk_count"] > 0


def test_tests_run_without_a_background_worker():
    assert get_settings().job_concurrency == 0


def _parsed_exists(user, project_id, document_id) -> bool:
    ctx = TenantContext(user, uuid.UUID(project_id))
    key = parsed_key(ctx, uuid.UUID(document_id))
    return (get_settings().storage_root / key).exists()


async def test_document_deleted_mid_parse_leaves_no_parsed_text(
    client, migrated_db, user_a, project, monkeypatch
):
    up = _upload(client, user_a, project, "notes.txt", sources.txt())
    loop = asyncio.get_running_loop()
    real_parse = handlers_mod.parse_and_chunk

    def parse_then_user_deletes(*args, **kwargs):
        out = real_parse(*args, **kwargs)
        # The user deletes the document while the worker is still busy with it.
        r = asyncio.run_coroutine_threadsafe(
            asyncio.to_thread(
                client.delete,
                f"/v1/projects/{project}/documents/{up['document_id']}",
                headers=headers(user_a),
            ),
            loop,
        ).result()
        assert r.status_code == 204
        return out

    monkeypatch.setattr(handlers_mod, "parse_and_chunk", parse_then_user_deletes)
    await _process(migrated_db)

    assert not _parsed_exists(user_a, project, up["document_id"])
    assert await _point_count(user_a, project, up["document_id"]) == 0
    engine = make_engine(migrated_db)
    async with async_sessionmaker(engine)() as s:
        job = await s.get(Job, uuid.UUID(up["job_id"]))
    await engine.dispose()
    assert (job.status, job.attempts) == ("succeeded", 1)  # nothing left to retry


async def test_job_for_already_deleted_document_finishes_without_retries(
    client, migrated_db, user_a, project
):
    up = _upload(client, user_a, project, "notes.txt", sources.txt())
    url = f"/v1/projects/{project}/documents/{up['document_id']}"
    assert client.delete(url, headers=headers(user_a)).status_code == 204
    await _process(migrated_db)
    engine = make_engine(migrated_db)
    async with async_sessionmaker(engine)() as s:
        job = await s.get(Job, uuid.UUID(up["job_id"]))
    await engine.dispose()
    assert (job.status, job.attempts) == ("succeeded", 1)
    assert not _parsed_exists(user_a, project, up["document_id"])


async def test_interrupted_three_times_marks_document_failed(client, migrated_db, user_a, project):
    """A file that crashes the backend (e.g. out of memory) must not stay `parsing` forever."""
    up = _upload(client, user_a, project, "notes.txt", sources.txt())
    engine = make_engine(migrated_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    ctx = TenantContext(user_a, uuid.UUID(project))
    async with sm.begin() as s:
        await repo.update_job(
            s, ctx, uuid.UUID(up["job_id"]), status="running", attempts=MAX_JOB_ATTEMPTS
        )
        await repo.update_document(s, ctx, uuid.UUID(up["document_id"]), status="parsing")
    await recover_running_jobs(sm, make_failure_hooks(sm))
    await engine.dispose()
    doc, job = await _rows(migrated_db, user_a, project, up["document_id"], up["job_id"])
    assert (job.status, job.error_code) == ("failed", "interrupted")
    assert (doc.status, doc.error_code) == ("failed", "interrupted")


async def test_reuploading_a_failed_document_retries_it(client, migrated_db, user_a, project):
    up = _upload(client, user_a, project, "notes.txt", sources.txt())
    engine = make_engine(migrated_db)
    ctx = TenantContext(user_a, uuid.UUID(project))
    async with async_sessionmaker(engine).begin() as s:
        await repo.update_document(
            s, ctx, uuid.UUID(up["document_id"]), status="failed", error_code="parse_error"
        )
    await engine.dispose()

    again = _upload(client, user_a, project, "notes.txt", sources.txt())
    assert again["document_id"] == up["document_id"]
    assert again["deduplicated"] is True
    assert again["job_id"] is not None and again["job_id"] != up["job_id"]
    doc, job = await _rows(migrated_db, user_a, project, up["document_id"], again["job_id"])
    assert (doc.status, doc.error_code) == ("uploaded", None)
    assert (job.kind, job.status, job.payload) == (
        "ingest_document",
        "queued",
        {"document_id": up["document_id"]},
    )

    await _process(migrated_db, rounds=2)  # the old queued job and the retry both succeed
    doc, _ = await _rows(migrated_db, user_a, project, up["document_id"], again["job_id"])
    assert doc.status == "indexed" and doc.chunk_count


async def test_reuploading_a_healthy_document_still_deduplicates(client, user_a, project):
    up = _upload(client, user_a, project, "notes.txt", sources.txt())
    again = _upload(client, user_a, project, "notes.txt", sources.txt())
    assert (again["document_id"], again["job_id"], again["deduplicated"]) == (
        up["document_id"],
        None,
        True,
    )


async def test_deleting_an_indexed_document_removes_its_points(
    client, migrated_db, user_a, project
):
    up = _upload(client, user_a, project, "notes.txt", sources.txt())
    await _process(migrated_db)
    assert await _point_count(user_a, project, up["document_id"]) > 0
    url = f"/v1/projects/{project}/documents/{up['document_id']}"
    assert client.delete(url, headers=headers(user_a)).status_code == 204
    assert await _point_count(user_a, project, up["document_id"]) == 0
