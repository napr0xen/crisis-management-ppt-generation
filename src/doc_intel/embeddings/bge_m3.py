"""BGE-M3 dense + sparse embedder via FlagEmbedding (ADR-004)."""

import threading
from typing import TYPE_CHECKING

import numpy as np

from doc_intel.embeddings.schemas import DENSE_DIM, Embedding, SparseVector

if TYPE_CHECKING:
    from FlagEmbedding import BGEM3FlagModel

BATCH_SIZE = 16
MAX_LENGTH = 8192


def resolve_device(device: str) -> str:
    """The configured device if usable here, else `cpu`."""
    import torch

    usable = {"mps": torch.backends.mps.is_available(), "cuda": torch.cuda.is_available()}
    return device if usable.get(device) else "cpu"


def local_snapshot(model_name: str, ignore: list[str]) -> str:
    """Download once into the HF cache, skipping files we never load; return the local path."""
    from huggingface_hub import snapshot_download

    return snapshot_download(model_name, ignore_patterns=ignore)


class BGEM3Embedder:
    """Loads the model on first non-empty `embed()` (memory budget, architecture § 6)."""

    def __init__(self, model_name: str, device: str) -> None:
        self._model_name = model_name
        self._device = device
        self._model: BGEM3FlagModel | None = None
        # FlagEmbedding's encode() mutates model state (device/dtype moves); serialize all calls.
        self._lock = threading.Lock()

    def _load(self) -> "BGEM3FlagModel":
        from FlagEmbedding import BGEM3FlagModel  # torch/transformers import is slow; defer it too

        device = resolve_device(self._device)
        # FlagEmbedding's own download would also pull the 2.3 GB ONNX export we don't use.
        path = local_snapshot(self._model_name, ["onnx/*", "imgs/*", "*.jpg"])
        # A single device string → single-process encode (no multi-process pool).
        return BGEM3FlagModel(path, use_fp16=device != "cpu", devices=device)

    def embed(self, texts: list[str]) -> list[Embedding]:
        if not texts:
            return []
        with self._lock:
            if self._model is None:
                self._model = self._load()
            out = self._model.encode(
                texts,
                batch_size=BATCH_SIZE,
                max_length=MAX_LENGTH,
                return_dense=True,
                return_sparse=True,
                return_colbert_vecs=False,
            )
        return [
            _to_embedding(d, w)
            for d, w in zip(out["dense_vecs"], out["lexical_weights"], strict=True)
        ]


def _to_embedding(dense: np.ndarray, weights: dict[str, float]) -> Embedding:
    vec = np.asarray(dense, dtype=np.float64)
    if vec.shape != (DENSE_DIM,):
        raise ValueError(f"embedding model returned dim {vec.shape}, expected {DENSE_DIM}")
    vec /= np.linalg.norm(vec)  # model output is unit-norm in fp16; re-normalize after upcast
    pairs = sorted((int(i), float(w)) for i, w in weights.items() if w > 0)
    return Embedding(
        dense=vec.tolist(),
        sparse=SparseVector(indices=[i for i, _ in pairs], values=[w for _, w in pairs]),
    )
