"""Per-field mapping: retrieve -> extract -> verify -> resolve (docs/system-design.md § 7)."""

import asyncio
import json
import re
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Hashable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from dataclasses import field as dc_field
from typing import Any, Literal

import structlog
from pydantic import ValidationError

from doc_intel.core.config import get_settings
from doc_intel.core.tenancy import TenantContext
from doc_intel.embeddings.schemas import Hit
from doc_intel.llm.gateway import (
    LLMClient,
    LLMError,
    LLMInvalidOutput,
    complete_json,
    prompt_tokens,
)
from doc_intel.llm.prompts import load_prompt
from doc_intel.mapping.derived import (
    Computed,
    Table,
    both_tags,
    compute,
    named,
    numeric_headers,
    resolve_columns,
    row_line,
    summary_rows,
    tables,
    tagged_column,
)
from doc_intel.mapping.extractor import (
    MAX_CHART_POINTS,
    MAX_PRIMARY_HITS,
    PROMPT_OVERHEAD_TOKENS,
    bounded_field,
    check_candidates,
    check_roles,
    check_rows,
    extract,
    extract_batch,
    extract_records,
    extract_rows,
    hit_budget,
    synthesize,
)
from doc_intel.mapping.retriever import Retriever, select_context
from doc_intel.mapping.schemas import (
    MAX_QUOTE_CHARS,
    Candidate,
    ConflictDetails,
    Evidence,
    FieldMappingResult,
    LLMCandidate,
    LLMDerivation,
    LLMExtraction,
    LLMQuote,
    LLMRecord,
    LLMRecords,
    LLMRow,
    LLMRowCategories,
    LLMRows,
    LLMSlotValue,
    MappingSummary,
    MappingWarning,
    MissingReason,
    Value,
)
from doc_intel.mapping.verifier import (
    MIN_QUOTE_CHARS,
    TYPED_TYPES,
    bound_labels,
    canonical_value,
    derived_in_quotes,
    is_amount,
    normalize,
    source_text,
    states_value,
    supported,
    value_in_quotes,
    verify_quote,
)
from doc_intel.templates.anchors import find_tokens, has_letter
from doc_intel.templates.ir_builder import (
    _AMOUNT_WORDS,
    _IN_WORDS,
    _NET_TERMS,
    _OUT_WORDS,
    _TOTAL_WORDS,
    SLOT_FORMAT,
    _direction,
    _opening,
    _padded,
    _ranking_slots,
    derivation_intent,
    slot_type,
)
from doc_intel.templates.ir_builder import (
    _names as _slot_names,
)
from doc_intel.templates.schemas import SLOT_NUMBER, Constraints, Derivation, IRField

log = structlog.get_logger()

_BULLET = re.compile(r"^(?:[-*\u2022]|\d+[.)])\s+")
CONF_TYPED, CONF_TEXT, CONF_SYNTH, CONF_PARTIAL = 0.9, 0.8, 0.7, 0.5
MIN_PROSE_SUPPORT = 0.5  # word share a number-free synthesis item needs to be kept as partial
_LISTS = ("paragraphs", "bullet_list")
_ROW_TYPES = ("table", "chart")  # fields mapped by one structured row call


# shortcut: English/Spanish head words only ("... period", "periodo ..."), add per language.
def share_key(field: IRField) -> tuple[str, str, str]:
    """Fields with an equal key are one fact: they share one answer and one edit. A named
    placeholder (`{{x}}`, `[x]`, `<x>`) is a variable, so the same name is the same fact and the
    label is ignored (the IR labels each slide differently). A generic blank ("____", "XXX",
    "TBD") or a tokenless field shares only on an equal label and type. The template context (card
    heading, row label) must be equal too: one token under two headings is two facts."""
    token = field.constraints.token if field.write_mode == "replace_token" else None
    named = token.strip("{}[]<> ") if token and token.startswith(("{{", "[", "<")) else ""
    fact = normalize(named or field.label).replace("_", " ").replace("-", " ")
    # a named period is the document's: every slide's subtitle states the same one
    words = fact.split()
    period = bool(named and words) and (words[-1] == "period" or words[0] == "periodo")
    context = "" if period else " ".join(normalize(field.context or "").split())
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
    are the rows, their fields the columns. A member with a single field is no row. Keyed sets
    are returned too: `row_groups` maps each of their cards as a one-row group."""
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


def _keyed(rows: list[list[IRField]]) -> bool:
    """Every card has its own static heading: each card is its own one-row records group (a
    records call over all cards drops cards 2..N), and the sibling headings reject a value stated
    under another card."""
    heads = [next((f.context for f in row if has_letter(f.context)), None) for row in rows]
    return None not in heads and len(set(heads)) == len(heads)


def row_groups(fields: list[IRField]) -> list[list[list[IRField]]]:
    """Groups of fields that map in one call: groups -> rows in slot order -> fields in column
    order. In precedence (a field joins one group): repeating-card tiles, numbered slot grids and
    lists, native cell grids, then `fill_table_rows` fields (one one-field group each). A keyed
    card set is one one-row group per card. Groups come by their first field in the IR. Any other
    field is not grouped."""
    sets = _tile_groups(fields)
    tiles = [[row] for g in sets if _keyed(g) for row in g] + [g for g in sets if not _keyed(g)]
    taken = {f.field_id for g in sets for row in g for f in row}
    rest = [f for f in fields if f.field_id not in taken]
    groups = [*tiles, *_slot_grids(rest), *_cell_grids(rest)]
    groups += [[[f]] for f in fields if f.expected_type in _ROW_TYPES]
    order = {f.field_id: i for i, f in enumerate(fields)}
    return sorted(groups, key=lambda g: min(order[f.field_id] for row in g for f in row))


def field_query(field: IRField) -> str:
    field = bounded_field(field)  # template-derived text must not size the embedding query
    query = f"{field.label} — {field.description} ({' > '.join(field.section_path)})"
    return f"{field.context}: {query}" if field.context else query


def _chunk_budget() -> int:
    """How many chunks one prompt holds: the window's and the retrieval token budget's lesser."""
    s = get_settings()
    return min(
        hit_budget(s.llm_num_ctx, s.chunk_max_tokens),
        max(1, s.retrieval_context_tokens // s.chunk_max_tokens),
    )


async def _retrieve(
    ctx: TenantContext,
    query: str,
    document_ids: list[uuid.UUID],
    retriever: Retriever,
    heading: str | None = None,
) -> list[Hit]:
    # the whole chunk budget goes to ranked hits: unscored neighbor chunks are left out by curate
    top_n = min(_chunk_budget(), MAX_PRIMARY_HITS)
    hits = await retriever.search(ctx, query, document_ids=document_ids, top_n=top_n)
    if has_letter(heading) and heading != query:  # a section lead-in rarely matches the label
        seen = {h.chunk_id for h in hits}
        lead = await retriever.search(ctx, heading or "", document_ids=document_ids, top_n=1)
        hits += [h for h in lead if h.chunk_id not in seen]
    return select_context(hits) if hits else hits


Intent = tuple[str, Literal["CR", "DR"] | None]  # (op, tag), see `derivation_intent`


@dataclass
class _Shared:
    """What derived slots of one `map_fields` run share: the first successful binding per key
    (slots of one intent, or of one ranked group_sum series, give identical numbers and
    citations) and, per slot of a group_sum series, the series' lead field and intent."""

    bound: dict[Hashable, tuple[Table, Derivation]] = dc_field(default_factory=dict)
    series: dict[str, tuple[IRField, Intent]] = dc_field(default_factory=dict)
    # a refused binding per key: the other slots of the intent or series make no new call
    failed: dict[Hashable, MissingReason] = dc_field(default_factory=dict)


async def map_field(
    ctx: TenantContext,
    field: IRField,
    document_ids: list[uuid.UUID],
    retriever: Retriever,
    llm: LLMClient,
    other_headings: Sequence[str] = (),
    retry: tuple[LLMExtraction[Any], list[str]] | None = None,
    rank: int | None = None,
    shared: _Shared | None = None,
) -> FieldMappingResult:
    """`retry`: an earlier reply and why it was rejected, sent with the first extraction. `rank`:
    the slot number of a derived slot in a numbered grid. `shared`: see `_Shared`."""
    if field.mode == "derived":  # before the row types: a group_sum slot is a table or a list
        return await _derived(ctx, field, document_ids, retriever, llm, rank, shared)
    if field.expected_type in _ROW_TYPES:
        group = await map_group(ctx, [[field]], document_ids, retriever, llm, other_headings)
        return group[field.field_id]
    heading = bounded_field(field).context
    hits = await _retrieve(ctx, field_query(field), document_ids, retriever, heading)
    if not hits:
        return _missing(field, "no_relevant_chunks", document_ids)
    if field.mode == "synthesis" and field.expected_type not in TYPED_TYPES:
        # a narrative slot is written from the chunks, never looked up verbatim first
        # no heading check: a written summary never sits under the slot's heading in the source
        first = _missing(field, "not_found_by_llm", document_ids)
        return await _synthesized(llm, field, hits, first, document_ids)
    try:
        extraction = await extract(llm, field, hits, retry=retry)
        if notes := _rule_notes(field, hits, extraction):  # one corrective retry, never a loop
            extraction = await extract(llm, field, hits, retry=(extraction, notes))
        # the heading check keeps sibling cards' values out (Phase 14), so it needs siblings
        if other_headings:
            extraction = await check_candidates(llm, field, hits, extraction, other_headings)
    except LLMInvalidOutput:  # a transport LLMError propagates: the worker retries
        return _missing(field, "llm_error", document_ids)
    result = resolve(field, hits, extraction, checked_documents=document_ids)
    result = await _filled_by_synthesis(llm, field, hits, result, document_ids)
    result = await _escalated(llm, field, hits, result, document_ids, other_headings, extraction)
    return await _paraphrased(llm, field, hits, result, document_ids)


MIN_PROSE_CHARS = 40  # a slot this many characters wide holds a sentence, not a label or figure
# shortcut: box size cannot tell a name in a wide box from prose, so it gets one (verified,
# at most partial when weak) synthesis try too; upgrade with a prose signal from the IR.


def _prose(field: IRField) -> bool:
    """A free-text slot sized for prose: its box fits (`fit_chars`) or allows (`max_chars`) a
    sentence. A slot of unknown size is not."""
    size = field.fit_chars or field.constraints.max_chars or 0
    return field.expected_type in ("text", *_LISTS) and size >= MIN_PROSE_CHARS


async def _filled_by_synthesis(
    llm: LLMClient,
    field: IRField,
    hits: list[Hit],
    result: FieldMappingResult,
    docs: list[uuid.UUID],
) -> FieldMappingResult:
    """A list the verbatim pass could not fill whole (`_needs_synthesis`), or an extractive prose
    slot the model reports not found, gets one grounded synthesis attempt; the prose fallback
    follows the synthesis-mode rules, so a weakly backed value is at most `partial`."""
    if field.expected_type in _LISTS and _needs_synthesis(field, result):
        return await _synthesized(llm, field, hits, result, docs)
    if field.mode == "extractive" and result.missing_reason == "not_found_by_llm" and _prose(field):
        loose = field.model_copy(update={"mode": "synthesis"})
        return await _synthesized(llm, loose, hits, result, docs)
    return result


