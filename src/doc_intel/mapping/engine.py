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
    MAX_CHART_POINTS,
    MAX_PRIMARY_HITS,
    bounded_field,
    check_candidates,
    extract,
    extract_records,
    extract_rows,
    hit_budget,
    synthesize,
)
from doc_intel.mapping.retriever import Retriever
from doc_intel.mapping.schemas import (
    MAX_QUOTE_CHARS,
    Candidate,
    ConflictDetails,
    Evidence,
    FieldMappingResult,
    LLMCandidate,
    LLMExtraction,
    LLMQuote,
    LLMRecord,
    LLMRecords,
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
    supported,
    value_in_quotes,
    verify_quote,
)
from doc_intel.templates.anchors import find_tokens, has_letter
from doc_intel.templates.schemas import Constraints, IRField

_BULLET = re.compile(r"^(?:[-*\u2022]|\d+[.)])\s+")
CONF_TYPED, CONF_TEXT, CONF_SYNTH, CONF_PARTIAL = 0.9, 0.8, 0.7, 0.5
_LISTS = ("paragraphs", "bullet_list")
_ROW_TYPES = ("table", "chart")  # fields mapped by one structured row call


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
    """Numbered slot grids: `[Date 1] [Item 1] ... [Date 3] [Item 3]` is 3 rows of 2 columns. A
    single stem numbered 1..N (`[Key message 1] [Key message 2]`) is a list: N rows of one field,
    when the stem names a fact (has a letter) and the slots share one heading (or none)."""
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
            if len(numbers) < 2 or max(numbers) != len(numbers):
                continue
            if len(stems) >= 2:
                groups.append([[cells[s][n] for s in stems] for n in sorted(numbers)])
            elif has_letter(stems[0]):
                slots_1_n = [cells[stems[0]][n] for n in sorted(numbers)]
                if len({f.context for f in slots_1_n if has_letter(f.context)}) <= 1:
                    groups.append([[f] for f in slots_1_n])
    return groups


_CARD = re.compile(r"(\d+)\.(\d+)")


def _tile_groups(fields: list[IRField]) -> list[list[list[IRField]]]:
    """Fields of repeating cards (`IRField.card` "set.member"): the members of one set on one slide
    are the rows, their fields the columns. A member with a single field is no row."""
    sets: dict[tuple[object, str], dict[int, list[IRField]]] = {}
    for f in fields:
        if f.expected_type not in _ROW_TYPES and f.write_mode != "fill_table_rows" and f.card:
            if m := _CARD.fullmatch(f.card):
                sets.setdefault((_unit(f), m[1]), {}).setdefault(int(m[2]), []).append(f)
    groups = []
    for members in sets.values():
        rows = [members[k] for k in sorted(members) if len(members[k]) >= 2]
        if len(rows) >= 2:
            groups.append(rows)
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
    """Groups of fields that map in one call: groups -> rows in slot order -> fields in column
    order. In precedence (a field joins one group): repeating-card tiles, numbered slot grids and
    lists, native cell grids, then `fill_table_rows` fields (one one-field group each). Groups come
    by their first field in the IR. Any other field is not grouped."""
    tiles = _tile_groups(fields)
    taken = {f.field_id for g in tiles for row in g for f in row}
    rest = [f for f in fields if f.field_id not in taken]
    groups = [*tiles, *_slot_grids(rest), *_cell_grids(rest)]
    groups += [[[f]] for f in fields if f.expected_type in _ROW_TYPES]
    order = {f.field_id: i for i, f in enumerate(fields)}
    return sorted(groups, key=lambda g: min(order[f.field_id] for row in g for f in row))


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
    if field.expected_type in _ROW_TYPES:
        return (await map_group(ctx, [[field]], document_ids, retriever, llm))[field.field_id]
    hits = await _retrieve(ctx, field_query(field), document_ids, retriever)
    if not hits:
        return _missing(field, "no_relevant_chunks", document_ids)
    try:
        extraction = await extract(llm, field, hits)
        extraction = await check_candidates(llm, field, hits, extraction, other_headings)
    except LLMInvalidOutput:  # a transport LLMError propagates: the worker retries
        return _missing(field, "llm_error", document_ids)
    result = resolve(field, hits, extraction, checked_documents=document_ids)
    if field.expected_type in _LISTS and _needs_synthesis(field, result):
        return await _synthesized(llm, field, hits, result, document_ids)
    return result


