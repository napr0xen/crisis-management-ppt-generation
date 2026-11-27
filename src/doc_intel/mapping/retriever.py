"""Hybrid retrieval shared by Q&A and mapping (system-design § 3)."""

import asyncio
import re
from collections.abc import Callable
from uuid import UUID

import structlog

from doc_intel.core.config import get_settings
from doc_intel.core.tenancy import TenantContext
from doc_intel.embeddings.reranker import BGEReranker
from doc_intel.embeddings.schemas import Embedder, Hit
from doc_intel.ingestion.pipeline import count_tokens
from doc_intel.mapping.derived import row_lines
from doc_intel.vector.qdrant_store import QdrantStore, chunk_id

log = structlog.get_logger()
TOP_N = 8  # hits handed to the LLM


def _inline(s: str) -> str:
    """One line without header delimiters, so metadata cannot mimic a chunk header."""
    return " ".join(s.translate(str.maketrans("[]|", "   ")).split())


def format_hit(hit: Hit) -> str:
    """A chunk as shown to the LLM: header `[chunk_id | filename | page | section]`, then text."""
    page = hit.page_number if hit.page_number is not None else "-"
    section = _inline(hit.section_heading or "") or "-"
    header = f"[{hit.chunk_id} | {_inline(hit.filename)} | p.{page} | {section}]"
    lines = hit.text.split("\n")
    if by_id := row_lines(hit):
        lines[2:] = [f"[{i}] {ln}" for i, ln in by_id.items()]
    return f"{header}\n" + "\n".join(lines)


_WORDS = re.compile(r"\w+")
NEAR_DUPLICATE = 0.9  # Jaccard overlap of the word sets


def curate(
    hits: list[Hit], budget_tokens: int, count: Callable[[str], int], pinned: int = 0
) -> tuple[list[Hit], int]:
    """`hits` in their order minus near-duplicates, within `budget_tokens` of formatted chunks, and
    the kept chunks' token total. The first `pinned` hits (a source's opening chunks) claim the
    budget first, then scored hits in order; 0.0-score chunks (unscored neighbors) are left out,
    unless no hit has a score, so the context is never empty. Each pass stops at the first chunk
    that does not fit, and the first claimed chunk is always kept."""
    unique: list[tuple[int, Hit]] = []
    seen: list[set[str]] = []
    for i, h in enumerate(hits):
        words = set(_WORDS.findall(h.text.lower()))
        if any(len(words & o) / len(words | o) >= NEAR_DUPLICATE for o in seen if words | o):
            continue
        unique.append((i, h))
        seen.append(words)
    keep: set[int] = set()
    used = 0
    scored = any(h.score != 0.0 for i, h in unique if i >= pinned)
    for tier in range(2):
        for i, h in unique:
            if (0 if i < pinned else 1 if h.score != 0.0 or not scored else 2) != tier:
                continue
            tokens = count(format_hit(h))
            if keep and used + tokens > budget_tokens:
                break
            used += tokens
            keep.add(i)
    return [h for i, h in unique if i in keep], used


def select_context(hits: list[Hit], budget_tokens: int | None = None, pinned: int = 0) -> list[Hit]:
    """`curate` with the configured token budget (or `budget_tokens`); logs ids, scores and counts
    only."""
    s = get_settings()
    kept, used = curate(
        hits,
        budget_tokens or s.retrieval_context_tokens,
        lambda t: count_tokens(t, s.chunk_tokenizer),
        pinned,
    )
    log.info(
        "retrieval",
        chunk_ids=[str(h.chunk_id) for h in kept],
        scores=[round(h.score, 4) for h in kept],
        context_tokens=used,
        hits=len(kept),
    )
    return kept


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

    async def table_chunks(self, ctx: TenantContext, document_ids: list[UUID]) -> list[Hit]:
        """Every table chunk of the documents (derived slots read whole tables, not top-k)."""
        return await self._store.table_chunks(ctx, document_ids)

    async def opening(self, ctx: TenantContext, document_ids: list[UUID], n: int = 2) -> list[Hit]:
        """The first `n` chunks of each document (title, author, date usually sit there), documents
        in the given order, chunks by index."""
        found = await self._store.retrieve(
            ctx, [chunk_id(d, i) for d in document_ids for i in range(n)]
        )
        rank = {d: k for k, d in enumerate(document_ids)}
        return sorted(found, key=lambda h: (rank.get(h.document_id, len(rank)), h.chunk_index or 0))
