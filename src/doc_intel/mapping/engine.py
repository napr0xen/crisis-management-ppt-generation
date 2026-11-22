"""Per-field mapping: retrieve -> extract -> verify -> resolve (docs/system-design.md § 7)."""

import re
import uuid
from collections.abc import Sequence
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
    repeating slot (three risk cards with `[Risk]`), so none of its fields are shared."""
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


def field_query(field: IRField) -> str:
    field = bounded_field(field)  # template-derived text must not size the embedding query
    query = f"{field.label} — {field.description} ({' > '.join(field.section_path)})"
    return f"{field.context}: {query}" if field.context else query


async def map_field(
    ctx: TenantContext,
    field: IRField,
    document_ids: list[uuid.UUID],
    retriever: Retriever,
    llm: LLMClient,
    other_headings: Sequence[str] = (),
) -> FieldMappingResult:
    s = get_settings()
    budget = hit_budget(s.llm_num_ctx, s.chunk_max_tokens)
    hits = await retriever.search(
        ctx, field_query(field), document_ids=document_ids, top_n=min(budget, MAX_PRIMARY_HITS)
    )
    hits = await retriever.with_neighbors(ctx, hits, budget) if hits else hits
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


def _candidates(
    field: IRField, hits: list[Hit], extraction: LLMExtraction[Any]
) -> list[tuple[Value, list[Evidence]]]:
    """Verified, deduplicated candidates as (value, evidence), LLM order kept."""
    merged: dict[Any, tuple[Value, list[Evidence]]] = {}
    for cand in extraction.candidates:
        evidence: list[Evidence] = []
        seen: set[tuple[Any, str]] = set()
        for q in cand.quotes:
            if not MIN_QUOTE_CHARS <= len(q.quote) <= MAX_QUOTE_CHARS:
                continue
            hit = verify_quote(hits, q.chunk_id, q.quote)
            if hit is not None and (hit.chunk_id, q.quote) not in seen:
                seen.add((hit.chunk_id, q.quote))
                evidence.append(  # metadata from the hit, never from the LLM
                    Evidence(
                        document_id=hit.document_id,
                        chunk_id=hit.chunk_id,
                        filename=hit.filename,
                        page_number=hit.page_number,
                        section_heading=hit.section_heading,
                        quote=q.quote,
                    )
                )
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


def _is_partial(field: IRField, value: Value, extraction: LLMExtraction[Any]) -> bool:
    c = field.constraints
    if extraction.incomplete:
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
    partial = _is_partial(field, value, extraction) or not _extractive(
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
