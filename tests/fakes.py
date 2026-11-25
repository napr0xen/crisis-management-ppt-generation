"""Test doubles shared across test files (testing-strategy § 1)."""

import hashlib
import json
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


def slot_values_reply(*slots: tuple | None) -> str:
    """A batch (`slot_values`) reply: slot n is `(value, hit, quote)`, or None for not found."""
    entries = []
    for n, slot in enumerate(slots, start=1):
        quotes = [{"chunk_id": str(slot[1].chunk_id), "quote": slot[2]}] if slot else []
        candidates = [{"value": slot[0], "quotes": quotes}] if slot else []
        entries.append(
            {"n": n, "candidates": candidates, "not_found": slot is None, "incomplete": False}
        )
    return json.dumps({"slots": entries})


class FakeLLM:
    """Scripted `LLMClient`: returns the queued replies in order and records every call."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[list[dict[str, str]], dict | None]] = []
        self.thinks: list[bool] = []

    async def complete(
        self, messages: list[dict[str, str]], json_schema: dict | None = None, think: bool = False
    ) -> str:
        self.calls.append((list(messages), json_schema))
        self.thinks.append(think)
        return self.replies.pop(0)


def stub_field_mapping(monkeypatch, fake) -> None:
    """Replace the engine's per-field mapper with `fake`; a batch maps each of its fields by it."""
    from doc_intel.mapping import engine

    async def batch(ctx, fields, document_ids, retriever, llm, siblings, opening=False):
        return {f.field_id: await fake(ctx, f, document_ids, retriever, llm) for f in fields}

    monkeypatch.setattr(engine, "map_field", fake)
    monkeypatch.setattr(engine, "map_batch", batch)
