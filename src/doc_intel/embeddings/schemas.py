"""Shared embedding/retrieval contract (Phase 3). Names: system-design § 3, data-model Qdrant."""

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from doc_intel.ingestion.schemas import ElementType

DENSE_DIM = 1024  # BAAI/bge-m3


class SparseVector(BaseModel):
    """BGE-M3 learned lexical weights: token ids and their weights."""

    model_config = ConfigDict(extra="forbid")

    indices: list[int]
    values: list[float]


class Embedding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dense: list[float]  # DENSE_DIM floats, L2-normalized
    sparse: SparseVector


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[Embedding]:
        """One `Embedding` per text, same order; `[]` for `[]`. Sync: call via asyncio.to_thread."""
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
