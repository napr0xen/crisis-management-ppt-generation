import json
import math
import uuid

import httpx
import pytest

from doc_intel.core.config import get_settings
from doc_intel.embeddings import sparse
from doc_intel.embeddings.ollama import (
    BATCH_SIZE,
    NUM_CTX,
    QUERY_INSTRUCTION,
    EmbeddingError,
    OllamaQwen3Embedder,
)
from doc_intel.embeddings.reranker import BGEReranker
from doc_intel.embeddings.schemas import Hit
from tests.fakes import FakeEmbedder

DIM = 8  # small dims keep the mocked responses readable; the real path is in the live test


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


def _ollama(requests: list[dict], status: int = 200, dim: int = DIM):
    """A mocked Ollama /api/embed that records every request body."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/embed"
        body = json.loads(request.content)
        requests.append(body)
        if status != 200:
            return httpx.Response(status, json={"error": "boom"})
        vec = [1.0] + [0.0] * (dim - 1)
        return httpx.Response(200, json={"embeddings": [vec for _ in body["input"]]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _embedder(client: httpx.AsyncClient, dim: int = DIM) -> OllamaQwen3Embedder:
    return OllamaQwen3Embedder(client, "http://ollama:11434", "qwen3-embedding:0.6b", dim)


# OllamaQwen3Embedder (mocked /api/embed)


async def test_documents_are_embedded_in_batches_with_model_dimensions_and_ctx():
    requests: list[dict] = []
    texts = [f"chunk {i}" for i in range(BATCH_SIZE * 2 + 5)]
    out = await _embedder(_ollama(requests)).embed_documents(texts)
    assert [len(r["input"]) for r in requests] == [BATCH_SIZE, BATCH_SIZE, 5]
    assert all(r["model"] == "qwen3-embedding:0.6b" for r in requests)
    assert all(r["dimensions"] == DIM for r in requests)
    assert all(r["options"] == {"num_ctx": NUM_CTX} for r in requests)
    assert [i for r in requests for i in r["input"]] == texts  # raw text, order kept
    assert len(out) == len(texts) and all(len(e.dense) == DIM for e in out)


async def test_empty_document_list_makes_no_request():
    requests: list[dict] = []
    assert await _embedder(_ollama(requests)).embed_documents([]) == []
    assert requests == []


async def test_only_queries_get_the_retrieval_instruction():
    requests: list[dict] = []
    embedder = _embedder(_ollama(requests))
    query = await embedder.embed_query("How many customers lost power?")
    await embedder.embed_documents(["How many customers lost power?"])
    assert requests[0]["input"] == [QUERY_INSTRUCTION + "How many customers lost power?"]
    assert QUERY_INSTRUCTION == (
        "Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: "
    )
    assert requests[1]["input"] == ["How many customers lost power?"]
    # The sparse side never sees the instruction: only the user's words count lexically.
    assert query.sparse == sparse.encode_query("How many customers lost power?")


async def test_documents_get_bm25_sparse_vectors():
    requests: list[dict] = []
    [doc] = await _embedder(_ollama(requests)).embed_documents(["flood flood response"])
    assert doc.sparse == sparse.encode_document("flood flood response")


@pytest.mark.parametrize("status", [404, 500])
async def test_http_errors_raise_embed_error(status):
    with pytest.raises(EmbeddingError) as exc:
        await _embedder(_ollama([], status=status)).embed_documents(["x"])
    assert exc.value.code == "embed_error"


async def test_unreachable_ollama_raises_embed_error():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    with pytest.raises(EmbeddingError):
        await _embedder(client).embed_query("x")


async def test_wrong_dimension_from_ollama_raises_embed_error():
    with pytest.raises(EmbeddingError):
        await _embedder(_ollama([], dim=DIM + 1), dim=DIM).embed_documents(["x"])


async def test_error_messages_never_echo_the_text():
    with pytest.raises(EmbeddingError) as exc:
        await _embedder(_ollama([], status=500)).embed_documents(["SENTINEL_secret_text"])
    assert "SENTINEL" not in str(exc.value)


# BM25 sparse encoder (no model)


def test_sparse_is_deterministic_sorted_and_positive():
    a = sparse.encode_document("The river crested at 7.2 metres; the river flooded.")
    assert a == sparse.encode_document("The river crested at 7.2 metres; the river flooded.")
    assert a.indices == sorted(a.indices) and len(set(a.indices)) == len(a.indices)
    assert all(v > 0 for v in a.values)
    assert all(0 <= i < 2**32 for i in a.indices)  # Qdrant sparse indices are u32


def test_sparse_tokens_are_case_insensitive_and_shared_between_query_and_document():
    doc = sparse.encode_document("Power OUTAGE downtown")
    query = sparse.encode_query("outage")
    assert set(query.indices) <= set(doc.indices)


def test_repeated_terms_saturate_bm25_style():
    once = dict(zip(*sparse.encode_document("flood").model_dump().values(), strict=True))
    many = dict(zip(*sparse.encode_document("flood " * 50).model_dump().values(), strict=True))
    [(i, w1)] = once.items()
    assert w1 < many[i] < (sparse.K1 + 1)  # grows with tf but saturates below k1 + 1


def test_query_terms_have_unit_weight_and_no_text_means_empty_vector():
    q = sparse.encode_query("flood flood shelter")
    assert len(q.indices) == 2 and q.values == [1.0, 1.0]
    assert sparse.encode_document("... --- !!!").indices == []


def test_sparse_tokenizing_is_linear_time():
    import time

    start = time.perf_counter()
    sparse.encode_document("a" * 2_000_000 + " " + "b " * 500_000)
    assert time.perf_counter() - start < 2.0


# FakeEmbedder


async def test_fake_embedder_matches_the_protocol_and_records_calls():
    fake = FakeEmbedder()
    docs = await fake.embed_documents(["flood response", "flood response"])
    query = await fake.embed_query("flood")
    assert docs[0] == docs[1] and len(docs[0].dense) == 1024
    assert _norm(docs[0].dense) == pytest.approx(1.0)
    assert set(query.sparse.indices) <= set(docs[0].sparse.indices)
    assert fake.calls == [["flood response", "flood response"], ["flood"]]
    assert await fake.embed_documents([]) == []


# BGEReranker (no model)


class _FakeScorer:
    def compute_score(self, pairs: list[tuple[str, str]]) -> list[float]:
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


# Real models (Ollama for embeddings; reranker downloads once into the HF cache)


@pytest.mark.slow
@pytest.mark.llm
async def test_qwen3_embedding_live_via_ollama():
    settings = get_settings()
    async with httpx.AsyncClient() as client:
        embedder = OllamaQwen3Embedder(
            client,
            settings.embedding_base_url,
            settings.embedding_model,
            settings.embedding_dimensions,
        )
        a, b, c = await embedder.embed_documents(
            [
                "The quarterly budget was approved by the board.",
                "The board signed off on the budget for the quarter.",
                "Penguins live in the southern hemisphere.",
            ]
        )
        q = await embedder.embed_query("What budget did the board approve?")
    for e in (a, b, c, q):
        assert len(e.dense) == settings.embedding_dimensions == 1024
        assert _norm(e.dense) == pytest.approx(1.0, abs=1e-3)
    assert _cos(a.dense, b.dense) > _cos(a.dense, c.dense)
    assert _cos(q.dense, a.dense) > _cos(q.dense, c.dense)


@pytest.mark.slow
def test_reranker_real_model() -> None:
    settings = get_settings()
    reranker = BGEReranker(settings.reranker_model, settings.reranker_device)
    hits = [
        _hit("Penguins live in the southern hemisphere."),
        _hit("The board approved the quarterly budget of 12,000 EUR."),
        _hit("Office hours are 9 to 5."),
    ]
    out = reranker.rerank("What budget did the board approve?", hits, top_k=2)
    assert len(out) == 2
    assert out[0].text == hits[1].text
    assert out[0].score >= out[1].score