async def _paraphrased(
    llm: LLMClient,
    field: IRField,
    hits: list[Hit],
    result: FieldMappingResult,
    docs: list[uuid.UUID],
) -> FieldMappingResult:
    """An extractive prose slot whose candidates still all fail verification after the thinking
    retry (a note no source states verbatim) gets one grounded synthesis attempt, at most
    `partial` when weakly backed. After the retry, so a slightly misquoted name is copied, not
    written."""
    if (
        field.mode == "extractive"
        and result.missing_reason == "evidence_failed_verification"
        and _prose(field)
    ):
        loose = field.model_copy(update={"mode": "synthesis"})
        return await _synthesized(llm, loose, hits, result, docs)
    return result


def _escalation_reason(field: IRField, result: FieldMappingResult, hits: list[Hit]) -> str | None:
    """Why a first-pass result earns one thinking retry, or None (`populated` and `partial`
    never do, nor a miss with no well-scored chunk, an LLM error or no chunks at all, nor a
    synthesis slot's miss: no verbatim value is what it expects)."""
    if result.status == "conflict":
        return "conflict"
    if result.status != "missing":
        return None
    if result.missing_reason == "evidence_failed_verification":
        return "verification_failed"
    threshold = get_settings().think_escalation_score_threshold
    if result.missing_reason != "not_found_by_llm" or field.mode == "synthesis":
        return None
    return "high_score_miss" if any(h.score >= threshold for h in hits) else None


def _near_cited(hits: list[Hit], extraction: LLMExtraction[Any]) -> list[Hit]:
    """The retrieved chunks the candidates cite and their neighbors, else every chunk."""
    cited = {q.chunk_id.strip().lower() for c in extraction.candidates for q in c.quotes}
    at = {(h.document_id, h.chunk_index) for h in hits if str(h.chunk_id) in cited}
    beside = {(d, i + step) for d, i in at if i is not None for step in (-1, 1)}
    near = [h for h in hits if str(h.chunk_id) in cited or (h.document_id, h.chunk_index) in beside]
    return near or hits


def _log_rejected(slot_id: str, codes: list[str]) -> None:
    log.info("verification_rejected", slot_id=slot_id, codes=codes)  # codes only, never text


_ESCALATED: ContextVar[set[Hashable] | None] = ContextVar("escalated", default=None)
# the escalations one `map_fields` run made: the same rejection is never sent to the model twice


def _feedback(
    field: IRField, hits: list[Hit], reason: str, extraction: LLMExtraction[Any] | None
) -> list[tuple[Value, str, str]]:
    """(value, code, note) for each candidate of a failed first reply. A candidate dropped
    with no `_rejection` code (heading check, quote not found) is a generic `quote_not_verified`."""
    if reason != "verification_failed" or extraction is None:
        return []
    out = []
    for c in extraction.candidates:
        code, note = _rejection(field, hits, c.value, _evidence(hits, c.quotes)) or (
            "quote_not_verified",
            "it was not accepted for this slot: its quote did not verify or it belongs under "
            "another heading",
        )
        out.append((c.value, code, note))
    return out


def _feedback_notes(field: IRField, notes: list[tuple[Value, str, str]]) -> list[str]:
    kind = getattr(field, "slot_type", None) or field.expected_type
    return [
        f"Your previous extraction {str(v)[:40]!r} was rejected because {code}: {note}."
        for v, code, note in notes
    ] + [
        f"Re-read the chunks and give a valid {kind} for {field.label!r} with quotes copied "
        "exactly from the chunk they cite."
    ]


async def _escalated(
    llm: LLMClient,
    field: IRField,
    hits: list[Hit],
    first: FieldMappingResult,
    docs: list[uuid.UUID],
    other_headings: Sequence[str] = (),
    extraction: LLMExtraction[Any] | None = None,
) -> FieldMappingResult:
    """At most one more extraction, thinking on, for a conflict, a verification failure or a
    high-score miss. A verification failure of `extraction` (the first reply) logs its reason
    codes and retries with that reply, each rejected value with its code and a closing note naming
    the slot type (`_feedback_notes`), over the cited chunks and their neighbors when a candidate
    has a code; a high-score miss retries naming the three best-scored chunks. One `map_fields`
    run never repeats an escalation of the same slot, reason and rejections (`_ESCALATED`).
    Verified like the first pass, a list re-checked against its length limits; a `missing`
    reply, a list that still overflows or an LLM error keeps the first result, so a verified
    conflict is never dropped and nothing loops. Groups: `_escalated_group`."""
    s = get_settings()
    failed = first.missing_reason == "evidence_failed_verification"
    rejected = _rejections(field, hits, extraction) if failed and extraction else []
    if rejected:
        _log_rejected(field.field_id, [code for code, _ in rejected])
    if not s.think_on_escalation or (reason := _escalation_reason(field, first, hits)) is None:
        return first
    start, after, error = time.monotonic(), first, None
    lists = field.expected_type in _LISTS
    notes = _feedback(field, hits, reason, extraction)
    key = (field.field_id, reason, frozenset((c, normalize(str(v))) for v, c, _ in notes))
    if (memo := _ESCALATED.get()) is not None:
        if key in memo:  # the same rejection again: another thinking call would answer the same
            log.info("think_escalation_skipped", slot_id=field.field_id, reason=reason)
            return first
        memo.add(key)
    retry = (extraction, _feedback_notes(field, notes)) if extraction and notes else None
    if rejected and extraction:
        hits = _near_cited(hits, extraction)
    if reason == "high_score_miss" and extraction:
        top = ", ".join(str(h.chunk_id) for h in sorted(hits, key=lambda h: -h.score)[:3])
        retry = (
            extraction,
            [
                f"Your previous reply found no value, although chunks {top} score highest for "
                "this slot: inspect every line of them, including table rows and footers, and "
                "reply not_found only if the value appears nowhere in the provided chunks."
            ],
        )
    try:
        budget = s.think_escalation_max_tokens
        extraction = await extract(llm, field, hits, retry=retry, think_budget=budget)
        if other_headings:  # the same heading check as the first pass
            extraction = await check_candidates(llm, field, hits, extraction, other_headings)
        after = resolve(field, hits, extraction, checked_documents=docs)
        if lists and after.status != "missing" and _needs_synthesis(field, after):
            after = await _synthesized(llm, field, hits, after, docs)  # the first pass's check
    except LLMError as exc:  # best effort: a slow or invalid thinking call leaves the first pass
        error = type(exc).__name__
    overflow = isinstance(after.value, list) and not _fits(field, [str(i) for i in after.value])
    kept = first if after.status == "missing" or (lists and overflow) else after
    _log_escalation(field.field_id, reason, first.status, kept.status, start, error)
    return kept


def _log_escalation(
    slot_id: str, reason: str, before: str, after: str, start: float, error: str | None
) -> None:
    log.info(
        "think_escalation",
        slot_id=slot_id,  # a slug (IRField pattern), loggable as in ir_invalid
        reason=reason,
        before_status=before,
        after_status=after,
        elapsed_ms=round((time.monotonic() - start) * 1000),
        error=error,  # the type only, never the message
    )


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


def _backing(text: str, said: list[str], loose: bool) -> Literal["strong", "weak"] | None:
    """`strong` when the quotes `supported` the item. When `loose`, `weak` for an item a summary
    of table rows writes (it shares few of their words): every number it states is quoted, or it
    states none and half its words are. Else None: the item is dropped."""
    if text and supported(text, said):
        return "strong"
    floor = 0.0 if any(map(str.isdigit, text)) else MIN_PROSE_SUPPORT
    return "weak" if loose and text and supported(text, said, floor) else None


async def _synthesized(
    llm: LLMClient,
    field: IRField,
    hits: list[Hit],
    first: FieldMappingResult,
    docs: list[uuid.UUID],
) -> FieldMappingResult:
    """Items written by the LLM, each kept only if `supported` by its own verified quotes (a text
    field gets them joined into one string, every quote backing item 0). A `synthesis` field also
    keeps an item that shares few of its quotes' words but states only their numbers, or that
    states no number and shares half its words, and is then `partial`. Any failure, or nothing
    kept, returns the first-pass result unchanged."""
    try:
        draft = await synthesize(llm, field, hits)
    except LLMInvalidOutput:
        return first
    lists = field.expected_type in _LISTS
    values: list[str] = []
    evidence: list[Evidence] = []
    weak = False  # an item kept on its numbers alone: the slot needs review
    for cand in draft.items:
        quotes = _evidence(hits, cand.quotes)
        text = _BULLET.sub("", cand.value.strip())
        if backing := _backing(text, [e.quote for e in quotes], field.mode == "synthesis"):
            weak |= backing == "weak"
            evidence += [e.model_copy(update={"item": len(values) if lists else 0}) for e in quotes]
            values.append(text)
    if draft.not_found or not values:
        return first
    c = field.constraints
    value: Value = values if lists else " ".join(values)
    whole = len(values) == len(draft.items) and _fits(field, values if lists else [str(value)])
    if first.status == "populated" and not whole:
        return first  # a populated verbatim value is replaced only by a complete, fitting one
    ok = whole and not weak and (c.min_items is None or len(values) >= c.min_items)
    return FieldMappingResult(
        field_id=field.field_id,
        status="populated" if ok else "partial",
        value=value,
        evidence=evidence,
        confidence=CONF_SYNTH if ok else CONF_PARTIAL,
        missing_reason=None if ok else "insufficient_evidence",
        checked_documents=docs,
        conflict_details=None,
        provenance="rag",
    )


MAX_BIND_TABLES = 20  # shortcut: tables past the 20th are not offered to the binding, upgrade by
# ranking the tables against the slot label when a source holds more.
_BIND_ROWS = 3  # rows shown from each end of a table in the binding prompt


def _bind_view(label: str, table: Table) -> str:
    rows = (
        table.rows
        if len(table.rows) <= 2 * _BIND_ROWS
        else (table.rows[:_BIND_ROWS] + table.rows[-_BIND_ROWS:])
    )
    lines = [f"[{r.id}] {ln}" for h, r in rows if (ln := row_line(h, r)) is not None]
    return (
        f"{label} | {table.rows[0][0].filename}\nheaders: "
        f"{json.dumps(list(table.headers), ensure_ascii=False)}\n" + "\n".join(lines)
    )


