"""Upserts stay under Qdrant's 32 MB request limit (~1,250 1024-dim points per request)."""

import uuid

from doc_intel.core.tenancy import TenantContext
from doc_intel.embeddings.schemas import Embedding, SparseVector
from doc_intel.ingestion.schemas import ChunkPayload
from doc_intel.vector.qdrant_store import UPSERT_BATCH, QdrantStore

CTX = TenantContext("user_1", uuid.uuid4())


class RecordingClient:
    def __init__(self) -> None:
        self.upserts: list[tuple[int, object]] = []
        self.exists_calls = 0

    async def upsert(self, collection, points, wait, update_filter):
        self.upserts.append((len(points), update_filter))

    async def collection_exists(self, collection):
        self.exists_calls += 1
        return False

    async def create_collection(self, *args, **kwargs):
        pass

    async def create_payload_index(self, *args, **kwargs):
        pass


def _chunks(n: int):
    doc = uuid.uuid4()
    chunks = [
        ChunkPayload(
            document_id=doc,
            filename="f.txt",
            page_number=None,
            section_heading=None,
            element_type="text",
            chunk_index=i,
            text=f"chunk {i}",
        )
        for i in range(n)
    ]
    emb = Embedding(dense=[1.0] + [0.0] * 1023, sparse=SparseVector(indices=[1], values=[1.0]))
    return chunks, [emb] * n


async def test_large_upserts_are_split_into_batches_of_256():
    client = RecordingClient()
    await QdrantStore(client, "c").upsert_chunks(CTX, *_chunks(600))
    assert UPSERT_BATCH == 256
    assert [n for n, _ in client.upserts] == [256, 256, 88]
    # Every batch keeps the cross-tenant overwrite guard.
    assert all(f is not None for _, f in client.upserts)


async def test_small_upsert_is_one_request_and_empty_is_none():
    client = RecordingClient()
    store = QdrantStore(client, "c")
    await store.upsert_chunks(CTX, *_chunks(3))
    await store.upsert_chunks(CTX, [], [])
    assert [n for n, _ in client.upserts] == [3]


async def test_ensure_collection_runs_once_per_store():
    client = RecordingClient()
    store = QdrantStore(client, "c")
    await store.ensure_collection()
    await store.ensure_collection()
    assert client.exists_calls == 1
