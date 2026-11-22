"""Per-field mapping: retrieve -> extract -> verify -> resolve (docs/system-design.md § 7)."""

import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from doc_intel.core.config import get_settings
from doc_intel.core.tenancy import TenantContext
from doc_intel.embeddings.schemas import Hit
from doc_intel.llm.gateway import LLMClient, LLMInvalidOutput
from doc_intel.mapping.extractor import (
    MAX_PRIMARY_HITS,
    bounded_field,
    check_candidates,
    extract,
    extract_rows,
    hit_budget,
)
from doc_intel.mapping.retriever import Retriever
from doc_intel.mapping.schemas import (
    MAX_QUOTE_CHARS,
    Candidate,
    ConflictDetails,
    Evidence,
    FieldMappingResult,
    LLMExtraction,
    LLMQuote,
    LLMRows,
    MappingSummary,
    MappingWarning,
    MissingReason,
    Value,
)
from doc_intel.mapping.verifier import (
    MIN_QUOTE_CHARS,
    TYPED_TYPES,
    canonical_value,
    normalize,
    value_in_quotes,
    verify_quote,
)
from doc_intel.templates.schemas import IRField

_BULLET = re.compile(r"^(?:[-*\u2022]|\d+[.)])\s+")
CONF_TYPED, CONF_TEXT, CONF_PARTIAL = 0.9, 0.8, 0.5


def share_key(field: IRField) -> tuple[str, str, str]:
    """Fields with an equal key are one fact: they share one answer and one edit. A named
    placeholder (`{{x}}`, `[x]`, `<x>`) is a variable, so the same name is the same fact and the
    label is ignored (the IR labels each slide differently). A generic blank ("____", "XXX",
    "TBD") or a tokenless field shares only on an equal label and type. The template context (card
    heading, row label) must be equal too: one token under two headings is two facts."""
    token = field.constraints.token if field.write_mode == "replace_token" else None
    named = token.strip("{}[]<> ") if token and token.startswith(("{{", "[", "<")) else ""
    fact = normalize(named or field.label).replace("_", " ").replace("-", " ")
    context = " ".join(normalize(field.context or "").split())
    return " ".join(fact.split()), field.expected_type, context


_SLIDE = re.compile(r"^pptx:s(\d+):")
_DOCX_TABLE = re.compile(r"^docx:body:t\d+:")


def _unit(field: IRField) -> object:
    """The slide (pptx), sheet (xlsx) or heading section (docx) the field sits in."""
    if m := _SLIDE.match(field.anchor_id):
        return m[1]
    if field.anchor_id.startswith("xlsx:"):
        return field.anchor_id.rsplit("!", 1)[0]
    # shortcut: docx sections come from the LLM-copied section_path, read the AnchorMap path if
    # the model ever rewrites it.
    path = field.section_path[:-1] if _DOCX_TABLE.match(field.anchor_id) else field.section_path
    return tuple(path)  # a table cell's "Table N" is dropped: cards under one heading are slots


def shared_facts(fields: list[IRField]) -> dict[str, tuple[str, str, str]]:
    """field_id -> `share_key` for the fields that share one answer and one edit: copies of one
    key on different slides, sheets or docx sections. A key seen twice in one of them is a
    repeating slot (three risk cards with `[Risk]`), so none of its fields are shared. Fields of a
    row group (`row_groups`) are rows of their own table, never shared."""
    grouped = {f.field_id for g in row_groups(fields) for row in g for f in row}
    fields = [f for f in fields if f.field_id not in grouped]
    keys = {f.field_id: share_key(f) for f in fields}
    units: dict[tuple[str, str, str], list[object]] = {}
    for f in fields:
        units.setdefault(keys[f.field_id], []).append(_unit(f))
    return {
        fid: key
        for fid, key in keys.items()
        if len(units[key]) > 1 and len(set(units[key])) == len(units[key])
    }


_MAX_SIBLINGS, _MAX_HEADING = 20, 120


def sibling_headings(fields: list[IRField]) -> dict[str, list[str]]:
    """field_id -> the other distinct contexts in the field's slide, sheet or section, so the
    context check can reject a value stated under a sibling card's heading."""
    by_unit: dict[object, list[str]] = {}
    for f in fields:
        if f.context and f.context not in by_unit.setdefault(_unit(f), []):
            by_unit[_unit(f)].append(f.context)
    return {
        f.field_id: [h[:_MAX_HEADING] for h in by_unit[_unit(f)] if h != f.context][:_MAX_SIBLINGS]
        for f in fields
        if f.context
    }


