"""ingest_document step 5: embed the chunks, replace the document's points, mark it `indexed`.

Real Postgres + LocalStorage; the embedder and vector store are in-test fakes.
"""

import threading
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from doc_intel.core.config import get_settings
from doc_intel.core.tenancy import TenantContext
from doc_intel.db import repo
from doc_intel.db.models import Job
from doc_intel.db.session import make_engine
from doc_intel.embeddings.schemas import Embedding, SparseVector
from doc_intel.ingestion.schemas import ChunkPayload, ParsedDocument
from doc_intel.ingestion.upload import parsed_key
from doc_intel.jobs import handlers as handlers_mod
from doc_intel.jobs.handlers import make_failure_hooks, make_handlers
from doc_intel.jobs.worker import MAX_JOB_ATTEMPTS, run_one
from doc_intel.mapping.retriever import Retriever
from doc_intel.storage.local import LocalStorage
from tests.conftest import headers
from tests.fakes import FakeLLM
from tests.fixtures import sources

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("empty_queue")]


class FakeEmbedder:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []

    async def embed_documents(self, texts: list[str]) -> list[Embedding]:
        self.calls.append(texts)
        if self.fail:
            raise RuntimeError("model crashed")
        return [
            Embedding(dense=[float(len(t)), 1.0], sparse=SparseVector(indices=[i], values=[1.0]))
            for i, t in enumerate(texts)
        ]


class FakeVectorStore:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.on_upsert = None  # optional async hook, run before recording
        self.ensured = 0

    async def ensure_collection(self) -> None:
        self.ensured += 1  # not in `calls`: the delete/upsert order assertions stay unchanged

    async def delete_document_points(self, ctx, document_id) -> None:
        self.calls.append(("delete", ctx, document_id))

    async def upsert_chunks(self, ctx, chunks, embeddings) -> None:
        if self.on_upsert:
            await self.on_upsert()
        self.calls.append(("upsert", ctx, chunks, embeddings))


