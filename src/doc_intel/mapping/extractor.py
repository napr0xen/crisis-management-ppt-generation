"""Field extraction LLM call (docs/system-design.md § 7.2)."""

import json
from collections.abc import Sequence
from typing import Any

from doc_intel.embeddings.schemas import Hit
from doc_intel.llm.gateway import (
    RETRY_ECHO_CHARS,
    LLMClient,
    LLMInvalidOutput,
    complete_json,
    prompt_tokens,
)
from doc_intel.llm.prompts import load_prompt
from doc_intel.mapping.retriever import format_hit
from doc_intel.mapping.schemas import LLMCandidateCheck, LLMExtraction, LLMRows, LLMSynthesis
from doc_intel.templates.schemas import IRField

# Context budget: a chunk is <= CHUNK_MAX_TOKENS (512) tokens, and the system prompt, field spec
# and one truncated retry echo take PROMPT_OVERHEAD_TOKENS, so hit_budget chunks plus that fit
# prompt_tokens(LLM_NUM_CTX) (6 at 8192, 26 at 32768) and the rest of the window is the reply.
PROMPT_OVERHEAD_TOKENS = 3072
MAX_PRIMARY_HITS = 12  # ranked hits; the rest of the budget is neighbor chunks (retriever)

_SPEC_FIELDS = {"label", "description", "section_path", "expected_type", "constraints", "context"}
_MAX_PATH_ELEMENTS, _MAX_PATH_ELEMENT_CHARS = 4, 120
_MAX_LIST_ENTRIES, _MAX_ENTRY_CHARS = 30, 80
EXTRACTION_MODELS: dict[str, type[LLMExtraction[Any]]] = {
    "text": LLMExtraction[str],
    "number": LLMExtraction[str],
    "currency": LLMExtraction[str],
    "percent": LLMExtraction[str],
    "date": LLMExtraction[str],
    "paragraphs": LLMExtraction[list[str]],
    "bullet_list": LLMExtraction[list[str]],
}