async def _bound(
    llm: LLMClient,
    field: IRField,
    found: list[Table],
    retry: tuple[LLMDerivation, list[str]] | None = None,
    intent: Intent | None = None,
) -> tuple[Table, Derivation, LLMDerivation] | MissingReason:
    """The LLM call of a derived slot: which table and which exact headers. A bad or unusable
    binding is a reason, never a guess. `retry` is an earlier binding and why it was refused (the
    house retry shape of `extract`)."""
    spec = bounded_field(field).model_dump_json(
        include={"label", "description", "expected_type", "section_path", "context"},
        exclude_none=True,
    )
    views = "\n\n".join(_bind_view(f"t{i}", t) for i, t in enumerate(found[:MAX_BIND_TABLES], 1))
    messages = [
        {"role": "system", "content": load_prompt("derive_bind")},
        {"role": "user", "content": f"Slot:\n{spec}\n\nTables:\n\n{views}"},
    ]
    if retry:
        earlier, notes = retry
        listing = "\n".join(f"- {n}" for n in notes)
        messages += [
            {"role": "assistant", "content": earlier.model_dump_json()},
            {
                "role": "user",
                "content": f"That binding does not fit this slot:\n{listing}\nReply with the "
                "corrected full JSON.",
            },
        ]
    try:
        bind = await complete_json(llm, messages, LLMDerivation)
    except LLMInvalidOutput:  # a transport LLMError propagates: the worker retries
        return "llm_error"
    labels = {f"t{i}": t for i, t in enumerate(found[:MAX_BIND_TABLES], 1)}
    table = labels.get(bind.table.strip().lower())
    if not bind.feasible or table is None or bind.derivation is None:
        return "not_found_by_llm"  # `bind.reason` is model text: never logged
    spec = _with_intent(field, bind.derivation, table, intent)
    if spec == "column_ambiguous" and retry is None:  # one correction, naming the usable columns
        heads = json.dumps(numeric_headers(table), ensure_ascii=False)
        note = (
            f"column_ambiguous: {bind.derivation.column!r} is not one usable numeric column of "
            f"{bind.table.strip().lower()}; set column to exactly one of {heads}, or reply "
            "feasible false with a reason."
        )
        return await _bound(llm, field, found, (bind, [note]), intent)
    if spec == "group_ambiguous":  # no column holds the group: the LLM sorts the rows
        sorted_rows = await _categorized(llm, field, table)
        if isinstance(sorted_rows, str):
            return sorted_rows
        table = sorted_rows
        by_label = bind.derivation.model_copy(update={"group_by": ASSIGNED, "group_sep": None})
        spec = _with_intent(field, by_label, table, intent)
    if isinstance(spec, str):
        log.info("derived_rejected", slot_id=field.field_id, code=spec)  # code only, never text
        return "no_relevant_chunks" if spec == "column_missing" else "evidence_failed_verification"
    return table, spec, bind


ASSIGNED = "Category (assigned)"  # the header of LLM-assigned row categories, never a source one
# shortcut: a table of more data rows is not sorted (the slot stays missing), upgrade by sorting
# in batches when a source needs it.
MAX_SORTED_ROWS = 150


async def _categorized(llm: LLMClient, field: IRField, table: Table) -> Table | MissingReason:
    """`table` with an `ASSIGNED` column: one LLM call names a category per data row from the
    row's own line. A row it skips gets an empty label, so a breakdown using it fails closed
    (`group_label_missing`). The labels are the model's, so the slot is at most `partial`."""
    data = [(h, r) for h, r in table.rows if r.row_role == "data"]
    if not data or len(data) > MAX_SORTED_ROWS or ASSIGNED in table.headers:
        return "evidence_failed_verification"
    spec = bounded_field(field).model_dump_json(
        include={"label", "description", "section_path", "context"}, exclude_none=True
    )
    lines = [f"[{r.id}] {ln}" for h, r in data if (ln := row_line(h, r)) is not None]
    heads = json.dumps(list(table.headers), ensure_ascii=False)
    messages = [
        {"role": "system", "content": load_prompt("row_categories")},
        {"role": "user", "content": f"Slot:\n{spec}\n\nheaders: {heads}\n" + "\n".join(lines)},
    ]
    try:
        reply = await complete_json(llm, messages, LLMRowCategories)
    except LLMInvalidOutput:  # a transport LLMError propagates: the worker retries
        return "llm_error"
    got = {c.id.strip(): c.category.strip() for c in reply.rows}
    rows = tuple(
        (h, r.model_copy(update={"cells": r.cells | {ASSIGNED: got.get(r.id, "")}}))
        for h, r in table.rows
    )
    return replace(table, headers=(*table.headers, ASSIGNED), rows=rows)


def _with_intent(
    field: IRField,
    bound: Derivation,
    table: Table,
    intent: Intent | None,
) -> Derivation | str:
    """The binding with the op and tag the slot's names fix (`derivation_intent`, or the series'
    `intent`): the binder names the table, code fixes what the slot means and finds the headers
    (`resolve_columns`). A tag needs a CR/DR-tagged column (separate money in / money out columns
    have none). A failure code when the result is no valid derivation."""
    op, tag = intent or (bound.op, None)
    tag = tag or (bound.tag if op != "diff" else None)  # no stated direction: the binder's tag
    resolved = resolve_columns(table, bound.model_copy(update={"op": op}), tag)
    if isinstance(resolved, str) or intent is None:
        return resolved
    bound = resolved
    if tag and not tagged_column(table, bound.column):
        # no CR/DR cells: only a filter or a column named for the direction keeps the tag's
        # meaning, else the sum would mix both directions
        if not (bound.where or _direction((bound.column,)) == tag):
            return "intent_mismatch"
        tag = None
    if op == "group_sum" and tag is None and both_tags(table, bound.column):
        # shortcut: a breakdown that names no direction groups the money out (a spending
        # breakdown), upgrade when a template asks for an untagged income breakdown.
        tag = "DR"
    spec = bound.model_dump() | {"op": op, "tag": tag, "rank": None}
    picking = ("top", "argmax", "argmin", "lookup")
    for name, only in (
        ("minus", ("diff",)),
        ("group_by", ("group_sum", "group_count")),
        ("pick", picking),
    ):
        spec[name] = spec[name] if op in only else None
    if op not in ("group_sum", "group_count"):
        spec["group_sep"] = None
    numeric = field.expected_type in ("number", "currency", "percent")
    if op in ("top", "argmax", "argmin") and not numeric and spec["pick"] in (None, bound.column):
        return "intent_mismatch"  # a text slot would take the amount cell
    try:
        return Derivation.model_validate(spec)
    except ValidationError:
        return "intent_mismatch"


def _row_evidence(table: Table, cited: tuple[Any, ...]) -> list[Evidence] | None:
    """One Evidence per cited row, its verbatim line in the chunk; None when a line does not
    re-verify or does not fit the quote limits (fail closed)."""
    out = []
    for hit, row, item in cited:
        quote = row_line(hit, row)
        if (
            quote is None
            or not MIN_QUOTE_CHARS <= len(quote) <= MAX_QUOTE_CHARS
            or verify_quote([hit], str(hit.chunk_id), quote) is None
        ):
            return None
        out.append(
            Evidence(
                document_id=hit.document_id,
                chunk_id=hit.chunk_id,
                filename=hit.filename,
                page_number=hit.page_number,
                section_heading=hit.section_heading,
                quote=quote,
                item=item,
                row_id=row.id,
            )
        )
    return out