def _fits(field: IRField, items: list[str]) -> bool:
    c = field.constraints
    too_many = c.max_items is not None and len(items) > c.max_items
    return not too_many and (c.max_chars is None or len("\n".join(items)) <= c.max_chars)


def _needs_synthesis(field: IRField, result: FieldMappingResult) -> bool:
    """The source has the content but not as verbatim items, or the verbatim items overflow."""
    if result.status == "populated":
        return isinstance(result.value, list) and not _fits(field, [str(i) for i in result.value])
    return result.status != "conflict" and result.missing_reason in (
        "evidence_failed_verification",
        "insufficient_evidence",
    )


async def _synthesized(
    llm: LLMClient,
    field: IRField,
    hits: list[Hit],
    first: FieldMappingResult,
    docs: list[uuid.UUID],
) -> FieldMappingResult:
    """Items written by the LLM, each kept only if `supported` by its own verified quotes. Any
    failure, or nothing kept, returns the first-pass result unchanged."""
    try:
        draft = await synthesize(llm, field, hits)
    except LLMInvalidOutput:
        return first
    values: list[str] = []
    evidence: list[Evidence] = []
    for cand in draft.items:
        quotes = _evidence(hits, cand.quotes)
        text = _BULLET.sub("", cand.value.strip())
        if text and supported(text, [e.quote for e in quotes]):
            evidence += [e.model_copy(update={"item": len(values)}) for e in quotes]
            values.append(text)
    if draft.not_found or not values:
        return first
    c = field.constraints
    whole = len(values) == len(draft.items) and _fits(field, values)
    if first.status == "populated" and not whole:
        return first  # a populated verbatim value is replaced only by a complete, fitting one
    ok = whole and (c.min_items is None or len(values) >= c.min_items)
    return FieldMappingResult(
        field_id=field.field_id,
        status="populated" if ok else "partial",
        value=values,
        evidence=evidence,
        confidence=CONF_SYNTH if ok else CONF_PARTIAL,
        missing_reason=None if ok else "insufficient_evidence",
        checked_documents=docs,
        conflict_details=None,
        provenance="rag",
    )


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


def _cell_value(cell: str, expected_type: str) -> tuple[Value, Any] | None:
    """`_convert` for one string cell: a list type gets a one-item list, not the characters."""
    return _convert([cell] if expected_type in _LISTS else cell, expected_type)


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


_MAX_QUERY_LABELS = 300


def _group_query(group: list[list[IRField]]) -> str:
    first = group[0][0]
    if first.expected_type in _ROW_TYPES:
        return field_query(first)
    tiles = all(f.card for row in group for f in row)
    # tiles repeat one set of labels: the first field of each row, once
    names = [bounded_field(row[0]).label for row in group] if tiles else None
    labels = " ".join(dict.fromkeys(names or [bounded_field(f).label for f in group[0]]))
    query = f"{labels[:_MAX_QUERY_LABELS]} ({' > '.join(bounded_field(first).section_path)})"
    shared = len({f.context for row in group for f in row}) == 1
    return f"{bounded_field(first).context}: {query}" if first.context and shared else query


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
    first = group[0][0]
    try:  # a transport LLMError propagates: the worker retries
        if first.expected_type in _ROW_TYPES:
            rows = await extract_rows(llm, first, hits)
            if rows.not_found or not rows.rows:
                return all_missing("not_found_by_llm")
            build = _chart_result if first.expected_type == "chart" else _table_result
            return {first.field_id: build(first, hits, rows, document_ids)}
        records = await extract_records(llm, group, hits)
    except LLMInvalidOutput:
        return all_missing("llm_error")
    if records.not_found or not records.records:
        return all_missing("not_found_by_llm")
    results = _record_results(group, hits, records, document_ids)
    if all(len(row) == 1 for row in group):  # a list: duplicates collapse, gaps may be synthesized
        return await _list_results(llm, group, hits, results, document_ids)
    return results


