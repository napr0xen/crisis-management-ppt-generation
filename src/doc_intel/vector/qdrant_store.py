"""The only module that talks to Qdrant (ADR-004, ADR-009, docs/data-model.md § Qdrant).

Every method takes a `TenantContext` and builds the `user_id` + `project_id` filter itself, so
callers can never pass a raw filter or reach another tenant's points.
"""

from uuid import UUID, uuid5

from qdrant_client import AsyncQdrantClient
from qdrant_client import models as m

from doc_intel.core.tenancy import TenantContext
from doc_intel.embeddings.schemas import DENSE_DIM, Embedding, Hit
from doc_intel.ingestion.schemas import ChunkPayload

# Fixed forever: changing it re-keys every point, so re-ingest would duplicate instead of overwrite.
CHUNK_NAMESPACE = UUID("6f0f5a3e-2b1c-4d7e-9a8b-3c5d7e9f1a2b")
PREFETCH_LIMIT = 40  # per retrieval mode, before RRF (system-design § 3)
# ~26 KB of JSON per BGE-M3 point: 256 keeps each request far below Qdrant's 32 MB default limit.
UPSERT_BATCH = 256
_INDEXED_KEYWORDS = ("project_id", "document_id", "element_type")
_HIT_KEYS = [k for k in Hit.model_fields if k not in ("chunk_id", "score")]


def chunk_id(document_id: UUID, chunk_index: int) -> UUID:
    """Deterministic point id, so re-ingesting a document overwrites its points."""
    return uuid5(CHUNK_NAMESPACE, f"{document_id}:{chunk_index}")


def _tenant_payload(ctx: TenantContext) -> dict[str, str]:
    user_id, project_id = str(ctx.user_id or ""), str(ctx.project_id or "")
    if not user_id.strip() or not project_id.strip():
        raise ValueError("tenant user_id and project_id are required")
    return {"user_id": user_id, "project_id": project_id}


def _match(key: str, value: str) -> m.FieldCondition:
    return m.FieldCondition(key=key, match=m.MatchValue(value=value))


def _tenant_filter(ctx: TenantContext, *conditions: m.Condition) -> m.Filter:
    return m.Filter(must=[*(_match(k, v) for k, v in _tenant_payload(ctx).items()), *conditions])


def _hit(point_id: str | int, payload: dict, score: float) -> Hit:
    return Hit(chunk_id=point_id, score=score, **{k: payload[k] for k in _HIT_KEYS})


class QdrantStore:
    def __init__(self, client: AsyncQdrantClient, collection: str) -> None:
        self._client = client
        self._collection = collection
        self._ready = False  # collection known to exist with its indexes

    @classmethod
    def connect(cls, url: str, collection: str) -> "QdrantStore":
        """Build the client here, so no other module ever imports qdrant_client."""
        return cls(AsyncQdrantClient(url=url), collection)

    async def close(self) -> None:
        await self._client.close()

    async def ensure_collection(self) -> None:
        """Create the collection if missing; (re)creating payload indexes is a no-op in Qdrant.

        Runs once per store: ingest calls it before every job in case startup found Qdrant down.
        """
        if self._ready:
            return
        # shortcut: two processes racing on first start can both see "missing" and one create
        # fails, upgrade to catching the conflict if the API and worker ever start separately.
        if not await self._client.collection_exists(self._collection):
            await self._client.create_collection(
                self._collection,
                vectors_config={
                    "dense": m.VectorParams(size=DENSE_DIM, distance=m.Distance.COSINE)
                },
                sparse_vectors_config={"sparse": m.SparseVectorParams()},  # no IDF modifier
            )
        await self._client.create_payload_index(
            self._collection,
            "user_id",
            m.KeywordIndexParams(type=m.KeywordIndexType.KEYWORD, is_tenant=True),
        )
        for key in _INDEXED_KEYWORDS:
            await self._client.create_payload_index(
                self._collection, key, m.PayloadSchemaType.KEYWORD
            )
        self._ready = True

    async def upsert_chunks(
        self, ctx: TenantContext, chunks: list[ChunkPayload], embeddings: list[Embedding]
    ) -> None:
        tenant = _tenant_payload(ctx)
        points = [
            m.PointStruct(
                id=str(chunk_id(c.document_id, c.chunk_index)),
                vector={
                    "dense": e.dense,
                    "sparse": m.SparseVector(indices=e.sparse.indices, values=e.sparse.values),
                },
                payload={**c.model_dump(mode="json"), **tenant},
            )
            for c, e in zip(chunks, embeddings, strict=True)  # length mismatch -> ValueError
        ]
        for start in range(0, len(points), UPSERT_BATCH):
            # update_filter: an existing point owned by another tenant is never overwritten.
            await self._client.upsert(
                self._collection,
                points[start : start + UPSERT_BATCH],
                wait=True,
                update_filter=_tenant_filter(ctx),
            )

    async def delete_document_points(self, ctx: TenantContext, document_id: UUID) -> None:
        selector = m.FilterSelector(
            filter=_tenant_filter(ctx, _match("document_id", str(document_id)))
        )
        await self._client.delete(self._collection, selector, wait=True)

    async def hybrid_search(
        self,
        ctx: TenantContext,
        query: Embedding,
        *,
        document_ids: list[UUID] | None = None,
        element_type: str | None = None,
        limit: int = 20,
    ) -> list[Hit]:
        """Dense + sparse prefetch, each tenant-filtered, fused with RRF (`Hit.score`)."""
        conditions: list[m.Condition] = []
        if document_ids is not None:
            conditions.append(
                m.FieldCondition(
                    key="document_id", match=m.MatchAny(any=[str(d) for d in document_ids])
                )
            )
        if element_type is not None:
            conditions.append(_match("element_type", element_type))
        flt = _tenant_filter(ctx, *conditions)
        sparse = m.SparseVector(indices=query.sparse.indices, values=query.sparse.values)
        res = await self._client.query_points(
            self._collection,
            prefetch=[
                m.Prefetch(query=query.dense, using="dense", filter=flt, limit=PREFETCH_LIMIT),
                m.Prefetch(query=sparse, using="sparse", filter=flt, limit=PREFETCH_LIMIT),
            ],
            query=m.FusionQuery(fusion=m.Fusion.RRF),
            limit=limit,
            with_payload=True,
        )
        return [_hit(p.id, p.payload, p.score) for p in res.points]

    async def retrieve(self, ctx: TenantContext, chunk_ids: list[UUID]) -> list[Hit]:
        """Fetch by id under the tenant filter: another tenant's id returns nothing."""
        flt = _tenant_filter(ctx, m.HasIdCondition(has_id=[str(i) for i in chunk_ids]))
        if not chunk_ids:
            return []
        records, _ = await self._client.scroll(
            self._collection, scroll_filter=flt, limit=len(chunk_ids), with_payload=True
        )
        return [_hit(r.id, r.payload, 0.0) for r in records]
