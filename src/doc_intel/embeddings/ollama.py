"""Dense embeddings from Ollama's `qwen3-embedding` + local BM25 sparse vectors (ADR-011)."""

from collections import OrderedDict

import httpx

from doc_intel.embeddings import sparse
from doc_intel.embeddings.schemas import Embedding

BATCH_SIZE = 32  # texts per /api/embed request
# Chunks are <= 512 tokens plus the query instruction; a smaller context also shrinks the
# model's memory in Ollama (measured: 0.81 GB at 1024 vs 1.24 GB at the default).
NUM_CTX = 1024
QUERY_INSTRUCTION = (
    "Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: "
)
QUERY_CACHE_SIZE = 512  # repeated queries (one per field, retries) skip the model call
TIMEOUT_S = 120.0  # the first call also loads the model


class EmbeddingError(Exception):
    code = "embed_error"


class OllamaQwen3Embedder:
    """`Embedder` over Ollama `/api/embed`. Only queries get Qwen3's retrieval instruction."""

    def __init__(
        self, client: httpx.AsyncClient, base_url: str, model: str, dimensions: int
    ) -> None:
        self._client = client
        self._url = f"{base_url.rstrip('/')}/api/embed"
        self._model = model
        self._dimensions = dimensions  # Matryoshka truncation, so a larger model keeps 1024
        self._queries: OrderedDict[tuple[str, int, str], Embedding] = OrderedDict()

    async def embed_documents(self, texts: list[str]) -> list[Embedding]:
        dense = await self._dense(texts)
        return [
            Embedding(dense=d, sparse=sparse.encode_document(t))
            for d, t in zip(dense, texts, strict=True)
        ]

    async def embed_query(self, text: str) -> Embedding:
        key = (self._model, self._dimensions, text)
        if key in self._queries:
            self._queries.move_to_end(key)
            return self._queries[key]
        [dense] = await self._dense([QUERY_INSTRUCTION + text])
        # Lexical matching uses only the user's words, never the instruction.
        self._queries[key] = Embedding(dense=dense, sparse=sparse.encode_query(text))
        if len(self._queries) > QUERY_CACHE_SIZE:
            self._queries.popitem(last=False)
        return self._queries[key]

    async def _dense(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start : start + BATCH_SIZE]
            try:
                response = await self._client.post(
                    self._url,
                    json={
                        "model": self._model,
                        "input": batch,
                        "dimensions": self._dimensions,
                        "options": {"num_ctx": NUM_CTX},
                    },
                    timeout=TIMEOUT_S,
                )
                response.raise_for_status()
                got = response.json()["embeddings"]
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                # Type only: the request carried document text.
                raise EmbeddingError(f"Ollama embedding failed ({type(exc).__name__}).") from exc
            if len(got) != len(batch) or any(len(v) != self._dimensions for v in got):
                raise EmbeddingError("Ollama returned embeddings of the wrong shape.")
            vectors += got
        return vectors
