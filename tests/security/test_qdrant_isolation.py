"""Qdrant tenant isolation (ADR-009): a tenant never sees or deletes another tenant's points.

Three tenants hold identical text and vectors: (user_a, project_1), (user_a, project_2) and
(user_b, project_1) -- the same `project_id` value under another user.
"""

import uuid
from dataclasses import dataclass

import pytest

from doc_intel.core.tenancy import TenantContext
from doc_intel.embeddings.schemas import DENSE_DIM, Embedding, SparseVector
from doc_intel.ingestion.schemas import ChunkPayload
from doc_intel.vector.qdrant_store import chunk_id

pytestmark = pytest.mark.integration

N_CHUNKS = 3


def emb(i: int) -> Embedding:
    dense = [0.0] * DENSE_DIM
    dense[i] = 1.0
    return Embedding(dense=dense, sparse=SparseVector(indices=[100 + i], values=[1.0]))


QUERY = emb(0)


@dataclass
class Seeded:
    ctx: TenantContext
    document_id: uuid.UUID

    @property
    def ids(self) -> list[uuid.UUID]:
        return [chunk_id(self.document_id, i) for i in range(N_CHUNKS)]


async def seed(store, ctx: TenantContext) -> Seeded:
    doc = uuid.uuid4()
    chunks = [
        ChunkPayload(
            document_id=doc,
            filename="same.pdf",
            page_number=1,
            section_heading="Same",
            element_type="table" if i == 1 else "text",
            chunk_index=i,
            text="identical text",
        )
        for i in range(N_CHUNKS)
    ]
    await store.upsert_chunks(ctx, chunks, [emb(i) for i in range(N_CHUNKS)])
    return Seeded(ctx, doc)


@pytest.fixture
async def tenants(vector_store) -> dict[str, Seeded]:
    user_a, user_b = f"user_a_{uuid.uuid4().hex[:8]}", f"user_b_{uuid.uuid4().hex[:8]}"
    project_1, project_2 = uuid.uuid4(), uuid.uuid4()
    return {
        "a1": await seed(vector_store, TenantContext(user_a, project_1)),
        "a2": await seed(vector_store, TenantContext(user_a, project_2)),
        "b1": await seed(vector_store, TenantContext(user_b, project_1)),
    }


async def visible_ids(store, ctx) -> set[uuid.UUID]:
    return {h.chunk_id for h in await store.hybrid_search(ctx, QUERY, limit=100)}


@pytest.mark.parametrize("caller", ["a1", "a2", "b1"])
async def test_search_sees_only_own_points(vector_store, tenants, caller):
    me = tenants[caller]
    other_docs = [t.document_id for k, t in tenants.items() if k != caller]

    assert await visible_ids(vector_store, me.ctx) == set(me.ids)
    tables = await vector_store.hybrid_search(me.ctx, QUERY, element_type="table")
    assert [h.chunk_id for h in tables] == [me.ids[1]]
    assert await vector_store.hybrid_search(me.ctx, QUERY, document_ids=other_docs) == []
    filtered = await vector_store.hybrid_search(
        me.ctx, QUERY, document_ids=other_docs, element_type="table"
    )
    assert filtered == []
    mixed = await vector_store.hybrid_search(
        me.ctx, QUERY, document_ids=[me.document_id, *other_docs]
    )
    assert {h.chunk_id for h in mixed} == set(me.ids)


@pytest.mark.parametrize("caller", ["a1", "a2", "b1"])
async def test_retrieve_ignores_other_tenants_ids(vector_store, tenants, caller):
    me = tenants[caller]
    other_ids = [i for k, t in tenants.items() if k != caller for i in t.ids]

    assert await vector_store.retrieve(me.ctx, other_ids) == []
    mixed = await vector_store.retrieve(me.ctx, [*other_ids, *me.ids])
    assert {h.chunk_id for h in mixed} == set(me.ids)


@pytest.mark.parametrize("caller", ["a1", "a2", "b1"])
async def test_upsert_cannot_overwrite_other_tenants_points(vector_store, tenants, caller):
    me = tenants[caller]
    others = [t for k, t in tenants.items() if k != caller]

    for other in others:  # same document_id -> same point ids as the other tenant's chunks
        hijack = ChunkPayload(
            document_id=other.document_id,
            filename="evil.pdf",
            page_number=None,
            section_heading=None,
            element_type="text",
            chunk_index=0,
            text="overwritten",
        )
        await vector_store.upsert_chunks(me.ctx, [hijack], [emb(0)])

    assert await visible_ids(vector_store, me.ctx) == set(me.ids)
    for other in others:
        [hit] = await vector_store.retrieve(other.ctx, [other.ids[0]])
        assert (hit.filename, hit.text) == ("same.pdf", "identical text")


@pytest.mark.parametrize("caller", ["a1", "a2", "b1"])
async def test_delete_cannot_remove_other_tenants_points(vector_store, tenants, caller):
    me = tenants[caller]
    others = [t for k, t in tenants.items() if k != caller]

    for other in others:
        await vector_store.delete_document_points(me.ctx, other.document_id)

    for other in others:
        assert await visible_ids(vector_store, other.ctx) == set(other.ids)
    assert await visible_ids(vector_store, me.ctx) == set(me.ids)

    await vector_store.delete_document_points(me.ctx, me.document_id)
    assert await visible_ids(vector_store, me.ctx) == set()
    for other in others:
        assert await visible_ids(vector_store, other.ctx) == set(other.ids)
