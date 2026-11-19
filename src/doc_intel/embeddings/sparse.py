"""Lexical sparse vectors for Qdrant's `sparse` named vector: BM25 term weights, no ML model.

Qdrant applies IDF itself (`modifier=IDF` on the collection, ADR-011), so documents carry only
the BM25 term-frequency part and queries a unit weight per distinct term.
"""

import re
from collections import Counter
from hashlib import blake2b

from doc_intel.embeddings.schemas import SparseVector

K1, B = 1.2, 0.75  # standard BM25 parameters
# shortcut: fixed BM25 average document length (chunks are <= 512 tokens), compute it per
# collection if lexical ranking quality needs it.
AVG_DOC_TOKENS = 256
_TOKEN = re.compile(r"\w+")  # linear time; Unicode letters and digits


def _index(token: str) -> int:
    """Stable 32-bit id per lowercase token (Qdrant sparse indices are u32)."""
    return int.from_bytes(blake2b(token.encode(), digest_size=4).digest(), "big")


def _counts(text: str) -> Counter[int]:
    return Counter(_index(t) for t in _TOKEN.findall(text.lower()))


def encode_document(text: str) -> SparseVector:
    counts = _counts(text)
    norm = K1 * (1 - B + B * sum(counts.values()) / AVG_DOC_TOKENS)
    indices = sorted(counts)
    return SparseVector(
        indices=indices, values=[counts[i] * (K1 + 1) / (counts[i] + norm) for i in indices]
    )


def encode_query(text: str) -> SparseVector:
    indices = sorted(_counts(text))
    return SparseVector(indices=indices, values=[1.0] * len(indices))