_SLOT = re.compile(r"^(.*?\S)[\s_#-]*(?<!\d)(\d{1,3})$")
_CELL = re.compile(r"^(.*):r(\d+)c(\d+)$")


def _slot_grids(fields: list[IRField]) -> list[list[list[IRField]]]:
    """Numbered slot grids: `[Date 1] [Item 1] ... [Date 3] [Item 3]` is 3 rows of 2 columns."""
    slots: dict[object, list[tuple[str, int, IRField]]] = {}
    for f in fields:
        token = f.constraints.token if f.write_mode == "replace_token" else None
        if (
            token
            and token.startswith(("{{", "[", "<"))
            and (m := _SLOT.match(token.strip("{}[]<> ")))
        ):
            stem = " ".join(normalize(m[1]).replace("_", " ").replace("-", " ").split())
            slots.setdefault(_unit(f), []).append((stem, int(m[2]), f))
    groups = []
    for items in slots.values():
        cells: dict[str, dict[int, IRField]] = {}
        repeated = set()
        for stem, n, f in items:  # first appearance orders the columns
            if n in cells.setdefault(stem, {}):
                repeated.add(stem)
            cells[stem][n] = f
        by_numbers: dict[frozenset[int], list[str]] = {}
        for stem, by_n in cells.items():
            if stem not in repeated:
                by_numbers.setdefault(frozenset(by_n), []).append(stem)
        for numbers, stems in by_numbers.items():
            if len(stems) >= 2 and len(numbers) >= 2 and max(numbers) == len(numbers):
                groups.append([[cells[s][n] for s in stems] for n in sorted(numbers)])
    return groups


def _cell_grids(fields: list[IRField]) -> list[list[list[IRField]]]:
    """Native tables whose every body-row cell is a field, so no static label column keys a row."""
    tables: dict[str, dict[int, dict[int, IRField]]] = {}
    for f in fields:
        if f.write_mode in ("fill_cell", "replace_text") and (m := _CELL.match(f.anchor_id)):
            if int(m[2]) >= 1:
                tables.setdefault(m[1], {}).setdefault(int(m[2]), {})[int(m[3])] = f
    groups = []
    for rows in tables.values():
        numbers = sorted(rows)
        width = {frozenset(r) for r in rows.values()}
        if len(rows) >= 2 and numbers == list(range(numbers[0], numbers[0] + len(rows))):
            columns = next(iter(width))
            if len(width) == 1 and columns == frozenset(range(len(columns))) and len(columns) >= 2:
                groups.append([[rows[n][c] for c in sorted(columns)] for n in numbers])
    return groups


def row_groups(fields: list[IRField]) -> list[list[list[IRField]]]:
    """Groups of fields that are the rows of one table: groups -> rows in slot order -> fields in
    column order. Numbered slot grids, native cell grids and `fill_table_rows` fields (one
    one-field group each), by first field in the IR. Any other field is not grouped."""
    groups = [*_slot_grids(fields), *_cell_grids(fields)]
    groups += [[[f]] for f in fields if f.expected_type == "table"]
    order = {f.field_id: i for i, f in enumerate(fields)}
    return sorted(groups, key=lambda g: order[g[0][0].field_id])


def field_query(field: IRField) -> str:
    field = bounded_field(field)  # template-derived text must not size the embedding query
    query = f"{field.label} — {field.description} ({' > '.join(field.section_path)})"
    return f"{field.context}: {query}" if field.context else query


async def _retrieve(
    ctx: TenantContext, query: str, document_ids: list[uuid.UUID], retriever: Retriever
) -> list[Hit]:
    s = get_settings()
    budget = hit_budget(s.llm_num_ctx, s.chunk_max_tokens)
    hits = await retriever.search(
        ctx, query, document_ids=document_ids, top_n=min(budget, MAX_PRIMARY_HITS)
    )
    return await retriever.with_neighbors(ctx, hits, budget) if hits else hits


async def map_field(
    ctx: TenantContext,
    field: IRField,
    document_ids: list[uuid.UUID],
    retriever: Retriever,
    llm: LLMClient,
    other_headings: Sequence[str] = (),
) -> FieldMappingResult:
    if field.expected_type == "table":
        return (await map_group(ctx, [[field]], document_ids, retriever, llm))[field.field_id]
    hits = await _retrieve(ctx, field_query(field), document_ids, retriever)
    if not hits:
        return _missing(field, "no_relevant_chunks", document_ids)
    try:
        extraction = await extract(llm, field, hits)
        extraction = await check_candidates(llm, field, hits, extraction, other_headings)
    except LLMInvalidOutput:  # a transport LLMError propagates: the worker retries
        return _missing(field, "llm_error", document_ids)
    return resolve(field, hits, extraction, checked_documents=document_ids)


