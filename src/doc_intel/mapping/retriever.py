"""Hybrid retrieval shared by Q&A and mapping (system-design § 3)."""

import asyncio
from uuid import UUID

from doc_intel.core.tenancy import TenantContext
from doc_intel.embeddings.reranker import BGEReranker
from doc_intel.embeddings.schemas import Embedder, Hit
from doc_intel.vector.qdrant_store import QdrantStore

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
    ) -> list[Hit]:
        """Dense+sparse query -> tenant-filtered RRF top `k` -> reranked or fused top 8."""
        embedding = await self._embedder.embed_query(query)
        hits = await self._store.hybrid_search(ctx, embedding, document_ids=document_ids, limit=k)
        if self._reranker is None:
            return hits[:TOP_N]
        return await asyncio.to_thread(self._reranker.rerank, query, hits, TOP_N)
