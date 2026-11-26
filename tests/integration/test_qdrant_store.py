"""`QdrantStore` against the live Qdrant, on the throwaway `doc_chunks_test` collection."""

import uuid

import pytest
from fastapi.testclient import TestClient
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as m

from doc_intel.core.config import Settings, get_settings
from doc_intel.core.tenancy import TenantContext
from doc_intel.embeddings.schemas import DENSE_DIM, Embedding, Hit, SparseVector
from doc_intel.ingestion.schemas import ChunkPayload, TableRow
from doc_intel.main import create_app
from doc_intel.vector.qdrant_store import QdrantStore, chunk_id
from tests.conftest import qdrant_api_key

pytestmark = pytest.mark.integration

UNUSED_DENSE, UNUSED_SPARSE = 1000, 99_999  # no seeded chunk uses these dimensions


def emb(dense_at: int, sparse_at: int) -> Embedding:
    """One-hot dense (so cosine picks exactly one chunk) plus a single sparse token."""
    dense = [0.0] * DENSE_DIM
    dense[dense_at] = 1.0
    return Embedding(dense=dense, sparse=SparseVector(indices=[sparse_at], values=[1.0]))


def chunk(document_id: uuid.UUID, index: int, element_type: str = "text") -> ChunkPayload:
    return ChunkPayload(
        document_id=document_id,
        filename="report.pdf",
        page_number=index + 1,
        section_heading="Overview",
        element_type=element_type,
        chunk_index=index,
        text=f"chunk {index}",
    )


def tenant() -> TenantContext:
    return TenantContext(f"user_{uuid.uuid4().hex[:8]}", uuid.uuid4())


async def seed(store, ctx, doc, element_types=("text", "text", "text")) -> list[ChunkPayload]:
    """Chunk i gets dense dim i and sparse token 100 + i."""
    chunks = [chunk(doc, i, t) for i, t in enumerate(element_types)]
    await store.upsert_chunks(ctx, chunks, [emb(i, 100 + i) for i in range(len(chunks))])
    return chunks


@pytest.fixture
async def wrong_dims_collection():
    """A collection left by another embedding setup (8-dim dense vectors)."""
    client = AsyncQdrantClient(url=get_settings().qdrant_url, api_key=qdrant_api_key())
    name = f"doc_chunks_test_wrong_dims_{uuid.uuid4().hex[:8]}"
    await client.create_collection(
        name,
        vectors_config={"dense": m.VectorParams(size=8, distance=m.Distance.COSINE)},
        sparse_vectors_config={"sparse": m.SparseVectorParams(modifier=m.Modifier.IDF)},
    )
    yield client, name
    await client.delete_collection(name)
    await client.close()


async def test_collection_for_another_embedding_setup_is_refused(wrong_dims_collection):
    client, name = wrong_dims_collection
    with pytest.raises(RuntimeError, match="another embedding setup"):
        await QdrantStore(client, name).ensure_collection()


async def test_startup_refuses_a_collection_for_another_embedding_setup(wrong_dims_collection):
    # Swallowing this at startup left only "qdrant unavailable" in the log while every
    # upload and question failed.
    _, name = wrong_dims_collection
    with pytest.raises(RuntimeError, match="another embedding setup"):
        with TestClient(create_app(Settings(qdrant_collection=name))):
            pass


async def test_ensure_collection_is_idempotent_and_configured(vector_store):
    await vector_store.ensure_collection()
    await vector_store.ensure_collection()

    info = await vector_store._client.get_collection(vector_store._collection)
    dense = info.config.params.vectors["dense"]
    assert (dense.size, dense.distance.value) == (DENSE_DIM, "Cosine")
    assert info.config.params.sparse_vectors["sparse"].modifier == "idf"  # BM25 (ADR-011)
    schema = info.payload_schema
    for key in ("user_id", "project_id", "document_id", "element_type"):
        assert schema[key].data_type.value == "keyword"
    assert schema["user_id"].params.is_tenant is True


def test_chunk_id_is_deterministic():
    doc = uuid.uuid4()
    assert chunk_id(doc, 3) == chunk_id(doc, 3)
    assert chunk_id(doc, 3) != chunk_id(doc, 4)
    assert chunk_id(doc, 3).version == 5