async def _derived(
    ctx: TenantContext,
    field: IRField,
    docs: list[uuid.UUID],
    retriever: Retriever,
    llm: LLMClient,
    rank: int | None = None,
    shared: _Shared | None = None,
) -> FieldMappingResult:
    """A `derived` slot (ADR-018): the aggregate is computed by `derived.compute` over the rows of
    one table; the LLM only binds the table when the IR has no derivation (code resolves the
    headers). `rank` (the slot number, from code) picks the k-th group or row of a group_sum or
    top derivation. `shared` reuses an earlier successful binding of the same intent or series."""
    lead, intent = (shared.series.get(field.field_id) if shared else None) or (
        field,
        derivation_intent(field),
    )
    key: Hashable | None = None  # `top` is never shared: each slot picks its own cell
    if field.field_id in (shared.series if shared else {}):
        key = ("series", lead.field_id)
    elif intent and intent[0] != "top" and not rank:
        key = (*intent, _residual(field))
    memo = shared.bound.get(key) if shared and key is not None and not field.derivation else None

    def run(table: Table, spec: Derivation) -> Computed | str:
        if rank and spec.op in ("group_sum", "top"):
            spec = spec.model_copy(update={"rank": rank})
        return compute(table, spec, field.expected_type)

    # shortcut: each derived slot re-reads all table chunks, cache per map_fields run if timing
    # shows it
    found = tables(await retriever.table_chunks(ctx, docs))
    if not found:
        return _missing(field, "no_relevant_chunks", docs)
    if field.derivation:
        fits = [t for t in found if all(h in t.headers for h in named(field.derivation))]
        if not fits:
            return _missing(field, "no_relevant_chunks", docs)
        # shortcut: two tables with the headers fail closed, upgrade by asking the binding call.
        if len(fits) > 1:
            return _missing(field, "evidence_failed_verification", docs)
        table, spec = fits[0], field.derivation
        out = run(table, spec)  # shortcut: a preset spec cannot name row
        # ids, so summary_row is missing; upgrade by letting the binding call fix presets
    elif memo:
        table, spec = memo
        out = run(table, spec)
    elif shared and key is not None and key in shared.failed:
        return _missing(field, shared.failed[key], docs)  # no new call for a refused binding
    else:
        bound = await _bound(llm, lead, found, intent=intent)
        if isinstance(bound, str):
            if shared and key is not None:
                shared.failed.setdefault(key, bound)
            return _missing(field, bound, docs)
        table, spec, bind = bound
        out = run(table, spec)
        if out == "summary_row":  # one corrective re-binding naming the flagged rows
            log.info("derived_rejected", slot_id=field.field_id, code=out)
            ids = ", ".join(summary_rows(table, spec))
            note = (
                f"summary_row: rows {ids} equal the sum of the rows before them, so they look "
                "like printed totals, subtotals or carried-forward rows; list every such row in "
                "exclude_rows."
            )
            again = await _bound(llm, lead, found, (bind, [note]), intent)
            if isinstance(again, str):
                return _missing(field, "evidence_failed_verification", docs)
            table, spec, _ = again
            out = run(table, spec)
    if (
        shared
        and key is not None
        and not field.derivation
        and not spec.where  # a filtered binding answers its own slot only
        and (not isinstance(out, str) or out in ("rank_tied", "rank_out_of_range"))
    ):
        shared.bound.setdefault(key, (table, spec))  # a failed rank is the series' too
    if isinstance(out, str):
        log.info("derived_rejected", slot_id=field.field_id, code=out)  # code only, never text
        return _missing(
            field,
            "no_relevant_chunks"
            if out in ("column_missing", "no_rows")
            else ("evidence_failed_verification"),
            docs,
        )
    evidence = _row_evidence(table, out.cited)
    if out.number is not None:  # the exact Decimal, never the rendered text re-parsed
        n = out.number
        stored = (int(n) if n == n.to_integral_value() else float(n),)
    else:
        stored = _convert(out.value, field.expected_type)
    if not evidence or stored is None:
        log.info("derived_rejected", slot_id=field.field_id, code="evidence")
        return _missing(field, "evidence_failed_verification", docs)
    typed = field.expected_type in TYPED_TYPES
    assigned = spec.group_by == ASSIGNED  # model-made group labels: never verified, review them
    return FieldMappingResult(
        field_id=field.field_id,
        status="partial" if assigned else "populated",
        value=stored[0],
        evidence=evidence,
        confidence=CONF_PARTIAL if assigned else (CONF_TYPED if typed else CONF_TEXT),
        missing_reason="insufficient_evidence" if assigned else None,
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


def _located(hits: list[Hit], chunk_id: str, quote: str) -> list[tuple[Hit, str]]:
    """(hit, quote) for a quote, verbatim in its cited hit. When it is not there but the cited
    chunk was retrieved, the quote, else each of its lines, may be found in that chunk or a
    retrieved chunk right before or after it (a small model cites the heading chunk next to a
    table, or glues a table's header onto a later row). An unretrieved chunk id finds nothing."""
    if (hit := verify_quote(hits, chunk_id, quote)) is not None:
        return [(hit, quote)]
    cited = next((h for h in hits if str(h.chunk_id) == chunk_id.strip().lower()), None)
    if cited is None:
        return []
    near = [cited] + [
        h
        for h in hits
        if cited.chunk_index is not None
        and h.document_id == cited.document_id
        and h.chunk_index is not None
        and abs(h.chunk_index - cited.chunk_index) == 1
    ]
    lines = [line.strip() for line in quote.splitlines()] if "\n" in quote else []
    found = []
    for part in [quote, *lines]:
        if sum(map(str.isalnum, part)) < MIN_QUOTE_CHARS:
            continue  # a separator row such as "| --- | --- |"
        if hit := next((h for h in near if verify_quote([h], str(h.chunk_id), part)), None):
            found.append((hit, part))
            if part is quote:
                break
    return found


def _evidence(hits: list[Hit], quotes: list[LLMQuote]) -> list[Evidence]:
    """The quotes that verify against `hits` (see `_located`), with the hit's metadata."""
    evidence: list[Evidence] = []
    seen: set[tuple[Any, str]] = set()
    for q in quotes:
        if not MIN_QUOTE_CHARS <= len(q.quote) <= MAX_QUOTE_CHARS:
            continue
        for hit, quote in _located(hits, q.chunk_id, q.quote):
            if (hit.chunk_id, quote) not in seen:
                seen.add((hit.chunk_id, quote))
                evidence.append(
                    Evidence(
                        document_id=hit.document_id,
                        chunk_id=hit.chunk_id,
                        filename=hit.filename,
                        page_number=hit.page_number,
                        section_heading=hit.section_heading,
                        quote=quote,
                    )
                )
    return evidence


_PLAIN_NUMBER = re.compile(r"[\d.,\s]+")
_CELL_BREAK = " | "  # a markdown cell boundary: the value is several table cells


def _is_count(field: IRField) -> bool:
    token = field.constraints.token if field.write_mode == "replace_token" else None
    return bool(token) and token.strip("{[< ").startswith("#")


def _names(label: str, quotes: list[str]) -> bool:
    """Every word of `label` is in the quotes: a word of 5+ letters by its first 5 (inflection,
    a PDF line break inside it), a shorter one whole. Words under 4 letters ("to", "of") are
    skipped unless the label has no longer one ("RFx")."""
    have = set(re.findall(r"[^\W\d_]+", normalize(" ".join(quotes))))
    words = re.findall(r"[^\W\d_]+", normalize(label))
    words = [w for w in words if len(w) >= 4] or words
    return bool(words) and all(
        any(h.startswith(w[:5]) for h in have) if len(w) >= 5 else w in have for w in words
    )


_WORDS = re.compile(r"[^\W_]+")


def _echoes_heading(field: IRField, value: Any) -> bool:
    """A card slot's text-like value (every item of a list) whose words are all words of the
    slot's own context: it repeats the card heading instead of stating what the source says under
    it. Table cells are not checked: their context also holds the slide heading, which a correct
    cell value may repeat ("North" under "North region sales / Region")."""
    if not (field.card and field.expected_type in ("text", *_LISTS) and has_letter(field.context)):
        return False
    heading = set(_WORDS.findall(normalize(field.context or "")))
    items = value if isinstance(value, list) else [value]
    words = [set(_WORDS.findall(normalize(str(i)))) for i in items]
    return bool(words) and all(w and w <= heading for w in words)


def _header_rows(text: str, quotes: list[str]) -> list[str]:
    """The header row of each Markdown table in `text` that a quote copies a body row of: a row of
    a sideways table ("| No. of events | 7 | 6 |") names its columns only there."""
    said = [normalize(q) for q in quotes]
    found: list[str] = []
    table: list[str] = []
    for line in [*text.splitlines(), ""]:
        if line.lstrip().startswith("|"):
            table.append(line)
            continue
        body = [normalize(r) for r in table[1:]]
        if any(r and any(c.isalnum() for c in r) and r in q for r in body for q in said):
            found.append(table[0])
        table = []
    return found


def _rule_note(
    field: IRField, value: Any, evidence: list[Evidence], hits: Sequence[Hit] = ()
) -> str | None:
    """Why a verified candidate breaks the slot's own rules (said back to the LLM), else None. A
    `#` count is a whole number with no unit, and one chunk states both that number and the slot's
    own label (the last part of its context, or the header row of the cited table): never a
    headline total or an amount, never a number from one chunk and a label from another. A text
    slot is one cell, never a row, and never just its own heading repeated."""
    if _is_count(field):
        own = (field.context or field.label).rsplit(" / ", 1)[-1]
        texts = {h.chunk_id: h.text for h in hits}
        by_chunk: dict[uuid.UUID, list[str]] = {}
        for e in evidence:
            by_chunk.setdefault(e.chunk_id, []).append(e.quote)
        plain = [
            quotes + _header_rows(texts.get(chunk, ""), quotes)
            for chunk, quotes in by_chunk.items()
            if (span := source_text(value, "number", quotes)) is not None
            and _PLAIN_NUMBER.fullmatch(span)
        ]
        if not isinstance(canonical_value(value, "number"), int) or not plain:
            return f"{str(value)[:40]!r} is not a whole count with no unit or magnitude."
        if not any(_names(own, quotes) for quotes in plain):
            return f"no quote states {str(value)[:40]!r} as the count for {own[:80]!r}."
    if field.expected_type == "text" and _CELL_BREAK in str(value):
        return "the value copies several table cells; give only the one cell this slot asks for."
    if _echoes_heading(field, value):
        return (
            "the value only repeats the slot's heading; give the description the chunks state "
            "under that heading."
        )
    return None


def _rule_notes(field: IRField, hits: list[Hit], extraction: LLMExtraction[Any]) -> list[str]:
    """One note per candidate when every candidate verifies but breaks a slot rule, else []."""
    notes = []
    for cand in extraction.candidates:
        evidence = _evidence(hits, cand.quotes)
        note = _rule_note(field, cand.value, evidence, hits) if evidence else None
        if note is None:
            return []
        notes.append(note)
    return notes


def _row_label_note(
    field: IRField, hits: Sequence[Hit], value: Any, evidence: list[Evidence]
) -> str | None:
    """A typed value that sits only in cells labelled otherwise (row label + column header, from
    the cited chunks' structured rows) while another cell is labelled like the slot: the note for
    the retry, else None. No rows, no labelled match anywhere or a value cell that matches: None."""
    cited = {e.chunk_id for e in evidence}
    cells = [
        (r.id, first if k != next(iter(r.cells)) else "", k, v)
        for h in hits
        if h.chunk_id in cited
        for r in h.rows or []
        for first in [next(iter(r.cells.values()), "")]
        for k, v in r.cells.items()
    ]
    kind = field.expected_type
    named = [_names(field.label, [f"{row} {head}"]) for _, row, head, _ in cells]
    stated = [value_in_quotes(value, kind, [text]) for *_, text in cells]
    if not any(stated) or any(n and s for n, s in zip(named, stated, strict=True)):
        return None
    # the labelled cell must itself state a value of the slot's type, else it is no rival
    true = next(
        (
            c
            for c, n, s in zip(cells, named, stated, strict=True)
            if n and not s and states_value(c[3], kind)
        ),
        None,
    )
    if true is None:
        return None
    at = next(c for c, s in zip(cells, stated, strict=True) if s)
    return (
        f"{str(value)[:40]!r} is in row [{at[0]}] {(at[1] or at[2])[:60]!r}; the slot "
        f"{field.label[:60]!r} is row [{true[0]}] {(true[1] or true[2])[:60]!r}."
    )


_AMOUNT_CODE = "amount_in_text_column"
# shortcut: English/Spanish header words only, add words (or an LLM header classifier) when a
# template in another language puts amounts under its identifier columns.
_ID_HEADERS = frozenset(
    "reference ref id identifier clabe code folio description concept date "
    "referencia codigo descripcion concepto fecha".split()
)


def _amount_in_text(expected_type: str, header: str | None, text: str) -> bool:
    """A text slot under an identifier/reference/description/date header holding an amount."""
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", header or "") if not unicodedata.combining(c)
    )
    words = set(re.findall(r"\w+", folded.casefold()))
    return expected_type == "text" and bool(_ID_HEADERS & words) and is_amount(text)


def _amount_note(header: str | None) -> str:
    return (
        f"column {(header or '')[:60]!r} takes an identifier from the same source row, not an "
        "amount; leave it empty if the row has none."
    )


_DIRECTION_CODE = "direction_mismatch"
_TEMPORAL_CODE = "temporal_role_mismatch"


def _labels(value: Any, kind: str, quotes: list[str]) -> list[str]:
    """The label text bound to each place the quotes state the value: the text before it, else
    (nothing but punctuation before it) the text after it."""
    return [b if has_letter(b) else a for b, a in bound_labels(value, kind, quotes)]


def _role_code(field: IRField, value: Any, quotes: list[str]) -> str | None:
    """A reason code when every label bound to the cited number or date states the opposite
    direction of a directional slot, or an opening/prior role a date slot lacks; else None (a
    label stating nothing decides nothing)."""
    names = _slot_names(field)
    bound = _labels(value, field.expected_type, quotes)
    if field.expected_type == "date":
        stated = not _opening(names) and bound and all(_opening((b,)) for b in bound)
        return _TEMPORAL_CODE if stated else None
    if (want := _direction(names)) and bound:
        found = [_direction((b,)) for b in bound]
        return _DIRECTION_CODE if all(f not in (None, want) for f in found) else None
    return None


def _drop_running_balance(
    field: IRField, hits: list[Hit], found: list[tuple[Value, list[Evidence]]]
) -> list[tuple[Value, list[Evidence]]]:
    """An opening slot with a candidate labelled opening/prior (by its words or a carry-forward
    row) drops the candidates that are unlabelled table data cells (running balances). With no
    labelled candidate nothing is dropped; two labelled values stay a conflict."""
    kind = field.expected_type
    if kind not in ("number", "currency") or not _opening((field.label, field.context or "")):
        return found

    def sort(value: Value, evidence: list[Evidence]) -> str:
        cited = {e.chunk_id for e in evidence}
        rows = [
            r
            for h in hits
            if h.chunk_id in cited
            for r in h.rows or []
            if any(value_in_quotes(value, kind, [c]) for c in r.cells.values())
        ]
        bound = _labels(value, kind, [e.quote for e in evidence])
        heads = [h for r in rows for h, c in r.cells.items() if value_in_quotes(value, kind, [c])]
        if any(_opening((b,)) for b in [*bound, *heads]) or any(
            r.row_role == "carry_forward" for r in rows
        ):
            return "opening"
        return "running" if any(r.row_role == "data" for r in rows) else "other"

    sorts = [sort(v, e) for v, e in found]
    if "opening" not in sorts:
        return found
    if "running" in sorts:
        _log_rejected(field.field_id, ["running_balance_cell"])
    return [c for c, k in zip(found, sorts, strict=True) if k != "running"]


_BARE_NUMBER = re.compile(r"[\d\s.,]*\d[\d\s.,]*")
_YEAR = re.compile(r"\d{4}")


