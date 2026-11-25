"""Structural chunker (system-design § 2.4): sections -> packed paragraphs, tables -> own chunks.

Guarantee: every chunk satisfies `count_tokens(text) <= max_tokens`.
"""

import re
from collections.abc import Callable
from itertools import groupby
from uuid import UUID

from doc_intel.ingestion.schemas import ChunkPayload, ParsedDocument, ParsedElement

Counter = Callable[[str], int]


def _pack[T](
    items: list[tuple[T, str]], sep: str, max_tokens: int, count: Counter
) -> list[tuple[T, str]]:
    """Greedily join consecutive pieces (each already fits); a merged piece keeps its first tag."""
    out: list[tuple[T, str]] = []
    for tag, text in items:
        if out and count(merged := f"{out[-1][1]}{sep}{text}") <= max_tokens:
            out[-1] = (out[-1][0], merged)
        else:
            out.append((tag, text))
    return out


def _split(text: str, max_tokens: int, count: Counter) -> list[str]:
    """Split on sentence boundaries, then whitespace, then characters, until every piece fits."""
    if count(text) <= max_tokens:
        return [text]
    for boundary in (r"(?<=[.!?])\s+", r"\s+"):
        parts = [p for p in re.split(boundary, text) if p]
        if len(parts) > 1:
            pieces = [(None, s) for p in parts for s in _split(p, max_tokens, count)]
            return [t for _, t in _pack(pieces, " ", max_tokens, count)]
    if len(text) <= 1:
        raise ValueError("max_tokens is too small for a single character")
    mid = len(text) // 2
    return _split(text[:mid], max_tokens, count) + _split(text[mid:], max_tokens, count)


def _md_row(cells: list[str]) -> str:
    return "| " + " | ".join(" ".join(c.split()).replace("|", r"\|") for c in cells) + " |"


def _table_texts(rows: list[list[str]], max_tokens: int, count: Counter) -> list[str]:
    if not rows:
        return []
    head = f"{_md_row(rows[0])}\n|{' --- |' * len(rows[0])}"
    lines = [_md_row(r) for r in rows[1:]]
    full = "\n".join([head, *lines])
    if count(full) <= max_tokens:
        return [full]
    if count(head) * 2 > max_tokens:
        # shortcut: a header using over half the budget is not repeated and the table is split as
        # plain text (rows lose their line breaks); give such tables a per-row layout if they occur.
        return _split(full, max_tokens, count)

    def with_head(t: str) -> int:
        return count(f"{head}\n{t}")

    pieces = [(None, frag) for line in lines for frag in _split(line, max_tokens, with_head)]
    return [f"{head}\n{t}" for _, t in _pack(pieces, "\n", max_tokens, with_head)]


def chunk(
    doc: ParsedDocument,
    *,
    document_id: UUID,
    filename: str,
    max_tokens: int,
    count_tokens: Counter,
) -> list[ChunkPayload]:
    pieces: list[tuple[ParsedElement, str]] = []
    for (kind, _), group in groupby(
        doc.elements, key=lambda e: (e.element_type, e.section_heading)
    ):
        els = list(group)
        if kind == "table":
            pieces += [
                (e, t) for e in els for t in _table_texts(e.rows or [], max_tokens, count_tokens)
            ]
        else:
            parts = [
                (e, p)
                for e in els
                if e.text.strip()
                for p in _split(e.text.strip(), max_tokens, count_tokens)
            ]
            pieces += _pack(parts, "\n\n", max_tokens, count_tokens)
    return [
        ChunkPayload(
            document_id=document_id,
            filename=filename,
            page_number=e.page_number,
            section_heading=e.section_heading,
            element_type=e.element_type,
            chunk_index=i,
            text=t,
        )
        for i, (e, t) in enumerate(pieces)
    ]


def embed_text(chunk: ChunkPayload) -> str:
    """The text that is embedded: filename and section give a bare chunk its context."""
    where = (
        chunk.filename
        if chunk.section_heading is None
        else (f"{chunk.filename} > {chunk.section_heading}")
    )
    return f"{where}\n{chunk.text}"