async def test_hybrid_search_dense_sparse_and_fused(vector_store):
    ctx, doc = tenant(), uuid.uuid4()
    chunks = await seed(vector_store, ctx, doc)
    ids = [chunk_id(doc, i) for i in range(3)]

    dense_only = await vector_store.hybrid_search(ctx, emb(0, UNUSED_SPARSE))
    assert dense_only[0].chunk_id == ids[0]
    assert dense_only[0] == Hit(
        chunk_id=ids[0], score=dense_only[0].score, **chunks[0].model_dump()
    )
    assert dense_only[0].score > 0

    sparse_only = await vector_store.hybrid_search(ctx, emb(UNUSED_DENSE, 101))
    assert sparse_only[0].chunk_id == ids[1]

    fused = await vector_store.hybrid_search(ctx, emb(0, 102))
    assert {h.chunk_id for h in fused[:2]} == {ids[0], ids[2]}
    assert fused[2].chunk_id == ids[1]
    assert fused[0].score >= fused[1].score > fused[2].score

    assert len(await vector_store.hybrid_search(ctx, emb(0, 102), limit=1)) == 1


async def test_reupsert_is_idempotent(vector_store):
    ctx, doc = tenant(), uuid.uuid4()
    await seed(vector_store, ctx, doc)
    await seed(vector_store, ctx, doc)

    hits = await vector_store.hybrid_search(ctx, emb(0, 100), limit=100)
    assert sorted(h.chunk_id for h in hits) == sorted(chunk_id(doc, i) for i in range(3))


async def test_upsert_rejects_length_mismatch(vector_store):
    ctx, doc = tenant(), uuid.uuid4()
    with pytest.raises(ValueError):
        await vector_store.upsert_chunks(ctx, [chunk(doc, 0)], [])
    with pytest.raises(ValueError):
        await vector_store.upsert_chunks(ctx, [], [emb(0, 100)])
    await vector_store.upsert_chunks(ctx, [], [])  # nothing to write is not an error


async def test_document_ids_and_element_type_filters(vector_store):
    ctx, doc1, doc2 = tenant(), uuid.uuid4(), uuid.uuid4()
    await seed(vector_store, ctx, doc1, ("text", "table"))
    await seed(vector_store, ctx, doc2, ("text",))
    q = emb(0, 100)

    assert {h.document_id for h in await vector_store.hybrid_search(ctx, q)} == {doc1, doc2}
    only_doc2 = await vector_store.hybrid_search(ctx, q, document_ids=[doc2])
    assert [h.chunk_id for h in only_doc2] == [chunk_id(doc2, 0)]
    tables = await vector_store.hybrid_search(ctx, q, element_type="table")
    assert [h.chunk_id for h in tables] == [chunk_id(doc1, 1)]
    assert await vector_store.hybrid_search(ctx, q, document_ids=[doc2], element_type="table") == []
    assert await vector_store.hybrid_search(ctx, q, document_ids=[]) == []


async def test_delete_document_points_removes_only_that_document(vector_store):
    ctx, doc1, doc2 = tenant(), uuid.uuid4(), uuid.uuid4()
    await seed(vector_store, ctx, doc1)
    await seed(vector_store, ctx, doc2)

    await vector_store.delete_document_points(ctx, doc1)

    hits = await vector_store.hybrid_search(ctx, emb(0, 100), limit=100)
    assert {h.document_id for h in hits} == {doc2}
    assert len(hits) == 3
    assert await vector_store.retrieve(ctx, [chunk_id(doc1, i) for i in range(3)]) == []


async def test_retrieve_returns_own_chunks(vector_store):
    ctx, doc = tenant(), uuid.uuid4()
    chunks = await seed(vector_store, ctx, doc)
    ids = [chunk_id(doc, 0), chunk_id(doc, 2)]

    hits = await vector_store.retrieve(ctx, ids)

    expected = [Hit(chunk_id=chunk_id(doc, i), score=0.0, **chunks[i].model_dump()) for i in (0, 2)]
    assert sorted(hits, key=str) == sorted(expected, key=str)
    assert await vector_store.retrieve(ctx, []) == []


@pytest.mark.parametrize(
    "bad",
    [
        TenantContext("", uuid.uuid4()),
        TenantContext("   ", uuid.uuid4()),
        TenantContext(None, uuid.uuid4()),
        TenantContext("user_x", None),
        TenantContext("user_x", ""),
    ],
)
async def test_blank_tenant_is_rejected_by_every_method(vector_store, bad):
    doc = uuid.uuid4()
    with pytest.raises(ValueError):
        await vector_store.upsert_chunks(bad, [chunk(doc, 0)], [emb(0, 100)])
    with pytest.raises(ValueError):
        await vector_store.hybrid_search(bad, emb(0, 100))
    with pytest.raises(ValueError):
        await vector_store.retrieve(bad, [chunk_id(doc, 0)])
    with pytest.raises(ValueError):
        await vector_store.delete_document_points(bad, doc)