def _slot_mismatch(field: IRField, value: Any) -> str | None:
    """The constraint line when a date_period slot got an amount or bare number (a valid date, or
    a bare year, is a period) or an entity_name slot an amount. Other slot types have no
    universal check (the numeric ones are checked by expected_type)."""
    # shortcut: a bare 4-digit number reads as a year, upgrade by checking the source's context.
    # shortcut: boolean has no universal check, upgrade by checking the source's yes/no words.
    kind = slot_type(field)
    if kind not in ("date_period", "entity_name") or not isinstance(value, str):
        return None
    text = value.strip()
    if kind == "date_period" and (_YEAR.fullmatch(text) or _convert(text, "date")):
        return None
    bare = "".join(c for c in text if unicodedata.category(c) != "Sc").strip()
    # a name may be all digits (an id, an account number), so it rejects only an amount
    if is_amount(text) or (kind == "date_period" and _BARE_NUMBER.fullmatch(bare)):
        return SLOT_FORMAT[kind]
    return None


def _rejection(
    field: IRField, hits: list[Hit], value: Any, evidence: list[Evidence]
) -> tuple[str, str] | None:
    """Why the verifier rejects a candidate, as (reason code, note for the retry), else None. The
    code alone is loggable; the note holds the value and goes only to the LLM."""
    shown = repr(str(value)[:40])
    if _convert(value, field.expected_type) is None:
        return "value_unusable", f"{shown} is not a usable {field.expected_type} value."
    if not evidence:
        return "quote_not_in_chunk", (
            f"no quote given for {shown} is copied verbatim from the chunk it cites; copy the "
            "exact characters of that chunk."
        )
    if line := _slot_mismatch(field, value):
        return "slot_type_mismatch", f"{shown} does not fit this slot. {line}"
    if _amount_in_text(field.expected_type, field.context or field.label, str(value)):
        return _AMOUNT_CODE, _amount_note(field.context or field.label)
    if note := _rule_note(field, value, evidence, hits):
        return "slot_rule", note
    quotes = [e.quote for e in evidence]
    if field.expected_type in TYPED_TYPES and not (
        value_in_quotes(value, field.expected_type, quotes)
        or (field.mode == "synthesis" and derived_in_quotes(value, field.expected_type, quotes))
    ):
        kind = "date" if field.expected_type == "date" else "number"
        return f"{kind}_not_in_quote", (
            f"the {kind} {shown} is not written in its quotes; quote the line that states it."
        )
    if field.expected_type in TYPED_TYPES and (
        note := _row_label_note(field, hits, value, evidence)
    ):
        return "row_label_mismatch", note
    if field.expected_type in ("number", "currency", "date") and (
        code := _role_code(field, value, quotes)
    ):
        return code, (
            f"the label next to {shown} in its quote states a "
            f"{'direction' if code == _DIRECTION_CODE else 'opening/prior role'} the slot "
            f"{field.label[:60]!r} does not; quote the number its own label names."
        )
    return None


def _rejections(
    field: IRField, hits: list[Hit], extraction: LLMExtraction[Any]
) -> list[tuple[str, str]]:
    """(code, note) for each candidate `_candidates` drops."""
    found = (
        _rejection(field, hits, c.value, _evidence(hits, c.quotes)) for c in extraction.candidates
    )
    return [r for r in found if r]


def _candidates(
    field: IRField, hits: list[Hit], extraction: LLMExtraction[Any]
) -> list[tuple[Value, list[Evidence]]]:
    """Verified, deduplicated candidates as (value, evidence), LLM order kept."""
    merged: dict[Any, tuple[Value, list[Evidence]]] = {}
    for cand in extraction.candidates:
        evidence = _evidence(hits, cand.quotes)
        converted = _convert(cand.value, field.expected_type)
        if converted is None or _rejection(field, hits, cand.value, evidence):
            continue
        value, key = converted
        known = merged.setdefault(key, (value, []))[1]
        have = {(e.chunk_id, e.quote) for e in known}
        known.extend(e for e in evidence if (e.chunk_id, e.quote) not in have)
    return _drop_running_balance(field, hits, list(merged.values()))


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
    quotes = [e.quote for e in evidence]
    typed = field.expected_type in TYPED_TYPES
    # a computed figure no quote states is flagged for review
    derived = (
        typed
        and field.mode == "synthesis"
        and not value_in_quotes(value, field.expected_type, quotes)
    )
    partial = (
        _is_partial(field, value, extraction.incomplete)
        or not _extractive(field.expected_type, value, quotes)
        or derived
    )
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
_TRAILING_NUMBER = re.compile(r"[\s_#-]*(?<!\d)\d{1,3}$")  # a year ("FY2025") stays


def _group_heading(group: list[list[IRField]]) -> str | None:
    """The one context all the group's fields share, if any."""
    first = group[0][0]
    shared = len({f.context for row in group for f in row}) == 1
    return bounded_field(first).context if first.context and shared else None


_DIRECTION_HINT = {
    "CR": "CR incoming credits deposits receipts",
    "DR": "DR outgoing debits withdrawals payments",
}


def _group_query(group: list[list[IRField]]) -> str:
    first = group[0][0]
    if first.expected_type in _ROW_TYPES:
        return field_query(first)
    # a generic slot ("Value 1") repeats per row: its number is dropped, the repeats collapse
    labels = [bounded_field(f).label for row in group for f in row]
    names = [_TRAILING_NUMBER.sub("", label) or label for label in labels]
    joined = " ".join(dict.fromkeys(names))
    query = f"{joined[:_MAX_QUERY_LABELS]} ({' > '.join(bounded_field(first).section_path)})"
    names = tuple(n for f in _flat(group) for n in (*f.section_path, f.field_id, f.context or ""))
    if hint := _DIRECTION_HINT.get(_direction((*labels, *names)) or ""):
        query += f" {hint}"  # paired grids (receipts / payments) on one slide must not collide
    heading = _group_heading(group)
    return f"{heading}: {query}" if heading else query


async def map_group(
    ctx: TenantContext,
    group: list[list[IRField]],
    document_ids: list[uuid.UUID],
    retriever: Retriever,
    llm: LLMClient,
    other_headings: Sequence[str] = (),
) -> dict[str, FieldMappingResult]:
    """One retrieval and one structured call for all rows of a group, verified row by row."""
    hits = await _retrieve(ctx, _group_query(group), document_ids, retriever, _group_heading(group))
    if not hits:
        return {f.field_id: _missing(f, "no_relevant_chunks", document_ids) for f in _flat(group)}
    notes: list[str] = []
    first = await _group_pass(llm, group, hits, document_ids, other_headings, notes=notes)
    return await _escalated_group(llm, group, hits, first, document_ids, other_headings, notes)


def _flat(group: list[list[IRField]]) -> list[IRField]:
    return [f for row in group for f in row]


async def _group_pass(
    llm: LLMClient,
    group: list[list[IRField]],
    hits: list[Hit],
    document_ids: list[uuid.UUID],
    other_headings: Sequence[str],
    think_budget: int | None = None,
    notes: list[str] | None = None,
) -> dict[str, FieldMappingResult]:
    """The group's structured call and its row-by-row verification (`think_budget`: escalated).
    `notes` in: why the first pass's cells were rejected; out (first pass): the same, collected."""
    retry, rejected = (notes, None) if think_budget is not None else (None, notes)

    def all_missing(reason: MissingReason) -> dict[str, FieldMappingResult]:
        return {f.field_id: _missing(f, reason, document_ids) for f in _flat(group)}

    first = group[0][0]
    try:  # a transport LLMError propagates: the worker retries
        if first.expected_type in _ROW_TYPES:
            rows = await extract_rows(llm, first, hits, other_headings, think_budget, retry)
            if first.expected_type == "table":
                rows = rows.model_copy(
                    update={
                        "rows": await _own_rows(llm, first.context, other_headings, hits, rows.rows)
                    }
                )
            if rows.not_found or not rows.rows:
                _log_short("rows", 1, 0, hits, rows.not_found)
                return all_missing("not_found_by_llm")
            if first.expected_type == "chart":
                return {first.field_id: _chart_result(first, hits, rows, document_ids)}
            return {first.field_id: _table_result(first, hits, rows, document_ids, rejected)}
        records = await extract_records(llm, group, hits, other_headings, think_budget, retry)
        # shortcut: a list or grid is checked only beside sibling headings, add the check for a
        # lone heading when direction slips show up there; a keyed card (one row) is checked too.
        if other_headings and (len(group) == 1 or not _keyed(group)):
            head = next((f.context for f in _flat(group) if has_letter(f.context)), None)
            kept = await _own_rows(llm, head, other_headings, hits, records.records)
            records = records.model_copy(update={"records": kept})
    except LLMInvalidOutput:
        return all_missing("llm_error")
    returned = len({r.n for r in records.records if 1 <= r.n <= len(group)})
    if returned < len(group):
        _log_short("records", len(group), returned, hits, records.not_found)
    is_list = all(len(row) == 1 for row in group)
    if records.not_found or not records.records:
        if not (is_list and _prose(first)):
            return all_missing("not_found_by_llm")
        results = all_missing("not_found_by_llm")  # prose list slots: the synthesis fallback
    else:
        results = _record_results(group, hits, records, document_ids, rejected)
    if is_list:  # a list: duplicates collapse, gaps may be synthesized
        return await _list_results(llm, group, hits, results, document_ids)
    return results


def _filled_grid(
    group: list[list[IRField]], results: dict[str, FieldMappingResult]
) -> list[list[bool]]:
    """Which cells a group filled, row by row: a table's or chart's value cells, else its slots."""
    first = group[0][0]
    if first.expected_type in _ROW_TYPES:
        r = results[first.field_id]
        rows = r.value if r.status in ("populated", "partial") and isinstance(r.value, list) else []
        return [[bool(str(c).strip()) for c in row] for row in rows]
    return [[results[f.field_id].status in ("populated", "partial") for f in row] for row in group]


def _group_reason(
    group: list[list[IRField]], results: dict[str, FieldMappingResult], hits: list[Hit]
) -> str | None:
    """Why a group's first pass earns one thinking retry: a conflict or a verification failure,
    or with a chunk scored >= the threshold, nothing filled or a column empty in every filled
    row (a `ratings` column the fast pass dropped)."""
    rs = [results[f.field_id] for f in _flat(group)]
    if any(r.status == "conflict" for r in rs):
        return "conflict"
    if any(r.missing_reason == "evidence_failed_verification" for r in rs):
        return "verification_failed"
    if not any(h.score >= get_settings().think_escalation_score_threshold for h in hits):
        return None
    filled = [row for row in _filled_grid(group, results) if any(row)]
    if not filled:
        empty = all(r.missing_reason == "not_found_by_llm" for r in rs)
        synthesis = all(f.mode == "synthesis" for f in _flat(group))  # no verbatim value expected
        return "high_score_miss" if empty and not synthesis else None
    # rows of a tile group may differ in length: compare the columns all filled rows have
    return "missing_column" if any(not any(c) for c in zip(*filled, strict=False)) else None


def _group_status(results: list[FieldMappingResult]) -> str:
    statuses = {r.status for r in results}
    return statuses.pop() if len(statuses) == 1 else "partial"