def _supports(expected_type: str, cell: str, quote: str) -> bool:
    if expected_type in TYPED_TYPES:
        return _convert(cell, expected_type) is not None and value_in_quotes(
            cell, expected_type, [quote]
        )
    return normalize(cell) in normalize(quote)


def _verified_row(
    types: list[str], hits: list[Hit], row: LLMCandidate[list[str]] | LLMRecord
) -> tuple[list[str], list[Evidence], list[list[bool]], bool]:
    """A row's cells, verified evidence, `support[cell][quote]`, and whether ONE quote supports
    every non-empty cell (the row is one source record)."""
    cells = [c.strip() for c in (row.value + [""] * len(types))[: len(types)]]
    evidence = _evidence(hits, row.quotes)
    support = [
        [bool(c) and _supports(t, c, e.quote) for e in evidence]
        for c, t in zip(cells, types, strict=True)
    ]
    anchored = any(cells) and any(
        all(support[j][i] for j, c in enumerate(cells) if c) for i in range(len(evidence))
    )
    return cells, evidence, support, anchored


def _record_results(
    group: list[list[IRField]], hits: list[Hit], records: LLMRecords, docs: list[uuid.UUID]
) -> dict[str, FieldMappingResult]:
    by_n: dict[int, LLMRecord] = {}
    for record in records.records:
        by_n.setdefault(record.n, record)  # the first of a repeated n wins, other n drop
    results = {}
    for i, row_fields in enumerate(group):  # record n fills slot row n
        record = by_n.get(i + 1)
        cells, evidence, support, anchored = (
            _verified_row([f.expected_type for f in row_fields], hits, record)
            if record
            else ([], [], [], False)
        )
        for j, field in enumerate(row_fields):
            cell = cells[j] if cells else ""
            if not cell:
                results[field.field_id] = _missing(field, "not_found_by_llm", docs)
            elif not any(support[j]) or (value := _cell_value(cell, field.expected_type)) is None:
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


async def _list_results(
    llm: LLMClient,
    group: list[list[IRField]],
    hits: list[Hit],
    results: dict[str, FieldMappingResult],
    docs: list[uuid.UUID],
) -> dict[str, FieldMappingResult]:
    """A numbered list: the distinct verified items fill slots 1..N in order; when that leaves
    slots empty, the source may state the items in prose, so `synthesize` may fill them."""
    slots = [row[0] for row in group]
    kept, seen = [], set()
    for f in slots:
        r = results[f.field_id]
        if r.status in ("populated", "partial") and (key := normalize(str(r.value))) not in seen:
            seen.add(key)
            kept.append(r)
    collapsed = {
        f.field_id: kept[k].model_copy(update={"field_id": f.field_id})
        if k < len(kept)
        else _missing(f, "not_found_by_llm", docs)
        for k, f in enumerate(slots)
    }
    have = sum(r.status == "populated" for r in collapsed.values())
    if have == len(slots) or any(f.expected_type in TYPED_TYPES for f in slots):
        return collapsed
    # shortcut: the limit is the slots' total, synthesized items are not cut to their own slot's
    # max_chars (summarize warns), upgrade by asking per-item limits.
    limits = [f.constraints.max_chars for f in slots]
    pseudo = slots[0].model_copy(
        update={
            "expected_type": "bullet_list",
            "constraints": Constraints(
                max_items=len(slots), max_chars=sum(limits) if all(limits) else None
            ),
        }
    )
    try:
        draft = await synthesize(llm, pseudo, hits)
    except LLMInvalidOutput:
        return collapsed
    items: list[tuple[str, list[Evidence]]] = []
    seen = set()
    for cand in draft.items:
        quotes = _evidence(hits, cand.quotes)
        text = _BULLET.sub("", cand.value.strip())
        if text and normalize(text) not in seen and supported(text, [e.quote for e in quotes]):
            seen.add(normalize(text))
            items.append((text, quotes))
    items = items[: len(slots)]
    if len(items) <= have:
        return collapsed
    filled = {}
    for k, f in enumerate(slots):
        if k >= len(items):
            filled[f.field_id] = _missing(f, "not_found_by_llm", docs)
            continue
        text, quotes = items[k]
        filled[f.field_id] = FieldMappingResult(
            field_id=f.field_id,
            status="populated",
            value=[text] if f.expected_type in _LISTS else text,
            evidence=[e.model_copy(update={"item": 0}) for e in quotes],  # summarised: item 0
            confidence=CONF_SYNTH,
            missing_reason=None,
            checked_documents=docs,
            conflict_details=None,
            provenance="rag",
        )
    return filled


