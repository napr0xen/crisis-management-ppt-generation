"""Test doubles shared across test files (testing-strategy § 1)."""

import hashlib
import re
from collections import Counter

import numpy as np

from doc_intel.embeddings.schemas import DENSE_DIM, Embedding, SparseVector

SPARSE_VOCAB = 250_000


def _hash(s: str) -> int:
    return int.from_bytes(hashlib.sha256(s.encode()).digest()[:8], "big")


def _embed(text: str) -> Embedding:
    dense = np.random.default_rng(_hash(text)).standard_normal(DENSE_DIM)
    dense /= np.linalg.norm(dense)
    counts = Counter(_hash(w) % SPARSE_VOCAB for w in re.findall(r"\w+", text.lower()))
    indices = sorted(counts)
    return Embedding(
        dense=dense.tolist(),
        sparse=SparseVector(indices=indices, values=[float(counts[i]) for i in indices]),
    )


class FakeEmbedder:
    """Seeded, model-free `Embedder`: same text → same vectors; shared words → shared sparse ids."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed_documents(self, texts: list[str]) -> list[Embedding]:
        self.calls.append(list(texts))
        return [_embed(t) for t in texts]

    async def embed_query(self, text: str) -> Embedding:
        self.calls.append([text])
        return _embed(text)