async def _escalated_group(
    llm: LLMClient,
    group: list[list[IRField]],
    hits: list[Hit],
    first: dict[str, FieldMappingResult],
    docs: list[uuid.UUID],
    other_headings: Sequence[str],
    notes: list[str],
) -> dict[str, FieldMappingResult]:
    """`_escalated` for a table, chart or record group: one more pass, thinking on, kept whole
    only if it fills more cells than the first, so a miss or an LLM error keeps the first. The
    first pass's rejection `notes` go to that pass."""
    s = get_settings()
    reason = _group_reason(group, first, hits) or (_AMOUNT_CODE if notes else None)
    if not s.think_on_escalation or reason is None:
        return first
    start, after, error = time.monotonic(), first, None
    try:
        budget = s.think_escalation_max_tokens
        after = await _group_pass(llm, group, hits, docs, other_headings, budget, notes)
    except LLMError as exc:  # best effort, as for one slot
        error = type(exc).__name__
    more = sum(map(sum, _filled_grid(group, after))) > sum(map(sum, _filled_grid(group, first)))
    kept = after if more else first  # the whole group: rows never mix the two passes
    fields = _flat(group)
    _log_escalation(
        fields[0].field_id,
        reason,
        _group_status([first[f.field_id] for f in fields]),
        _group_status([kept[f.field_id] for f in fields]),
        start,
        error,
    )
    return kept


async def _own_rows[R: (LLMRow, LLMRecord)](
    llm: LLMClient, context: str | None, others: Sequence[str], hits: list[Hit], rows: list[R]
) -> list[R]:
    """The verified rows minus those the row check judges to belong to the opposite direction or a
    sibling heading. `unknown`, or an unusable check, keeps the row: no recall lost on doubt."""
    if not has_letter(context) or not rows:
        return rows
    items = [(r.cells(), " ... ".join(e.quote for e in _evidence(hits, r.quotes))) for r in rows]
    try:
        keep = await check_rows(llm, context or "", others, items)
    except LLMInvalidOutput:
        return rows
    return [r for r, k in zip(rows, keep, strict=True) if k]


def _log_short(kind: str, rows: int, returned: int, hits: list[Hit], not_found: bool) -> None:
    """Why a group came back short, as counts only (no labels, values or quotes)."""
    log.info(
        "group_short", kind=kind, rows=rows, returned=returned, hits=len(hits), not_found=not_found
    )


def _supports(expected_type: str, cell: str, quote: str) -> bool:
    if expected_type in TYPED_TYPES:
        return _convert(cell, expected_type) is not None and value_in_quotes(
            cell, expected_type, [quote]
        )
    return normalize(cell) in normalize(quote)


def _mixed_rows(
    types: list[str], cells: list[str], hits: Sequence[Hit], evidence: list[Evidence]
) -> dict[str, set[int]] | None:
    """`{source row id: indexes of the non-empty cells it states}` when every non-empty cell is
    stated by some row of the cited chunks but no single row states them all (a row mixed from
    several source rows); None when one row does, or a cell is in no structured row (unknown)."""
    # shortcut: a wrapped cell Docling puts in its own row (other cells empty) makes a correct
    # template row look mixed and drops it; merge continuation rows in the parser if seen live.
    cited = {e.chunk_id for e in evidence}
    rows = [r for h in hits if h.chunk_id in cited for r in h.rows or []]
    wanted = {j for j, c in enumerate(cells) if c}
    by_row = {
        r.id: {j for j in wanted if any(_supports(types[j], cells[j], v) for v in r.cells.values())}
        for r in rows
    }
    covered = set().union(*by_row.values()) if by_row else set()
    return by_row if wanted and covered == wanted and wanted not in by_row.values() else None


def _verified_row(
    types: list[str], hits: list[Hit], row: LLMRow | LLMRecord
) -> tuple[list[str], list[Evidence], list[list[bool]], bool]:
    """A row's cells, verified evidence, `support[cell][quote]`, and whether ONE quote supports
    every non-empty cell (the row is one source record)."""
    cells = [c.strip() for c in (row.cells() + [""] * len(types))[: len(types)]]
    evidence = _evidence(hits, row.quotes)
    support = [
        [bool(c) and _supports(t, c, e.quote) for e in evidence]
        for c, t in zip(cells, types, strict=True)
    ]
    anchored = any(cells) and any(
        all(support[j][i] for j, c in enumerate(cells) if c) for i in range(len(evidence))
    )
    return cells, evidence, support, anchored


def _cited_lines(hits: list[Hit], evidence: list[Evidence]) -> list[Evidence]:
    """Every line of the chunks a record cites, each a verbatim quote: a small model cites the
    right chunk but copies record 1's row into every record."""
    cited = {e.chunk_id for e in evidence}
    return _evidence(
        hits,
        [
            LLMQuote(chunk_id=str(h.chunk_id), quote=line.strip())
            for h in hits
            if h.chunk_id in cited
            for line in h.text.splitlines()
        ],
    )


def _same_span(a: Evidence, b: Evidence) -> bool:
    x, y = normalize(a.quote), normalize(b.quote)
    return a.chunk_id == b.chunk_id and (x in y or y in x)


_Row = tuple[list[str], list[Evidence], list[list[bool]], int | None]


def _stray_cells(
    types: list[str], cells: list[str], hits: Sequence[Hit], spans: list[Evidence]
) -> set[int]:
    """For a record mixed from several source rows (`_mixed_rows`), the cells the best row does
    not state: the row stating most cells, then the one stating the key (first non-empty) cell,
    then the first id. Else none."""
    by_row = _mixed_rows(types, cells, hits, spans)
    if by_row is None:
        return set()
    key = next(j for j, c in enumerate(cells) if c)
    best = min(by_row, key=lambda r: (-len(by_row[r]), key not in by_row[r], r))
    return {j for j, c in enumerate(cells) if c and j not in by_row[best]}


def _record_row(types: list[str], hits: list[Hit], record: LLMRecord | None) -> _Row:
    """A record's cells, its spans (verified quotes, then cited-chunk lines stating >= 2 of its
    cells), `support[span][cell]`, and its anchor: the span stating its first non-empty cell (its
    key), most cells first. Code only finds a line, every value is still checked against it."""
    if record is None:
        return [""] * len(types), [], [], None
    cells, evidence, _, _ = _verified_row(types, hits, record)

    def states(quote: str) -> list[bool]:
        return [bool(c) and _supports(t, c, quote) for c, t in zip(cells, types, strict=True)]

    seen = {(e.chunk_id, e.quote) for e in evidence}
    lines = [
        e
        for e in _cited_lines(hits, evidence)
        if (e.chunk_id, e.quote) not in seen and sum(states(e.quote)) >= 2
    ]
    spans = evidence + lines
    support = [states(s.quote) for s in spans]
    key = next((j for j, c in enumerate(cells) if c), 0)
    best = max(range(len(spans)), key=lambda i: (support[i][key], sum(support[i])), default=None)
    return cells, spans, support, best if best is not None and any(support[best]) else None


def _cell_types(row_fields: list[IRField], record: LLMRecord | None) -> list[str]:
    """Each slot's type for verification. A table cell the IR typed number, currency or percent
    that the record states as a word with no digit ("Preferred" under Rating) is checked as
    verbatim text: a column's type is the IR model's guess. A card slot and a date keep their type
    (a date is stored ISO, a word there would break the xlsx serial)."""
    cells = record.cells() if record else []
    return [
        "text"
        if f.write_mode == "fill_cell"
        and f.expected_type in _NUMERIC
        and j < len(cells)
        and has_letter(cells[j])
        and not any(ch.isdigit() for ch in cells[j])
        else f.expected_type
        for j, f in enumerate(row_fields)
    ]


_UNIT_WORD = re.compile(r"[^\W\d_]{2,}")
_MAGNITUDES = ("thousand", "million", "billion")
_NUMERIC = ("number", "currency", "percent")


def _restates(text: str, numbers: set[Any]) -> bool:
    """A caption that is only a sibling's number with its currency, unit or magnitude ("AED
    182.4M", "4.6 days", "91%"), not a measure's name ("Top 5 suppliers" names one)."""
    words = [w for w in _UNIT_WORD.findall(text) if w.casefold() not in _MAGNITUDES]
    return canonical_value(text, "number") in numbers and len(words) <= 1