def _table_result(
    field: IRField, hits: list[Hit], rows: LLMRows, docs: list[uuid.UUID]
) -> FieldMappingResult:
    """The rows whose every cell a quote states; unverified rows stay out of the table."""
    types = ["text"] * len(field.constraints.columns or rows.rows[0].value)
    llm_rows = rows.rows[: field.constraints.max_items]
    kept, evidence, all_anchored = [], [], True
    for cells, ev, support, anchored in (_verified_row(types, hits, r) for r in llm_rows):
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


def _word(text: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(normalize(text))}(?!\w)")


def _states_point(category: str, values: list[str], quote: str, others: list[str]) -> bool:
    """The quote states these values for this category: they follow the category before any
    other returned category starts, so `January 82 mm, February 64 mm` never backs January = 64.
    A number written before its category (`82 mm in January`) is not read as a point."""
    q = normalize(quote)
    if (m := _word(category).search(q)) is None:
        return False
    tail = q[m.end() :]
    stops = [s.start() for o in others if (s := _word(o).search(tail))]
    tail = tail[: min(stops)] if stops else tail
    return all(value_in_quotes(v, "number", [tail]) for v in values)


def _chart_result(
    field: IRField, hits: list[Hit], rows: LLMRows, docs: list[uuid.UUID]
) -> FieldMappingResult:
    """The chart's points: a row is kept only when one verified quote states its category and
    every number, so no label or value is ever invented. Fewer than two points is no chart."""
    # shortcut: canonical numbers carry no K/M suffix scaling ("$1.2M" is 1.2), so a series that
    # mixes "$900K" and "$1.2M" plots inconsistently; add unit scaling if sources do that.
    columns = field.constraints.columns or [field.label]
    types = ["text", *["number"] * len(columns)]
    llm_rows = rows.rows[: field.constraints.max_items or MAX_CHART_POINTS]
    kept: list[list[str]] = []
    evidence: list[Evidence] = []
    seen: set[str] = set()
    names = [r.value[0].strip() for r in llm_rows if r.value and r.value[0].strip()]
    for cells, ev, _, _ in (_verified_row(types, hits, r) for r in llm_rows):
        category, numbers = cells[0], [canonical_value(c, "number") for c in cells[1:]]
        others = [n for n in names if normalize(n) != normalize(category)]
        if (
            not any(_states_point(category, cells[1:], e.quote, others) for e in ev)
            or not category
            or find_tokens(category)
            or normalize(category) in seen
            or None in numbers
        ):
            continue
        seen.add(normalize(category))
        kept.append([category, *map(str, numbers)])
        have = {(x.chunk_id, x.quote) for x in evidence}
        evidence += [e for e in ev if (e.chunk_id, e.quote) not in have]
    if len(kept) < 2:
        return _missing(field, "evidence_failed_verification", docs)
    partial = len(kept) < len(llm_rows)
    return FieldMappingResult(
        field_id=field.field_id,
        status="partial" if partial else "populated",
        value=kept,
        evidence=evidence,
        confidence=CONF_PARTIAL if partial else CONF_TYPED,
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
    `on_field(done, "")` after it. A group is mapped at its first field in the IR."""
    groups = {f.field_id: g for g in row_groups(fields) for row in g for f in row}
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
