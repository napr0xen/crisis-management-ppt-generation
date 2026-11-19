import math
import uuid
from typing import Any

import numpy as np
import pytest

from doc_intel.core.config import get_settings
from doc_intel.embeddings.bge_m3 import BGEM3Embedder
from doc_intel.embeddings.reranker import BGEReranker
from doc_intel.embeddings.schemas import DENSE_DIM, Hit
from tests.fakes import FakeEmbedder


def _norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _hit(text: str) -> Hit:
    return Hit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="a.pdf",
        page_number=1,
        section_heading=None,
        element_type="text",
        text=text,
        score=0.5,
    )


# FakeEmbedder


def test_fake_shapes_normalized_and_deterministic() -> None:
    fake = FakeEmbedder()
    a, b, a2 = fake.embed(["hello world", "other text", "hello world"])
    assert len(a.dense) == DENSE_DIM
    assert _norm(a.dense) == pytest.approx(1.0)
    assert a == a2
    assert a.dense != b.dense
    assert a.sparse.indices == sorted(a.sparse.indices)
    assert fake.calls == [["hello world", "other text", "hello world"]]


def test_fake_empty_input() -> None:
    assert FakeEmbedder().embed([]) == []


def test_fake_sparse_counts_and_shared_words_overlap() -> None:
    fake = FakeEmbedder()
    a, b, c = fake.embed(["Budget budget plan", "the budget", "unrelated words"])
    assert len(a.sparse.indices) == 2
    assert sorted(a.sparse.values) == [1.0, 2.0]
    assert set(a.sparse.indices) & set(b.sparse.indices)
    assert not set(a.sparse.indices) & set(c.sparse.indices)


# BGEM3Embedder (no model)


class _FakeM3:
    def __init__(self) -> None:
        self.calls = 0

    def encode(self, sentences: list[str], **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        assert kwargs["batch_size"] == 16
        assert kwargs["return_dense"] and kwargs["return_sparse"]
        dense = np.zeros((len(sentences), DENSE_DIM), dtype=np.float16)
        dense[:, 0] = 3.0
        dense[:, 1] = 4.0
        return {
            "dense_vecs": dense,
            "lexical_weights": [{"42": np.float16(0.5), "7": 0.25, "9": 0.0} for _ in sentences],
        }


def test_bge_m3_is_lazy_and_empty_input_does_not_load(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: BGEM3Embedder) -> None:
        raise AssertionError("model loaded")

    monkeypatch.setattr(BGEM3Embedder, "_load", boom)
    embedder = BGEM3Embedder("BAAI/bge-m3", "cpu")
    assert embedder.embed([]) == []


def test_bge_m3_converts_output_and_loads_once(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _FakeM3()
    loads: list[int] = []

    def load(self: BGEM3Embedder) -> _FakeM3:
        loads.append(1)
        return model

    monkeypatch.setattr(BGEM3Embedder, "_load", load)
    embedder = BGEM3Embedder("BAAI/bge-m3", "cpu")
    [e] = embedder.embed(["x"])
    embedder.embed(["y", "z"])
    assert len(loads) == 1 and model.calls == 2
    assert e.dense[:2] == pytest.approx([0.6, 0.8])
    assert e.sparse.indices == [7, 42]
    assert e.sparse.values == [0.25, 0.5]


# BGEReranker (no model)


class _FakeScorer:
    def compute_score(self, pairs: list[tuple[str, str]], **kwargs: Any) -> list[float]:
        return [float(len(passage)) for _, passage in pairs]


def test_reranker_orders_by_score_and_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(BGEReranker, "_load", lambda self: _FakeScorer())
    hits = [_hit("bb"), _hit("dddd"), _hit("a"), _hit("ccc")]
    out = BGEReranker("BAAI/bge-reranker-v2-m3", "cpu").rerank("q", hits, top_k=2)
    assert [h.text for h in out] == ["dddd", "ccc"]
    assert [h.score for h in out] == [4.0, 3.0]
    assert out[0].chunk_id == hits[1].chunk_id


def test_reranker_empty_does_not_load(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: BGEReranker) -> None:
        raise AssertionError("model loaded")

    monkeypatch.setattr(BGEReranker, "_load", boom)
    assert BGEReranker("BAAI/bge-reranker-v2-m3", "cpu").rerank("q", [], top_k=8) == []


# Real models (download once into the HF cache)


@pytest.mark.slow
def test_bge_m3_real_model() -> None:
    settings = get_settings()
    embedder = BGEM3Embedder(settings.embedding_model, settings.embedding_device)
    texts = [
        "The quarterly budget was approved by the board.",
        "The board signed off on the budget for the quarter.",
        "Penguins live in the southern hemisphere.",
    ]
    a, b, c = embedder.embed(texts)
    for e in (a, b, c):
        assert len(e.dense) == DENSE_DIM
        assert _norm(e.dense) == pytest.approx(1.0, abs=1e-3)
        assert e.sparse.indices and all(v > 0 for v in e.sparse.values)
        assert e.sparse.indices == sorted(e.sparse.indices)
    assert _cos(a.dense, b.dense) > _cos(a.dense, c.dense)


@pytest.mark.slow
def test_reranker_real_model() -> None:
    settings = get_settings()
    reranker = BGEReranker(settings.reranker_model, settings.embedding_device)
    hits = [
        _hit("Penguins live in the southern hemisphere."),
        _hit("The board approved the quarterly budget of 12,000 EUR."),
        _hit("Office hours are 9 to 5."),
    ]
    out = reranker.rerank("What budget did the board approve?", hits, top_k=2)
    assert len(out) == 2
    assert out[0].text == hits[1].text
    assert out[0].score >= out[1].score