def _record_results(
    group: list[list[IRField]],
    hits: list[Hit],
    records: LLMRecords,
    docs: list[uuid.UUID],
    rejected: list[str] | None = None,
) -> dict[str, FieldMappingResult]:
    """Record n fills slot row n. A cell its anchor states is kept; a cell only another record's
    anchor states is that record's value shifted in, so it is dropped; any other cell that is
    stated elsewhere is kept but makes its row partial, as does an unsupported cell. A card's text
    slot that only restates the card's number is a caption slip: missing, the row unaffected."""
    by_n: dict[int, LLMRecord] = {}
    for record in records.records:
        by_n.setdefault(record.n, record)  # the first of a repeated n wins, other n drop
    types = [_cell_types(row_fields, by_n.get(i + 1)) for i, row_fields in enumerate(group)]
    rows = [_record_row(types[i], hits, by_n.get(i + 1)) for i in range(len(group))]
    anchors = [(i, r[1][r[3]]) for i, r in enumerate(rows) if r[3] is not None]
    results = {}
    for i, (row_fields, (cells, spans, support, anchor)) in enumerate(
        zip(group, rows, strict=True)
    ):
        others = [a for k, a in anchors if k != i]
        kept: dict[int, tuple[Value, list[int]]] = {}
        loose = False
        # a keyed card is a one-row group whose slots may come from different source rows
        stray = _stray_cells(types[i], cells, hits, spans) if len(group) > 1 else set()
        for j, field in enumerate(row_fields):
            by = [s for s in range(len(spans)) if support[s][j]]
            value = _cell_value(cells[j], types[i][j]) if by and j not in stray else None
            if value and _amount_in_text(types[i][j], field.context or field.label, cells[j]):
                value = None  # an unsupported cell: an amount under an identifier header
                _log_rejected(field.field_id, [_AMOUNT_CODE])
                if rejected is not None:
                    rejected.append(_amount_note(field.context or field.label))
            if not cells[j]:
                results[field.field_id] = _missing(field, "not_found_by_llm", docs)
            elif value is None or (
                anchor not in by and all(any(_same_span(spans[s], o) for o in others) for s in by)
            ):
                loose |= value is None  # an unsupported cell; a shifted one is just dropped
                results[field.field_id] = _missing(field, "evidence_failed_verification", docs)
            elif any(normalize(str(v)) == normalize(str(value[0])) for v, _ in kept.values()):
                # one source value never fills two slots of one tile (a value and its caption)
                results[field.field_id] = _missing(field, "evidence_failed_verification", docs)
            else:
                kept[j] = (value[0], by)
                loose |= anchor not in by
        numbers = {v for j, (v, _) in kept.items() if types[i][j] in _NUMERIC}
        for j in [j for j, (v, _) in kept.items() if row_fields[j].card and types[i][j] == "text"]:
            if _restates(str(kept[j][0]), numbers):
                del kept[j]
                results[row_fields[j].field_id] = _missing(
                    row_fields[j], "evidence_failed_verification", docs
                )
        used = sorted({s for _, by in kept.values() for s in by}, key=lambda s: s != anchor)
        evidence = [spans[s] for s in used]
        anchored = bool(kept) and not loose
        for j, (value, _) in kept.items():
            field = row_fields[j]
            typed = types[i][j] in TYPED_TYPES
            results[field.field_id] = FieldMappingResult(
                field_id=field.field_id,
                status="populated" if anchored else "partial",
                value=value,
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
    slots empty, the source may state the items in prose, so `synthesize` may fill them. Prose
    slots (`_prose`) also keep a weakly backed item (`_backing`), and its slot is `partial`."""
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
    items: list[tuple[str, list[Evidence], bool]] = []
    seen = set()
    loose = _prose(slots[0])  # prose slots keep weakly backed items, as partial
    for cand in draft.items:
        quotes = _evidence(hits, cand.quotes)
        text = _BULLET.sub("", cand.value.strip())
        backing = _backing(text, [e.quote for e in quotes], loose)
        if backing and normalize(text) not in seen:
            seen.add(normalize(text))
            items.append((text, quotes, backing == "weak"))
    items = items[: len(slots)]
    if len(items) <= have:
        return collapsed
    filled = {}
    for k, f in enumerate(slots):
        if k >= len(items):
            filled[f.field_id] = _missing(f, "not_found_by_llm", docs)
            continue
        text, quotes, weak = items[k]
        if weak and collapsed[f.field_id].status == "populated":
            filled[f.field_id] = collapsed[
                f.field_id
            ]  # a weak item never replaces a verbatim value
            continue
        filled[f.field_id] = FieldMappingResult(
            field_id=f.field_id,
            status="partial" if weak else "populated",
            value=[text] if f.expected_type in _LISTS else text,
            evidence=[e.model_copy(update={"item": 0}) for e in quotes],  # summarised: item 0
            confidence=CONF_PARTIAL if weak else CONF_SYNTH,
            missing_reason="insufficient_evidence" if weak else None,
            checked_documents=docs,
            conflict_details=None,
            provenance="rag",
        )
    return filled


def _table_result(
    field: IRField,
    hits: list[Hit],
    rows: LLMRows,
    docs: list[uuid.UUID],
    rejected: list[str] | None = None,
) -> FieldMappingResult:
    """The rows whose every cell a quote states; unverified rows stay out of the table."""
    types = ["text"] * len(field.constraints.columns or rows.rows[0].cells())
    llm_rows = rows.rows[: field.constraints.max_items]
    columns = field.constraints.columns or []
    kept, evidence, all_anchored, mixed = [], [], True, 0
    for cells, ev, support, anchored in (_verified_row(types, hits, r) for r in llm_rows):
        amounts = [h for h, c in zip(columns, cells, strict=False) if _amount_in_text("text", h, c)]
        if amounts:  # an unsupported cell: the row stays out, as any unverified row does
            _log_rejected(field.field_id, [_AMOUNT_CODE] * len(amounts))
            if rejected is not None:
                rejected.extend(_amount_note(h) for h in amounts)
            continue
        if ev and any(cells) and all(not c or any(s) for c, s in zip(cells, support, strict=True)):
            if _mixed_rows(types, cells, hits, ev) is not None:
                mixed += 1  # its cells come from different source rows
                continue
            kept.append(cells)
            evidence += [
                e
                for e in ev
                if (e.chunk_id, e.quote) not in {(x.chunk_id, x.quote) for x in evidence}
            ]
            all_anchored &= anchored
    if mixed:
        _log_rejected(field.field_id, ["row_mixed"] * mixed)
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
    names = [r.cells()[0].strip() for r in llm_rows if r.value and r.cells()[0].strip()]
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


_ROLE_FILLER = re.compile(r"(\s+(value|amount|valor|importe|monto))+$")


def _role_key(label: str) -> str:
    """The label without trailing filler words: "Total deposits value" and "Total Deposits"
    name one role, "Value date" and "Date" two."""
    return _ROLE_FILLER.sub("", normalize(label)) or normalize(label)


async def _check_slot_roles(
    llm: LLMClient,
    fields: list[IRField],
    results: dict[str, FieldMappingResult],
    shared: dict[str, tuple[str, str, str]],
    only: set[str] | None = None,
) -> dict[str, tuple[FieldMappingResult, str]]:
    """Slots of one text box, or typed slots of the deck holding one number, are different
    roles, but the per-field check cannot see the other slots, so one value filled into several
    of them is checked once, together. A slot the quotes do not state, or any slot when the check
    is unusable, is never populated. `only`: check just the groups holding one of these fields.
    Returns each slot the check rejected (not its shared copies, not on an unusable check) with
    its result before and the reason for its retry."""
    boxes: dict[tuple[Any, ...], list[IRField]] = {}
    for f in fields:
        r = results[f.field_id]
        if (
            f.write_mode == "replace_token"
            and r.status in ("populated", "partial")
            and r.provenance == "rag"
            and r.value is not None
            and f.mode != "derived"  # computed from rows, not a value one quote states
        ):
            if f.expected_type == "text" and isinstance(r.value, str):
                boxes.setdefault(("text", f.anchor_id, normalize(r.value)), []).append(f)
            elif f.expected_type in TYPED_TYPES:
                # deck-wide: "$ 402.031,45" and 402031.45 are one number whatever the slide
                n = canonical_value(r.value, f.expected_type)
                # shortcut: dates stay per slide (a date repeats across slides and row-grid
                # slots would be judged against each other), widen if a date role is misfiled.
                key = (
                    (n, None)
                    if n is not None and f.expected_type != "date"
                    else (
                        normalize(str(r.value)),
                        _unit(f),
                    )
                )
                boxes.setdefault(("typed", *key), []).append(f)
    demoted: dict[str, MissingReason] = {}
    rejected: dict[str, tuple[FieldMappingResult, str]] = {}
    for group in boxes.values():
        if len(group) < 2 or len({_role_key(f.label) for f in group}) < 2:
            continue
        if only is not None and not any(f.field_id in only for f in group):
            continue
        evidence = list(
            {(e.chunk_id, e.quote): e for f in group for e in results[f.field_id].evidence}.values()
        )
        reason: MissingReason = "evidence_failed_verification"
        try:
            keep = await check_roles(llm, group, str(results[group[0].field_id].value), evidence)
        except LLMInvalidOutput:  # a transport LLMError propagates: the worker retries
            keep, reason = set(), "llm_error"
        demoted |= {f.field_id: reason for f in group if f.field_id not in keep}
        if reason == "evidence_failed_verification":
            value = repr(str(results[group[0].field_id].value)[:40])
            roles = [bounded_field(f).label for f in group if f.field_id in keep]
            for f in group:
                if f.field_id not in keep:
                    held = f"is the value of {', '.join(roles)}" if roles else "is not stated"
                    note = f"slot_role: {value} {held}, not of {bounded_field(f).label}."
                    rejected[f.field_id] = (results[f.field_id], note)
    for fid, reason in list(demoted.items()):  # copies of a demoted fact elsewhere
        if key := shared.get(fid):
            demoted |= {
                g: reason
                for g, k in shared.items()
                if k == key and results[g].status in ("populated", "partial")
            }
    for f in fields:
        if reason := demoted.get(f.field_id):
            results[f.field_id] = _missing(f, reason, results[f.field_id].checked_documents)
    return rejected


def _echo(value: Value | None, evidence: list[Evidence]) -> LLMExtraction[Any]:
    """A result as the extraction reply that gave it, echoed back in a retry."""
    quotes = [LLMQuote(chunk_id=str(e.chunk_id), quote=e.quote) for e in evidence]
    return LLMExtraction[Any](
        candidates=[LLMCandidate[Any](value=str(value), quotes=quotes)],
        not_found=False,
        incomplete=False,
    )


MAX_BATCH_SLOTS = 12


def _batch_tokens() -> int:
    s = get_settings()
    return min(s.retrieval_context_tokens, prompt_tokens(s.llm_num_ctx) - PROMPT_OVERHEAD_TOKENS)


BATCH_FIELD_HITS = 4  # ranked hits per slot of a batch (the 2 neighbor chunks each had before)


async def _retrieve_batch(
    ctx: TenantContext,
    fields: list[IRField],
    document_ids: list[uuid.UUID],
    retriever: Retriever,
    opening: bool,
) -> list[Hit]:
    """One context for a unit's slots: each slot's own top hits and each distinct heading's top
    hit, merged by rank, then cut by tokens. A source's opening chunks (title, author, date) come
    first on the opening unit. No unscored neighbor chunks: `curate` would drop them."""
    headings = list(
        dict.fromkeys(h for f in fields if has_letter(h := bounded_field(f).context) and h)
    )
    searches = [(field_query(f), BATCH_FIELD_HITS) for f in fields] + [(h, 1) for h in headings]
    ranked = await asyncio.gather(
        *(retriever.search(ctx, q, document_ids=document_ids, top_n=n) for q, n in searches)
    )
    merged: list[Hit] = []
    seen: set[uuid.UUID] = set()
    for rank in range(max(map(len, ranked), default=0)):
        for found in ranked:
            if rank < len(found) and found[rank].chunk_id not in seen:
                seen.add(found[rank].chunk_id)
                merged.append(found[rank])
    first = await retriever.opening(ctx, document_ids) if opening else []
    if not merged:
        return select_context(first, _batch_tokens()) if first else []
    lead = {h.chunk_id for h in first}
    context = [*first, *(h for h in merged if h.chunk_id not in lead)]
    return select_context(context, _batch_tokens(), pinned=len(first))


def _slot_extraction(field: IRField, entry: LLMSlotValue | None) -> LLMExtraction[Any]:
    """The batch entry as a single-field extraction: a list type takes a list (a string is one
    item), a scalar type a string (a list candidate is dropped, never joined)."""
    if entry is None:
        return LLMExtraction[Any](candidates=[], not_found=True, incomplete=False)
    as_list = field.expected_type in _LISTS
    candidates = []
    for cand in entry.candidates:
        value = cand.value
        if as_list and isinstance(value, str):
            value = [value]
        if isinstance(value, list) == as_list:
            candidates.append(LLMCandidate[Any](value=value, quotes=cand.quotes))
    return LLMExtraction[Any](
        candidates=candidates, not_found=entry.not_found, incomplete=entry.incomplete
    )


async def map_batch(
    ctx: TenantContext,
    fields: list[IRField],
    document_ids: list[uuid.UUID],
    retriever: Retriever,
    llm: LLMClient,
    siblings: dict[str, list[str]],
    opening: bool = False,
) -> dict[str, FieldMappingResult]:
    """The loose fields of one unit in one retrieval and one extraction call, each verified alone
    through `resolve`. A field that breaks its slot rules is mapped alone again (`map_field`), as
    is every field when the reply is invalid; one value under two headings is checked."""
    hits = await _retrieve_batch(ctx, fields, document_ids, retriever, opening)
    if not hits:
        return {f.field_id: _missing(f, "no_relevant_chunks", document_ids) for f in fields}

    async def alone(f: IRField) -> FieldMappingResult:
        return await map_field(ctx, f, document_ids, retriever, llm, siblings.get(f.field_id, ()))

    try:  # a transport LLMError propagates: the worker retries
        values = await extract_batch(llm, fields, hits)
    except LLMInvalidOutput:
        return {f.field_id: await alone(f) for f in fields}
    by_n: dict[int, LLMSlotValue] = {}
    for entry in values.slots:
        by_n.setdefault(entry.n, entry)  # the first of a repeated n wins, other n drop
    results: dict[str, FieldMappingResult] = {}
    extractions: dict[str, LLMExtraction[Any]] = {}
    for n, f in enumerate(fields, start=1):
        extraction = _slot_extraction(f, by_n.get(n))
        if _rule_notes(f, hits, extraction):
            results[f.field_id] = await alone(f)
            continue
        extractions[f.field_id] = extraction
        result = resolve(f, hits, extraction, checked_documents=document_ids)
        results[f.field_id] = await _filled_by_synthesis(llm, f, hits, result, document_ids)
    same: dict[str, list[IRField]] = {}
    for f in fields:
        r = results[f.field_id]
        if f.field_id in extractions and r.status in ("populated", "partial"):
            if isinstance(r.value, str) and has_letter(f.context):
                same.setdefault(normalize(r.value), []).append(f)
    for twins in same.values():
        if len({f.context for f in twins}) < 2:
            continue
        for f in twins:
            try:
                checked = await check_candidates(
                    llm, f, hits, extractions[f.field_id], siblings.get(f.field_id, ())
                )
            except LLMInvalidOutput:
                results[f.field_id] = _missing(f, "llm_error", document_ids)
                continue
            if checked.not_found:
                results[f.field_id] = _missing(f, "not_found_by_llm", document_ids)
    for f in fields:  # fields mapped alone escalated in map_field already
        if f.field_id in extractions:
            first = results[f.field_id]
            sibs = siblings.get(f.field_id, ())
            extraction = extractions[f.field_id]
            after = await _escalated(llm, f, hits, first, document_ids, sibs, extraction)
            results[f.field_id] = await _paraphrased(llm, f, hits, after, document_ids)
    return results


async def map_fields(
    ctx: TenantContext,
    fields: list[IRField],
    document_ids: list[uuid.UUID],
    retriever: Retriever,
    llm: LLMClient,
    on_field: Callable[[int, str], Awaitable[None]] | None = None,
) -> dict[str, FieldMappingResult]:
    """`_map_fields` with a fresh `_ESCALATED` memo for the run."""
    token = _ESCALATED.set(set())
    try:
        return await _map_fields(ctx, fields, document_ids, retriever, llm, on_field)
    finally:
        _ESCALATED.reset(token)


async def _map_fields(
    ctx: TenantContext,
    fields: list[IRField],
    document_ids: list[uuid.UUID],
    retriever: Retriever,
    llm: LLMClient,
    on_field: Callable[[int, str], Awaitable[None]] | None = None,
) -> dict[str, FieldMappingResult]:
    """Map a whole IR: one call per row group, one per unit's loose fields (at most
    MAX_BATCH_SLOTS, `map_batch`), one per lone loose field and per fact shared across slides.
    `on_field(done, field_id)` runs before each unit with its first field, and
    `on_field(done, "")` after it. A group or batch is mapped at its first field in the IR."""
    # a derived slot is mapped alone by `map_field`: never in a group, a batch or a shared fact
    plain = [f for f in fields if f.mode != "derived"]
    ranked = _ranking_slots(fields)
    shared_binding = _Shared(series=_series(fields, ranked))
    groups = {f.field_id: g for g in row_groups(plain) for row in g for f in row}
    shared = shared_facts(fields)
    siblings = sibling_headings(fields)
    loose: dict[object, list[IRField]] = {}
    for f in fields:  # a native table's cells are their own batch, apart from the text slots
        # a synthesis slot is mapped alone: its own retrieval and a synthesis call
        if (
            f.field_id not in groups
            and f.expected_type not in _ROW_TYPES
            and f.mode not in ("synthesis", "derived")
        ):
            table = m[1] if (m := _CELL.match(f.anchor_id)) else None
            loose.setdefault((_unit(f), table), []).append(f)
    batches = {
        chunk[0].field_id: chunk
        for unit in loose.values()
        for chunk in (unit[i : i + MAX_BATCH_SLOTS] for i in range(0, len(unit), MAX_BATCH_SLOTS))
    }
    opening = _unit(fields[0]) if fields else None
    results: dict[str, FieldMappingResult] = {}
    by_fact: dict[tuple[str, str, str], FieldMappingResult] = {}
    for field in fields:
        if field.field_id in results:
            continue
        if on_field:
            await on_field(len(results), field.field_id)  # template-derived: progress only
        if field.mode == "derived":
            results[field.field_id] = await map_field(
                ctx,
                field,
                document_ids,
                retriever,
                llm,
                rank=_slot_rank(field, ranked),
                shared=shared_binding,
            )
        elif field.field_id in groups:
            results |= await map_group(
                ctx,
                groups[field.field_id],
                document_ids,
                retriever,
                llm,
                siblings.get(field.field_id, ()),
            )
        else:
            batch = batches.get(field.field_id, [field])
            todo = []
            for f in batch:  # a fact already mapped on another slide is copied, not asked again
                if (key := shared.get(f.field_id)) and key in by_fact:
                    results[f.field_id] = by_fact[key].model_copy(update={"field_id": f.field_id})
                else:
                    todo.append(f)
            first_unit = bool(todo) and _unit(todo[0]) == opening
            if len(todo) == 1 and (not first_unit or todo[0].mode == "synthesis"):
                results[todo[0].field_id] = await map_field(
                    ctx, todo[0], document_ids, retriever, llm, siblings.get(todo[0].field_id, ())
                )
            elif todo:
                results |= await map_batch(
                    ctx, todo, document_ids, retriever, llm, siblings, first_unit
                )
            for f in todo:
                if key := shared.get(f.field_id):
                    by_fact[key] = results[f.field_id]
        if on_field:
            await on_field(len(results), "")
    _check_ranks(fields, results, ranked)
    rejected = await _check_slot_roles(llm, fields, results, shared)
    ungrouped = [f for f in fields if f.field_id in rejected and f.field_id not in groups]
    await _retry_roles(ctx, ungrouped, fields, rejected, document_ids, retriever, llm, results)
    return {f.field_id: results[f.field_id] for f in fields}


def _slot_rank(field: IRField, ranked: set[str]) -> int | None:
    """The slot number of a ranking slot (`receipt_date_2`, as the IR builder's rule finds them:
    a `_2` copy of another derived field is not one), else None."""
    m = SLOT_NUMBER.search(field.field_id)
    return int(m[1]) if m and field.mode == "derived" and field.field_id in ranked else None


def _residual(field: IRField) -> frozenset[str]:
    """The content words of a slot's label after the direction, total, net, amount and number
    words: "Total money in" and "Total deposits" say the same, "Total card payments" says more
    than "Total payments"."""
    text = _padded(field.label)
    for phrase in (" money in ", " money out ", *_NET_TERMS):
        text = text.replace(phrase, " ")
    skip = _IN_WORDS | _OUT_WORDS | _TOTAL_WORDS | _AMOUNT_WORDS
    return frozenset(w for w in text.split() if w not in skip and not w.isdigit())


def _series(fields: list[IRField], ranked: set[str]) -> dict[str, tuple[IRField, Intent]]:
    """field_id -> (lead field, intent) for the ranked slots bound by the IR's own derivation
    nowhere, grouped by slide and field_id first word: when any slot of the series has a
    `group_sum` intent, all of them (the amount sibling names no group) bind one group_sum, with
    the first such slot's direction, the lead being its first slot."""
    series: dict[tuple[object, str], list[IRField]] = {}
    for f in fields:
        i = derivation_intent(f)
        if (
            f.mode == "derived"
            and f.derivation is None
            and _slot_rank(f, ranked)
            and f.expected_type != "date"
            and (i is None or i[0] == "group_sum")  # a `top` slot picks its own row and cell
        ):
            series.setdefault((_unit(f), f.field_id.split("_")[0]), []).append(f)
    out: dict[str, tuple[IRField, Intent]] = {}
    for members in series.values():
        intents = [(f, i) for f in members if (i := derivation_intent(f)) and i[0] == "group_sum"]
        if intents:
            tag = next((i[1] for _, i in intents if i[1]), None)
            out |= {f.field_id: (intents[0][0], ("group_sum", tag)) for f in members}
    return out