def _missing(field: IRField, reason: MissingReason, docs: list[uuid.UUID]) -> FieldMappingResult:
    return FieldMappingResult(
        field_id=field.field_id,
        status="missing",
        value=None,
        evidence=[],
        confidence=0.0,
        missing_reason=reason,
        checked_documents=docs,
        conflict_details=None,
        provenance="rag",
    )


def _convert(raw: Any, expected_type: str) -> tuple[Value, Any] | None:
    """(value, merge key) for a candidate value, or None when it is empty or unusable."""
    if expected_type in TYPED_TYPES:
        canonical = canonical_value(raw, expected_type)
        return None if canonical is None else (canonical, canonical)
    if expected_type == "text":
        text = raw.strip()
        return (text, normalize(text)) if text else None
    if expected_type == "table":
        rows = [[c.strip() for c in row] for row in raw]
        rows = [r for r in rows if any(r)]
        return (rows, tuple(map(tuple, rows))) if rows else None
    items = [i for i in (_BULLET.sub("", x.strip()) for x in raw) if i]
    return (items, tuple(normalize(i) for i in items)) if items else None


def _evidence(hits: list[Hit], quotes: list[LLMQuote]) -> list[Evidence]:
    """The quotes that verify against `hits`, with the hit's metadata."""
    evidence: list[Evidence] = []
    seen: set[tuple[Any, str]] = set()
    for q in quotes:
        if not MIN_QUOTE_CHARS <= len(q.quote) <= MAX_QUOTE_CHARS:
            continue
        hit = verify_quote(hits, q.chunk_id, q.quote)
        if hit is not None and (hit.chunk_id, q.quote) not in seen:
            seen.add((hit.chunk_id, q.quote))
            evidence.append(
                Evidence(
                    document_id=hit.document_id,
                    chunk_id=hit.chunk_id,
                    filename=hit.filename,
                    page_number=hit.page_number,
                    section_heading=hit.section_heading,
                    quote=q.quote,
                )
            )
    return evidence


def _candidates(
    field: IRField, hits: list[Hit], extraction: LLMExtraction[Any]
) -> list[tuple[Value, list[Evidence]]]:
    """Verified, deduplicated candidates as (value, evidence), LLM order kept."""
    merged: dict[Any, tuple[Value, list[Evidence]]] = {}
    for cand in extraction.candidates:
        evidence = _evidence(hits, cand.quotes)
        converted = _convert(cand.value, field.expected_type)
        if not evidence or converted is None:
            continue
        if field.expected_type in TYPED_TYPES and not value_in_quotes(
            cand.value, field.expected_type, [e.quote for e in evidence]
        ):
            continue
        value, key = converted
        known = merged.setdefault(key, (value, []))[1]
        have = {(e.chunk_id, e.quote) for e in known}
        known.extend(e for e in evidence if (e.chunk_id, e.quote) not in have)
    return list(merged.values())


def _extractive(expected_type: str, value: Value, quotes: list[str]) -> bool:
    """Text-like values must be copied from a verified quote, else the LLM may have been steered."""
    if expected_type in TYPED_TYPES:
        return True  # value_in_quotes already proved it
    if isinstance(value, str):
        items = [value]
    elif expected_type == "table":
        items = [cell for row in value for cell in row]
    else:
        items = list(value)
    normalized = [normalize(q) for q in quotes]
    return all(any(normalize(i) in q for q in normalized) for i in items if i)


def _is_partial(field: IRField, value: Value, incomplete: bool) -> bool:
    c = field.constraints
    if incomplete:
        return True
    if c.min_items is not None and isinstance(value, list) and len(value) < c.min_items:
        return True
    return bool(
        field.expected_type == "table"
        and c.columns
        and any(sum(1 for cell in row if cell) < len(c.columns) for row in value)
    )