def hit_budget(num_ctx: int, chunk_tokens: int) -> int:
    """How many chunks of `chunk_tokens` fit one extraction prompt."""
    return max(1, (prompt_tokens(num_ctx) - PROMPT_OVERHEAD_TOKENS) // chunk_tokens)


def bounded_field(field: IRField) -> IRField:
    """A copy whose template-derived strings are capped, for the prompt and the search query."""
    c = field.constraints

    def cap(s: str | None) -> str | None:
        return None if s is None else s[:_MAX_ENTRY_CHARS]

    def cap_all(xs: list[str] | None) -> list[str] | None:
        return None if xs is None else [x[:_MAX_ENTRY_CHARS] for x in xs[:_MAX_LIST_ENTRIES]]

    constraints = c.model_copy(
        update={
            "columns": cap_all(c.columns),
            "allowed_values": cap_all(c.allowed_values),
            "unit": cap(c.unit),
            "date_format": cap(c.date_format),
            "token": cap(c.token),
        }
    )
    path = [p[:_MAX_PATH_ELEMENT_CHARS] for p in field.section_path[-_MAX_PATH_ELEMENTS:]]
    context = None if field.context is None else field.context[:_MAX_PATH_ELEMENT_CHARS]
    return field.model_copy(
        update={"section_path": path, "constraints": constraints, "context": context}
    )


async def extract(llm: LLMClient, field: IRField, hits: list[Hit]) -> LLMExtraction[Any]:
    """One schema-constrained call; `hits` must be the hits later used for verification."""
    spec = bounded_field(field).model_dump_json(include=_SPEC_FIELDS, exclude_none=True)
    sources = "\n\n".join(format_hit(h) for h in hits)
    messages = [
        {"role": "system", "content": load_prompt("field_extract")},
        {"role": "user", "content": f"Field:\n{spec}\n\nChunks:\n\n{sources}"},
    ]
    return await complete_json(llm, messages, EXTRACTION_MODELS[field.expected_type])


async def synthesize(llm: LLMClient, field: IRField, hits: list[Hit]) -> LLMSynthesis:
    """One call writing short items for a list field; `hits` must be those used for verification."""
    spec = bounded_field(field).model_dump_json(include=_SPEC_FIELDS, exclude_none=True)
    sources = "\n\n".join(format_hit(h) for h in hits)
    messages = [
        {"role": "system", "content": load_prompt("field_synthesis")},
        {"role": "user", "content": f"Field:\n{spec}\n\nChunks:\n\n{sources}"},
    ]
    draft = await complete_json(llm, messages, LLMSynthesis)
    c = field.constraints
    values = [i.value for i in draft.items]
    chars = len("\n".join(values))
    too_many = c.max_items is not None and len(values) > c.max_items
    too_long = c.max_chars is not None and chars > c.max_chars
    if not (too_many or too_long):
        return draft
    limits = [
        f"{n} {unit}" for n, unit in ((c.max_chars, "characters"), (c.max_items, "items")) if n
    ]
    ask = (
        f"Your items total {chars} characters and {len(values)} items; the limit is "
        f"{' and '.join(limits)}. Rewrite them shorter: keep the most important facts, drop "
        "details, keep every quote verbatim."
    )
    echo = draft.model_dump_json()[:RETRY_ECHO_CHARS]
    retry = [*messages, {"role": "assistant", "content": echo}, {"role": "user", "content": ask}]
    try:  # one corrective retry; it may still overflow, the engine then falls back
        return await complete_json(llm, retry, LLMSynthesis)
    except LLMInvalidOutput:
        return draft


def _rows_spec(group: list[list[IRField]]) -> dict[str, Any]:
    first = bounded_field(group[0][0])
    if first.expected_type == "table":  # a fill_table_rows field: its constraints name the columns
        columns: list[dict[str, Any]] = [{"label": c} for c in first.constraints.columns or []]
        spec: dict[str, Any] = {
            "columns": columns,
            "description": f"{first.label}: {first.description}",
            "max_rows": first.constraints.max_items,
        }
    else:  # a grid: one column per field of the first row, one row per slot
        columns = []
        for f in map(bounded_field, group[0]):
            col = {"label": f.label, "description": f.description, "expected_type": f.expected_type}
            columns.append({**col, "context": f.context} if f.context else col)
        spec = {"columns": columns, "max_rows": len(group)}
    spec |= {"section_path": first.section_path, "context": first.context}
    return {k: v for k, v in spec.items() if v is not None}


async def extract_rows(llm: LLMClient, group: list[list[IRField]], hits: list[Hit]) -> LLMRows:
    """One schema-constrained call for a whole row group; `hits` are later used for verification."""
    spec = json.dumps(_rows_spec(group), ensure_ascii=False)
    sources = "\n\n".join(format_hit(h) for h in hits)
    messages = [
        {"role": "system", "content": load_prompt("table_rows")},
        {"role": "user", "content": f"Table:\n{spec}\n\nChunks:\n\n{sources}"},
    ]
    return await complete_json(llm, messages, LLMRows)


_MAX_CHECKED, _MAX_QUOTES_PER_CANDIDATE, _MAX_QUOTE, _MAX_BEFORE = 10, 3, 1000, 500


async def check_candidates(
    llm: LLMClient,
    field: IRField,
    hits: list[Hit],
    extraction: LLMExtraction[Any],
    other_headings: Sequence[str] = (),
) -> LLMExtraction[Any]:
    """Not found unless the source states a candidate under the slot's heading (`context`).

    One focused call per context field with candidates, told the sibling slots' headings.
    All or nothing: any match keeps every candidate, none turns the extraction into not_found.
    It never drops a single candidate, so it can never choose between conflicting values.
    """
    if not field.context or extraction.not_found or not extraction.candidates:
        return extraction
    spec = bounded_field(field)
    by_id = {str(h.chunk_id): h for h in hits}
    by_pos = {(h.document_id, h.chunk_index): h for h in hits if h.chunk_index is not None}
    quotes = []
    for i, cand in enumerate(extraction.candidates[:_MAX_CHECKED]):
        first = by_id.get(cand.quotes[0].chunk_id) if cand.quotes else None
        prev = None
        if first is not None and first.chunk_index is not None:
            prev = by_pos.get((first.document_id, first.chunk_index - 1))
        joined = " ... ".join(q.quote[:_MAX_QUOTE] for q in cand.quotes[:_MAX_QUOTES_PER_CANDIDATE])
        item = {
            "n": i + 1,
            "value": str(cand.value)[:200],
            "quote": joined,
            "section": first.section_heading if first else None,
            "before": prev.text[:_MAX_BEFORE] if prev else None,
        }
        quotes.append({k: v for k, v in item.items() if v is not None})
    head = {
        "context": spec.context,
        "label": spec.label,
        "description": spec.description,
        "other_headings": list(other_headings) or None,
    }
    user = {**{k: v for k, v in head.items() if v is not None}, "quotes": quotes}
    messages = [
        {"role": "system", "content": load_prompt("candidate_check")},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]
    verdict = await complete_json(llm, messages, LLMCandidateCheck)
    if any(c.match for c in verdict.checks):
        return extraction
    return LLMExtraction(candidates=[], not_found=True, incomplete=extraction.incomplete)