def _check_ranks(
    fields: list[IRField], results: dict[str, FieldMappingResult], ranked: set[str]
) -> None:
    """Every populated cell of one ranked record (same slide, field_id prefix word and number)
    must cite the same rows, else the whole record is missing: a binding that disagrees on the
    ranking would mix the cells of different rows."""

    def demote(cells: list[IRField]) -> None:
        log.info("derived_rejected", slot_id=cells[0].field_id, code="rank_mixed")
        for f in cells:
            results[f.field_id] = _missing(
                f, "evidence_failed_verification", results[f.field_id].checked_documents
            )

    records: dict[tuple[object, str, int], list[IRField]] = {}
    for f in fields:
        if (n := _slot_rank(f, ranked)) and results[f.field_id].status == "populated":
            records.setdefault((_unit(f), f.field_id.split("_")[0], n), []).append(f)
    for cells in records.values():
        cited = {frozenset(e.row_id for e in results[f.field_id].evidence) for f in cells}
        if len(cited) > 1:
            demote(cells)
    # distinct ranks of one grid never cite the same rows: that is a binding that ignores rank
    seen: dict[tuple[object, str], dict[frozenset[str | None], list[IRField]]] = {}
    for (unit, word, _), cells in records.items():
        if results[cells[0].field_id].status == "populated":  # not demoted by the first pass
            rows = frozenset(e.row_id for f in cells for e in results[f.field_id].evidence)
            seen.setdefault((unit, word), {}).setdefault(rows, []).extend(cells)
    for by_rows in seen.values():
        for cells in by_rows.values():
            if len({_slot_rank(f, ranked) for f in cells}) > 1:
                demote(cells)


async def _retry_roles(
    ctx: TenantContext,
    todo: list[IRField],
    fields: list[IRField],
    rejected: dict[str, tuple[FieldMappingResult, str]],
    document_ids: list[uuid.UUID],
    retriever: Retriever,
    llm: LLMClient,
    results: dict[str, FieldMappingResult],
) -> None:
    """A role the check rejected (`todo`) gets one `map_field` retry told why. A miss or the
    rejected value again keeps it rejected; another value replaces it and its shared copies, and
    its groups are role-checked once more, never retried again."""
    shared = shared_facts(fields)
    siblings = sibling_headings(fields)
    retried = set()
    for f in todo:
        before, note = rejected[f.field_id]
        _log_rejected(f.field_id, ["slot_role"])
        retry = (_echo(before.value, before.evidence), [note])
        sibs = siblings.get(f.field_id, ())
        after = await map_field(ctx, f, document_ids, retriever, llm, sibs, retry)
        if after.status == "missing" or normalize(str(after.value)) == normalize(str(before.value)):
            continue
        retried.add(f.field_id)
        key = shared.get(f.field_id)
        for g in fields:  # the copies of its fact on other slides take the new answer
            same = g.field_id == f.field_id or (key and shared.get(g.field_id) == key)
            if same and results[g.field_id].provenance == "rag":  # never a user-set value
                results[g.field_id] = after.model_copy(update={"field_id": g.field_id})
    if retried:  # a retried value may collide with a third role
        await _check_slot_roles(llm, fields, results, shared, only=retried)


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