def resolve(
    field: IRField,
    hits: list[Hit],
    extraction: LLMExtraction[Any],
    checked_documents: list[uuid.UUID],
) -> FieldMappingResult:
    # shortcut: checked_documents is the requested set, it includes a source deleted mid-run.
    if extraction.not_found or not extraction.candidates:
        return _missing(field, "not_found_by_llm", checked_documents)
    found = _candidates(field, hits, extraction)
    if not found:
        return _missing(field, "evidence_failed_verification", checked_documents)
    base: dict[str, Any] = {
        "field_id": field.field_id,
        "checked_documents": checked_documents,
        "provenance": "rag",
    }
    if len(found) > 1:  # never auto-resolve a conflict
        return FieldMappingResult(
            **base,
            status="conflict",
            value=None,
            evidence=[],
            confidence=0.0,
            missing_reason=None,
            conflict_details=ConflictDetails(
                candidates=[Candidate(value=v, evidence=ev) for v, ev in found]
            ),
        )
    value, evidence = found[0]
    partial = _is_partial(field, value, extraction.incomplete) or not _extractive(
        field.expected_type, value, [e.quote for e in evidence]
    )
    typed = field.expected_type in TYPED_TYPES
    return FieldMappingResult(
        **base,
        status="partial" if partial else "populated",
        value=value,
        evidence=evidence,
        confidence=CONF_PARTIAL if partial else (CONF_TYPED if typed else CONF_TEXT),
        missing_reason="insufficient_evidence" if partial else None,
        conflict_details=None,
    )


def _group_query(group: list[list[IRField]]) -> str:
    first = group[0][0]
    if first.expected_type == "table":
        return field_query(first)
    labels = " ".join(bounded_field(f).label for f in group[0])
    query = f"{labels} ({' > '.join(bounded_field(first).section_path)})"
    return f"{bounded_field(first).context}: {query}" if first.context else query


async def map_group(
    ctx: TenantContext,
    group: list[list[IRField]],
    document_ids: list[uuid.UUID],
    retriever: Retriever,
    llm: LLMClient,
) -> dict[str, FieldMappingResult]:
    """One retrieval and one structured call for all rows of a group, verified row by row."""
    fields = [f for row in group for f in row]

    def all_missing(reason: MissingReason) -> dict[str, FieldMappingResult]:
        return {f.field_id: _missing(f, reason, document_ids) for f in fields}

    hits = await _retrieve(ctx, _group_query(group), document_ids, retriever)
    if not hits:
        return all_missing("no_relevant_chunks")
    try:
        rows = await extract_rows(llm, group, hits)
    except LLMInvalidOutput:  # a transport LLMError propagates: the worker retries
        return all_missing("llm_error")
    if rows.not_found or not rows.rows:
        return all_missing("not_found_by_llm")
    if group[0][0].expected_type == "table":
        field = group[0][0]
        return {field.field_id: _table_result(field, hits, rows, document_ids)}
    return _grid_results(group, hits, rows, document_ids)


def _supports(expected_type: str, cell: str, quote: str) -> bool:
    if expected_type in TYPED_TYPES:
        return _convert(cell, expected_type) is not None and value_in_quotes(
            cell, expected_type, [quote]
        )
    return normalize(cell) in normalize(quote)


def _verified_rows(
    types: list[str], hits: list[Hit], rows: LLMRows
) -> list[tuple[list[str], list[Evidence], list[list[bool]], bool]]:
    """Per LLM row: cells, verified evidence, `support[cell][quote]`, and whether ONE quote
    supports every non-empty cell (the row is one source record)."""
    out = []
    for row in rows.rows:
        cells = [c.strip() for c in (row.value + [""] * len(types))[: len(types)]]
        evidence = _evidence(hits, row.quotes)
        support = [
            [bool(c) and _supports(t, c, e.quote) for e in evidence]
            for c, t in zip(cells, types, strict=True)
        ]
        anchored = any(cells) and any(
            all(support[j][i] for j, c in enumerate(cells) if c) for i in range(len(evidence))
        )
        out.append((cells, evidence, support, anchored))
    return out


