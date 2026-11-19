"""Optional BGE cross-encoder reranker (ADR-004); constructed only when `RERANKER_ENABLED`."""

import threading
from typing import TYPE_CHECKING

from doc_intel.embeddings.bge_m3 import local_snapshot, resolve_device
from doc_intel.embeddings.schemas import Hit

if TYPE_CHECKING:
    from FlagEmbedding import FlagReranker


class BGEReranker:
    """Loads the model on first non-empty `rerank()`.

    shortcut: stays resident once loaded (+1.5 GB), release when idle if Phase 10 memory needs it.
    """

    def __init__(self, model_name: str, device: str) -> None:
        self._model_name = model_name
        self._device = device
        self._model: FlagReranker | None = None
        self._lock = threading.Lock()

    def _load(self) -> "FlagReranker":
        from FlagEmbedding import FlagReranker

        device = resolve_device(self._device)
        path = local_snapshot(self._model_name, ["assets/*"])
        return FlagReranker(path, use_fp16=device != "cpu", devices=device)

    def rerank(self, query: str, hits: list[Hit], top_k: int) -> list[Hit]:
        """The `top_k` hits by reranker score (sigmoid, 0..1), best first; `score` is replaced."""
        if not hits:
            return []
        with self._lock:
            if self._model is None:
                self._model = self._load()
            scores = self._model.compute_score([(query, h.text) for h in hits], normalize=True)
        ranked = sorted(zip(scores, hits, strict=True), key=lambda p: p[0], reverse=True)
        return [h.model_copy(update={"score": float(s)}) for s, h in ranked[:top_k]]
