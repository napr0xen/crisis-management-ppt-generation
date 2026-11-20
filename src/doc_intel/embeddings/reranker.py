"""Optional cross-encoder reranker (ADR-011); constructed only when `RERANKER_ENABLED`."""

import threading

from doc_intel.embeddings.schemas import Hit

MAX_LENGTH = 512  # query + chunk tokens per pair


def resolve_device(device: str) -> str:
    """The configured device if usable here, else `cpu`."""
    import torch

    usable = {"mps": torch.backends.mps.is_available(), "cuda": torch.cuda.is_available()}
    return device if usable.get(device) else "cpu"


class _CrossEncoder:
    """`compute_score(pairs)` -> sigmoid relevance in 0..1, via plain transformers."""

    def __init__(self, model_name: str, device: str) -> None:
        import torch
        from huggingface_hub import snapshot_download
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        # Skip the repo's images and any ONNX export we never load.
        path = snapshot_download(model_name, ignore_patterns=["onnx/*", "assets/*", "*.jpg"])
        self._torch, self._device = torch, device
        self._tokenizer = AutoTokenizer.from_pretrained(path)
        dtype = torch.float32 if device == "cpu" else torch.float16
        model = AutoModelForSequenceClassification.from_pretrained(path, dtype=dtype)
        self._model = model.to(device).eval()

    def compute_score(self, pairs: list[tuple[str, str]]) -> list[float]:
        with self._torch.no_grad():
            inputs = self._tokenizer(
                [q for q, _ in pairs],
                [p for _, p in pairs],
                padding=True,
                truncation=True,
                max_length=MAX_LENGTH,
                return_tensors="pt",
            ).to(self._device)
            logits = self._model(**inputs).logits.view(-1).float()
            return self._torch.sigmoid(logits).tolist()


class BGEReranker:
    """Loads the model on first non-empty `rerank()`.

    shortcut: stays resident once loaded (+1.5 GB), release when idle if Phase 12 memory needs it.
    """

    def __init__(self, model_name: str, device: str) -> None:
        self._model_name = model_name
        self._device = device
        self._model: _CrossEncoder | None = None
        self._lock = threading.Lock()

    def _load(self) -> _CrossEncoder:
        return _CrossEncoder(self._model_name, resolve_device(self._device))

    def rerank(self, query: str, hits: list[Hit], top_k: int) -> list[Hit]:
        """The `top_k` hits by reranker score (sigmoid, 0..1), best first; `score` is replaced."""
        if not hits:
            return []
        with self._lock:
            if self._model is None:
                self._model = self._load()
            scores = self._model.compute_score([(query, h.text) for h in hits])
        ranked = sorted(zip(scores, hits, strict=True), key=lambda p: p[0], reverse=True)
        return [h.model_copy(update={"score": float(s)}) for s, h in ranked[:top_k]]
