"""Shared embedding/retrieval contract (Phase 3). Names: system-design § 3, data-model Qdrant."""

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from doc_intel.ingestion.schemas import ElementType, TableRow

DENSE_DIM = 1024  # EMBEDDING_DIMENSIONS default; the Qdrant `dense` vector size


class SparseVector(BaseModel):
    """Lexical term weights (BM25, `embeddings/sparse.py`): hashed token ids and weights."""

    model_config = ConfigDict(extra="forbid")

    indices: list[int]
    values: list[float]


class Embedding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dense: list[float]  # EMBEDDING_DIMENSIONS floats, L2-normalized
    sparse: SparseVector


class Embedder(Protocol):
    async def embed_documents(self, texts: list[str]) -> list[Embedding]:
        """One `Embedding` per chunk text, same order; `[]` for `[]`."""
        ...

    async def embed_query(self, text: str) -> Embedding:
        """A search query (instruction-aware where the model wants one)."""
        ...


class Hit(BaseModel):
    """One retrieved chunk; every field except `score` comes from the Qdrant payload."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: UUID
    document_id: UUID
    filename: str
    page_number: int | None
    section_heading: str | None
    element_type: ElementType
    text: str
    score: float
    chunk_index: int | None = None  # None for a point written before it was in the payload
    block_ids: list[str] = []  # empty for a point written before Phase 23
    rows: list[TableRow] | None = None  # None for a point written before Phase 23