@pytest.fixture
async def sm(migrated_db):
    engine = make_engine(migrated_db)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def upload(client, user_a, project):
    r = client.post(
        f"/v1/projects/{project}/documents",
        files={"file": ("notes.txt", sources.txt())},
        headers=headers(user_a),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    return uuid.UUID(body["document_id"]), uuid.UUID(body["job_id"])


@pytest.fixture
def ctx(user_a, project) -> TenantContext:
    return TenantContext(user_a, uuid.UUID(project))


class ThreadRecordingStorage(LocalStorage):
    """LocalStorage that records which thread each file operation ran on."""

    def __init__(self, root) -> None:
        super().__init__(root)
        self.ops: list[tuple[str, int]] = []

    def open_read(self, key):
        self.ops.append(("open_read", threading.get_ident()))
        return super().open_read(key)

    def put_bytes(self, key, data, overwrite=False):
        self.ops.append(("put_bytes", threading.get_ident()))
        super().put_bytes(key, data, overwrite)

    def delete(self, key):
        self.ops.append(("delete", threading.get_ident()))
        super().delete(key)


async def _process(sm, store, embedder, rounds=1, storage=None):
    settings = get_settings()
    storage = storage or LocalStorage(settings.storage_root)
    handlers = make_handlers(
        sm, storage, settings, embedder, store, FakeLLM(), Retriever(embedder, store)
    )
    for _ in range(rounds):
        await run_one(sm, handlers, make_failure_hooks(sm))


async def _doc_and_job(sm, ctx, document_id, job_id):
    async with sm() as s:
        return await repo.get_document(s, ctx, document_id), await s.get(Job, job_id)


def _parsed_exists(ctx, document_id) -> bool:
    return (get_settings().storage_root / parsed_key(ctx, document_id)).exists()


async def test_chunks_are_embedded_and_indexed(sm, ctx, upload, monkeypatch):
    document_id, job_id = upload
    stages = []
    real_set_progress = handlers_mod.set_progress

    async def record(sessionmaker, c, jid, progress):
        stages.append(progress)
        await real_set_progress(sessionmaker, c, jid, progress)

    monkeypatch.setattr(handlers_mod, "set_progress", record)
    store, embedder = FakeVectorStore(), FakeEmbedder()
    await _process(sm, store, embedder)

    doc, job = await _doc_and_job(sm, ctx, document_id, job_id)
    assert job.status == "succeeded"
    assert doc.status == "indexed"
    assert doc.chunk_count and doc.chunk_count > 0
    assert job.result == {"page_count": doc.page_count, "chunk_count": doc.chunk_count}

    # Stale points go first, then the fresh ones, all for this tenant and document.
    assert [c[0] for c in store.calls] == ["delete", "upsert"]
    assert store.calls[0][1:] == (ctx, document_id)
    _, upsert_ctx, chunks, embeddings = store.calls[1]
    assert upsert_ctx == ctx
    assert len(chunks) == len(embeddings) == doc.chunk_count
    assert {c.document_id for c in chunks} == {document_id}
    assert embedder.calls == [[c.text for c in chunks]]

    n = doc.chunk_count
    assert stages == [
        {"stage": "parsing"},
        {"stage": "embedding", "done": 0, "total": n},
        {"stage": "embedding", "done": n, "total": n},
    ]
    assert job.progress == {"stage": "embedding", "done": n, "total": n}


async def test_document_without_chunks_is_indexed_empty(sm, ctx, upload, monkeypatch):
    document_id, job_id = upload
    monkeypatch.setattr(
        handlers_mod, "parse_and_chunk", lambda *a, **k: (ParsedDocument(elements=[]), [])
    )
    store, embedder = FakeVectorStore(), FakeEmbedder()
    await _process(sm, store, embedder)

    doc, job = await _doc_and_job(sm, ctx, document_id, job_id)
    assert job.status == "succeeded"
    assert (doc.status, doc.chunk_count) == ("indexed", 0)
    assert embedder.calls == []
    assert store.calls == [("delete", ctx, document_id)]  # a previous run's points still go


async def test_rerun_replaces_points_again(sm, ctx, upload):
    document_id, _ = upload
    store, embedder = FakeVectorStore(), FakeEmbedder()
    await _process(sm, store, embedder)
    async with sm.begin() as s:
        again = await repo.create_job(s, ctx, "ingest_document", {"document_id": str(document_id)})
    await _process(sm, store, embedder)

    doc, job = await _doc_and_job(sm, ctx, document_id, again.id)
    assert job.status == "succeeded"
    assert doc.status == "indexed"
    assert [c[0] for c in store.calls] == ["delete", "upsert", "delete", "upsert"]
    assert store.calls[1][2] == store.calls[3][2]  # same chunks both times


async def test_deleted_while_indexing_leaves_no_points_or_text(sm, ctx, upload):
    document_id, job_id = upload
    store = FakeVectorStore()

    async def user_deletes():
        async with sm.begin() as s:
            await repo.delete_document(s, ctx, document_id)

    store.on_upsert = user_deletes
    await _process(sm, store, FakeEmbedder())

    assert [c[0] for c in store.calls] == ["delete", "upsert", "delete"]
    assert store.calls[-1] == ("delete", ctx, document_id)
    assert not _parsed_exists(ctx, document_id)
    async with sm() as s:
        job = await s.get(Job, job_id)
    assert (job.status, job.attempts) == ("succeeded", 1)  # nothing left to retry


async def test_embedder_failure_retries_then_fails_document(sm, ctx, upload):
    document_id, job_id = upload
    store, embedder = FakeVectorStore(), FakeEmbedder(fail=True)

    await _process(sm, store, embedder)
    doc, job = await _doc_and_job(sm, ctx, document_id, job_id)
    assert (job.status, job.attempts) == ("queued", 1)  # retried
    assert doc.status == "parsing"

    await _process(sm, store, embedder, rounds=MAX_JOB_ATTEMPTS - 1)
    doc, job = await _doc_and_job(sm, ctx, document_id, job_id)
    assert (job.status, job.error_code, job.attempts) == ("failed", "internal", MAX_JOB_ATTEMPTS)
    assert (doc.status, doc.error_code) == ("failed", "internal")
    assert len(embedder.calls) == MAX_JOB_ATTEMPTS
    assert [c[0] for c in store.calls] == ["delete"] * MAX_JOB_ATTEMPTS  # nothing upserted


async def test_large_documents_are_embedded_and_upserted_in_batches(sm, ctx, upload, monkeypatch):
    # Only one batch of vectors is held in memory at a time (a 100 MB sheet can be 10k+ chunks).
    document_id, job_id = upload
    chunks = [
        ChunkPayload(
            document_id=document_id,
            filename="notes.txt",
            page_number=None,
            section_heading=None,
            element_type="text",
            chunk_index=i,
            text=f"chunk {i}",
        )
        for i in range(5)
    ]
    monkeypatch.setattr(
        handlers_mod, "parse_and_chunk", lambda *a, **k: (ParsedDocument(elements=[]), chunks)
    )
    monkeypatch.setattr(handlers_mod, "INDEX_BATCH", 2)
    store, embedder = FakeVectorStore(), FakeEmbedder()
    await _process(sm, store, embedder)

    doc, job = await _doc_and_job(sm, ctx, document_id, job_id)
    assert (job.status, doc.status, doc.chunk_count) == ("succeeded", "indexed", 5)
    assert [len(texts) for texts in embedder.calls] == [2, 2, 1]
    assert [c[0] for c in store.calls] == ["delete", "upsert", "upsert", "upsert"]
    assert [c.chunk_index for call in store.calls[1:] for c in call[2]] == [0, 1, 2, 3, 4]
    assert job.progress == {"stage": "embedding", "done": 5, "total": 5}


async def test_file_io_runs_off_the_event_loop(sm, ctx, upload):
    # A 100 MB read on the loop would freeze every API request while the job runs.
    document_id, _ = upload
    store = FakeVectorStore()

    async def user_deletes():
        async with sm.begin() as s:
            await repo.delete_document(s, ctx, document_id)

    store.on_upsert = user_deletes  # also exercises the cleanup delete
    storage = ThreadRecordingStorage(get_settings().storage_root)
    await _process(sm, store, FakeEmbedder(), storage=storage)

    assert {op for op, _ in storage.ops} == {"open_read", "put_bytes", "delete"}
    loop_thread = threading.get_ident()
    assert all(tid != loop_thread for _, tid in storage.ops)


async def test_template_document_is_never_indexed(sm, ctx, upload):
    document_id, job_id = upload
    async with sm.begin() as s:
        await repo.update_document(s, ctx, document_id, role="template")
    store, embedder = FakeVectorStore(), FakeEmbedder()
    await _process(sm, store, embedder)

    doc, job = await _doc_and_job(sm, ctx, document_id, job_id)
    assert job.status == "succeeded"
    assert doc.status == "uploaded"
    assert store.calls == [] and embedder.calls == []
