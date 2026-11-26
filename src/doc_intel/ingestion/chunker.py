"""Structural chunker (system-design § 2.4): sections -> packed paragraphs, tables -> own chunks.

Guarantee: every chunk satisfies `count_tokens(text) <= max_tokens`.
"""

import re
from collections.abc import Callable
from itertools import groupby
from uuid import UUID

from doc_intel.ingestion.schemas import ChunkPayload, ParsedDocument, ParsedElement, TableRow

Counter = Callable[[str], int]


def _pack[T](
    items: list[tuple[T, str]],
    sep: str,
    max_tokens: int,
    count: Counter,
    join: Callable[[T, T], T] = lambda a, b: a,
) -> list[tuple[T, str]]:
    """Greedily join consecutive pieces (each already fits); a merged piece's tag is `join`ed
    (default: the first tag)."""
    out: list[tuple[T, str]] = []
    for tag, text in items:
        if out and count(merged := f"{out[-1][1]}{sep}{text}") <= max_tokens:
            out[-1] = (join(out[-1][0], tag), merged)
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


def _table_texts(
    rows: list[list[str]], max_tokens: int, count: Counter
) -> list[tuple[tuple[int, ...] | None, str]]:
    """(data-row indexes whose lines are in the chunk, chunk text); None when not line-aligned."""
    if not rows:
        return []
    head = f"{_md_row(rows[0])}\n|{' --- |' * len(rows[0])}"
    lines = [_md_row(r) for r in rows[1:]]
    full = "\n".join([head, *lines])
    if count(full) <= max_tokens:
        return [(tuple(range(len(lines))), full)]
    if count(head) * 2 > max_tokens:
        # shortcut: a header using over half the budget is not repeated and the table is split as
        # plain text (rows lose their line breaks, no row ids); give such tables a per-row layout.
        return [(None, t) for t in _split(full, max_tokens, count)]

    def with_head(t: str) -> int:
        return count(f"{head}\n{t}")

    pieces: list[tuple[tuple[int, ...], str]] = []
    for i, line in enumerate(lines):
        frags = _split(line, max_tokens, with_head)
        # shortcut: a row split over several chunks gets no row ids (-1), rarely needed.
        pieces += [((i,) if len(frags) == 1 else (-1,), frag) for frag in frags]
    packed = _pack(pieces, "\n", max_tokens, with_head, lambda a, b: a + b)
    return [(None if -1 in idx else idx, f"{head}\n{t}") for idx, t in packed]


def _provenance(elements: list[ParsedElement]) -> list[str]:
    """Per element, in document order: `P<page>-T<n>` for a table, `P<page>-B<nn>` for a text
    block (counted per page; the `P<page>-` part is dropped when there is no page)."""
    counts: dict[tuple[int | None, str], int] = {}
    ids = []
    for e in elements:
        kind = "T" if e.element_type == "table" else "B"
        n = counts[e.page_number, kind] = counts.get((e.page_number, kind), 0) + 1
        page = "" if e.page_number is None else f"P{e.page_number}-"
        ids.append(f"{page}{kind}{n}" if kind == "T" else f"{page}B{n:02d}")
    return ids


def _table_rows(header: list[str], data: list[list[str]], table_id: str, idx: tuple[int, ...]):
    keys = [" ".join(h.split()) for h in header]
    keys = [k if k and keys.index(k) == j else f"col{j + 1}" for j, k in enumerate(keys)]
    return [
        TableRow(
            id=f"{table_id}-R{i + 1:02d}",
            cells={
                (keys[j] if j < len(keys) else f"col{j + 1}"): " ".join(c.split())
                for j, c in enumerate(data[i])
            },
        )
        for i in idx
    ]


def chunk(
    doc: ParsedDocument,
    *,
    document_id: UUID,
    filename: str,
    max_tokens: int,
    count_tokens: Counter,
) -> list[ChunkPayload]:
    prov = dict(zip(map(id, doc.elements), _provenance(doc.elements), strict=True))
    # (element, block ids, table rows, text) per chunk
    pieces: list[tuple[ParsedElement, list[str], list[TableRow] | None, str]] = []
    for (kind, _), group in groupby(
        doc.elements, key=lambda e: (e.element_type, e.section_heading)
    ):
        els = list(group)
        if kind == "table":
            for e in els:
                rows = e.rows or []
                for idx, t in _table_texts(rows, max_tokens, count_tokens):
                    shown = (
                        None if idx is None else _table_rows(rows[0], rows[1:], prov[id(e)], idx)
                    )
                    pieces.append((e, [], shown, t))
        else:
            parts = [
                ((e, (prov[id(e)],)), p)
                for e in els
                if e.text.strip()
                for p in _split(e.text.strip(), max_tokens, count_tokens)
            ]
            packed = _pack(
                parts,
                "\n\n",
                max_tokens,
                count_tokens,
                lambda a, b: (a[0], a[1] + tuple(i for i in b[1] if i not in a[1])),
            )
            pieces += [(e, list(ids), None, t) for (e, ids), t in packed]
    return [
        ChunkPayload(
            document_id=document_id,
            filename=filename,
            page_number=e.page_number,
            section_heading=e.section_heading,
            element_type=e.element_type,
            chunk_index=i,
            text=t,
            block_ids=ids,
            rows=rows,
        )
        for i, (e, ids, rows, t) in enumerate(pieces)
    ]


def embed_text(chunk: ChunkPayload) -> str:
    """The text that is embedded: filename and section give a bare chunk its context."""
    where = (
        chunk.filename
        if chunk.section_heading is None
        else (f"{chunk.filename} > {chunk.section_heading}")
    )
    return f"{where}\n{chunk.text}"