def _grid_results(
    group: list[list[IRField]], hits: list[Hit], rows: LLMRows, docs: list[uuid.UUID]
) -> dict[str, FieldMappingResult]:
    verified = _verified_rows([f.expected_type for f in group[0]], hits, rows)
    results = {}
    for i, row_fields in enumerate(group):  # LLM row i fills slot i + 1, rows beyond the slots drop
        for j, field in enumerate(row_fields):
            cells, evidence, support, anchored = (
                verified[i] if i < len(verified) else ([], [], [], False)
            )
            cell = cells[j] if cells else ""
            if not cell:
                results[field.field_id] = _missing(field, "not_found_by_llm", docs)
            elif not any(support[j]) or (value := _convert(cell, field.expected_type)) is None:
                results[field.field_id] = _missing(field, "evidence_failed_verification", docs)
            else:
                typed = field.expected_type in TYPED_TYPES
                results[field.field_id] = FieldMappingResult(
                    field_id=field.field_id,
                    status="populated" if anchored else "partial",
                    value=value[0],
                    evidence=evidence,
                    confidence=(CONF_TYPED if typed else CONF_TEXT) if anchored else CONF_PARTIAL,
                    missing_reason=None if anchored else "insufficient_evidence",
                    checked_documents=docs,
                    conflict_details=None,
                    provenance="rag",
                )
    return results


def _table_result(
    field: IRField, hits: list[Hit], rows: LLMRows, docs: list[uuid.UUID]
) -> FieldMappingResult:
    """The rows whose every cell a quote states; unverified rows stay out of the table."""
    types = ["text"] * len(field.constraints.columns or rows.rows[0].value)
    llm_rows = rows.rows[: field.constraints.max_items]
    kept, evidence, all_anchored = [], [], True
    for cells, ev, support, anchored in _verified_rows(
        types, hits, rows.model_copy(update={"rows": llm_rows})
    ):
        if ev and any(cells) and all(not c or any(s) for c, s in zip(cells, support, strict=True)):
            kept.append(cells)
            evidence += [
                e
                for e in ev
                if (e.chunk_id, e.quote) not in {(x.chunk_id, x.quote) for x in evidence}
            ]
            all_anchored &= anchored
    converted = _convert(kept, "table")
    if converted is None:
        return _missing(field, "evidence_failed_verification", docs)
    partial = (
        len(kept) < len(llm_rows) or not all_anchored or _is_partial(field, converted[0], False)
    )
    return FieldMappingResult(
        field_id=field.field_id,
        status="partial" if partial else "populated",
        value=converted[0],
        evidence=evidence,
        confidence=CONF_PARTIAL if partial else CONF_TEXT,
        missing_reason="insufficient_evidence" if partial else None,
        checked_documents=docs,
        conflict_details=None,
        provenance="rag",
    )


async def map_fields(
    ctx: TenantContext,
    fields: list[IRField],
    document_ids: list[uuid.UUID],
    retriever: Retriever,
    llm: LLMClient,
    on_field: Callable[[int, str], Awaitable[None]] | None = None,
) -> dict[str, FieldMappingResult]:
    """Map a whole IR: one call per row group, one per fact shared across slides, one per other
    field. `on_field(done, field_id)` runs before each unit with its first field, and
    `on_field(done, "")` after it. Fields of a group are mapped at the position of the first."""
    groups = {g[0][0].field_id: g for g in row_groups(fields)}
    shared = shared_facts(fields)
    siblings = sibling_headings(fields)
    results: dict[str, FieldMappingResult] = {}
    by_fact: dict[tuple[str, str, str], FieldMappingResult] = {}
    for field in fields:
        if field.field_id in results:
            continue
        if on_field:
            await on_field(len(results), field.field_id)  # template-derived: progress only
        key = shared.get(field.field_id)
        if field.field_id in groups:
            results |= await map_group(ctx, groups[field.field_id], document_ids, retriever, llm)
        elif key and key in by_fact:
            results[field.field_id] = by_fact[key].model_copy(update={"field_id": field.field_id})
        else:
            results[field.field_id] = await map_field(
                ctx, field, document_ids, retriever, llm, siblings.get(field.field_id, ())
            )
            if key:
                by_fact[key] = results[field.field_id]
        if on_field:
            await on_field(len(results), "")
    return {f.field_id: results[f.field_id] for f in fields}


def _length(value: Value | None) -> int | None:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list) and all(isinstance(i, str) for i in value):
        return len("\n".join(value))
    return None  # numbers and tables have no character limit


def summarize(fields: list[IRField], results: dict[str, FieldMappingResult]) -> MappingSummary:
    counts = dict.fromkeys(("populated", "partial", "missing", "conflict"), 0)
    for r in results.values():
        counts[r.status] += 1
    warnings = []
    for f in fields:
        r = results.get(f.field_id)
        length = _length(r.value) if r and r.status in ("populated", "partial") else None
        if length is not None and f.constraints.max_chars and length > f.constraints.max_chars:
            warnings.append(MappingWarning(field_id=f.field_id, code="max_chars_exceeded"))
    return MappingSummary(**counts, warnings=warnings)