async def test_ensure_collection_refuses_a_collection_built_for_other_vectors():
    """Old vectors (e.g. another dense size or no IDF) would silently rank garbage."""
    from qdrant_client import AsyncQdrantClient
    from qdrant_client import models as qm

    from doc_intel.core.config import get_settings
    from doc_intel.vector.qdrant_store import QdrantStore

    client = AsyncQdrantClient(url=get_settings().qdrant_url, api_key=qdrant_api_key())
    name = f"doc_chunks_test_mismatch_{uuid.uuid4().hex[:8]}"
    await client.create_collection(
        name,
        # The old BGE-M3 layout: same dense size, so only the missing IDF gives it away.
        vectors_config={"dense": qm.VectorParams(size=DENSE_DIM, distance=qm.Distance.COSINE)},
        sparse_vectors_config={"sparse": qm.SparseVectorParams()},
    )
    try:
        with pytest.raises(RuntimeError, match="upload each document again"):
            await QdrantStore(client, name).ensure_collection()
        # Search must refuse too, not quietly rank against the old vectors.
        with pytest.raises(RuntimeError, match="upload each document again"):
            await QdrantStore(client, name).hybrid_search(tenant(), emb(0, 0))
    finally:
        await client.delete_collection(name)
        await client.close()


async def test_rows_and_block_ids_round_trip(vector_store):
    ctx, doc = tenant(), uuid.uuid4()
    rows = [TableRow(id="P1-T1-R01", cells={"k": "a"}), TableRow(id="P1-T1-R02", cells={"k": "b"})]
    t = chunk(doc, 0, "table").model_copy(update={"rows": rows})
    x = chunk(doc, 1).model_copy(update={"block_ids": ["P2-B01", "P2-B02"]})
    await vector_store.upsert_chunks(ctx, [t, x], [emb(0, 100), emb(1, 101)])

    got = {
        h.chunk_index: h
        for h in await vector_store.retrieve(ctx, [chunk_id(doc, 0), chunk_id(doc, 1)])
    }
    assert got[0].rows == rows and got[0].block_ids == []
    assert got[1].block_ids == ["P2-B01", "P2-B02"] and got[1].rows is None
    hits = await vector_store.hybrid_search(ctx, emb(0, 100), limit=1)
    assert hits[0].rows == rows


async def test_a_point_without_the_new_keys_loads_with_defaults(vector_store):
    ctx, doc = tenant(), uuid.uuid4()
    await seed(vector_store, ctx, doc)
    pid = str(chunk_id(doc, 0))
    await vector_store._client.overwrite_payload(
        vector_store._collection,
        {
            k: v
            for k, v in (await vector_store._client.retrieve(vector_store._collection, [pid]))[
                0
            ].payload.items()
            if k not in ("rows", "block_ids")
        },
        points=[pid],
    )
    (hit,) = await vector_store.retrieve(ctx, [chunk_id(doc, 0)])
    assert hit.rows is None and hit.block_ids == []


async def test_table_chunks_returns_every_table_chunk_of_the_documents(vector_store):
    ctx, doc, other = tenant(), uuid.uuid4(), uuid.uuid4()
    kinds = ("text", "table", "table") + ("text", "table") * 120  # more than one scroll page
    await seed(vector_store, ctx, doc, kinds)
    await seed(vector_store, ctx, other, ("table", "text"))

    hits = await vector_store.table_chunks(ctx, [doc])

    expected = [i for i, k in enumerate(kinds) if k == "table"]
    assert [h.chunk_index for h in hits] == expected
    assert {h.document_id for h in hits} == {doc} and all(h.element_type == "table" for h in hits)
    assert await vector_store.table_chunks(ctx, []) == []
    both = await vector_store.table_chunks(ctx, [other, doc])
    assert [(h.document_id, h.chunk_index) for h in both] == sorted(
        [(h.document_id, h.chunk_index) for h in both], key=lambda t: (t[0], t[1])
    ) and len(both) == len(expected) + 1
