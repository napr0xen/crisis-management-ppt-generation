"""Hybrid retrieval shared by Q&A and mapping (system-design § 3)."""

import asyncio
from uuid import UUID

from doc_intel.core.tenancy import TenantContext
from doc_intel.embeddings.reranker import BGEReranker
from doc_intel.embeddings.schemas import Embedder, Hit
from doc_intel.vector.qdrant_store import QdrantStore, chunk_id

TOP_N = 8  # hits handed to the LLM


def _inline(s: str) -> str:
    """One line without header delimiters, so metadata cannot mimic a chunk header."""
    return " ".join(s.translate(str.maketrans("[]|", "   ")).split())


def format_hit(hit: Hit) -> str:
    """A chunk as shown to the LLM: header `[chunk_id | filename | page | section]`, then text."""
    page = hit.page_number if hit.page_number is not None else "-"
    section = _inline(hit.section_heading or "") or "-"
    header = f"[{hit.chunk_id} | {_inline(hit.filename)} | p.{page} | {section}]"
    return f"{header}\n{hit.text}"


class Retriever:
    def __init__(
        self, embedder: Embedder, store: QdrantStore, reranker: BGEReranker | None = None
    ) -> None:
        self._embedder = embedder
        self._store = store
        self._reranker = reranker  # None unless RERANKER_ENABLED

    async def search(
        self,
        ctx: TenantContext,
        query: str,
        document_ids: list[UUID] | None = None,
        k: int = 20,
        top_n: int = TOP_N,
    ) -> list[Hit]:
        """Dense+sparse query -> tenant-filtered RRF top `k` -> reranked or fused top `top_n`."""
        embedding = await self._embedder.embed_query(query)
        hits = await self._store.hybrid_search(ctx, embedding, document_ids=document_ids, limit=k)
        if self._reranker is None:
            return hits[:top_n]
        return await asyncio.to_thread(self._reranker.rerank, query, hits, top_n)

    async def with_neighbors(self, ctx: TenantContext, hits: list[Hit], budget: int) -> list[Hit]:
        """`hits` plus the chunks next to them in their document, `budget` chunks at most, in
        reading order (documents by best hit, chunks by index). Every hit's previous chunk (where
        its heading usually is) comes before any next chunk, each pass in rank order."""
        hits = hits[:budget]
        have = {h.chunk_id for h in hits}
        wanted: list[UUID] = []
        for step in (-1, 1):
            for h in hits:
                if h.chunk_index is None:
                    continue
                i = h.chunk_index + step
                cid = chunk_id(h.document_id, i)
                if i >= 0 and cid not in have and len(hits) + len(wanted) < budget:
                    have.add(cid)
                    wanted.append(cid)
        found = await self._store.retrieve(ctx, wanted) if wanted else []
        rank: dict[UUID, int] = {}
        for h in hits:
            rank.setdefault(h.document_id, len(rank))
        order = {h.chunk_id: n for n, h in enumerate(hits)}
        # a point without chunk_index (written before the payload carried it) has no neighbors
        # and goes last, in rank order
        return sorted(
            [*hits, *found],
            key=lambda h: (
                h.chunk_index is None,
                rank.get(h.document_id, len(rank)),
                h.chunk_index or 0,
                order.get(h.chunk_id, 0),
            ),
        )
