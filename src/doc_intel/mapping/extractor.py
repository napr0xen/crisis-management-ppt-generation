"""Field extraction LLM call (docs/system-design.md § 7.2)."""

from typing import Any

from doc_intel.embeddings.schemas import Hit
from doc_intel.llm.gateway import LLMClient, complete_json
from doc_intel.llm.prompts import load_prompt
from doc_intel.mapping.retriever import format_hit
from doc_intel.mapping.schemas import LLMExtraction
from doc_intel.templates.schemas import IRField

# Context budget: hits are <= CHUNK_MAX_TOKENS (512) each, so 6 hits bound the chunk block at
# ~3.1k tokens; with system prompt, field spec and one truncated retry echo the prompt stays
# under ~5k of LLM_NUM_CTX=8192, leaving room for the reply.
MAX_FIELD_HITS = 6


_SPEC_FIELDS = {"label", "description", "section_path", "expected_type", "constraints"}
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
    "table": LLMExtraction[list[list[str]]],
}


def fit_hits(hits: list[Hit]) -> list[Hit]:
    return hits[:MAX_FIELD_HITS]


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
    return field.model_copy(update={"section_path": path, "constraints": constraints})


async def extract(llm: LLMClient, field: IRField, hits: list[Hit]) -> LLMExtraction[Any]:
    """One schema-constrained call; `hits` must be the fitted hits later used for verification."""
    spec = bounded_field(field).model_dump_json(include=_SPEC_FIELDS, exclude_none=True)
    sources = "\n\n".join(format_hit(h) for h in hits)
    messages = [
        {"role": "system", "content": load_prompt("field_extract")},
        {"role": "user", "content": f"Field:\n{spec}\n\nChunks:\n\n{sources}"},
    ]
    return await complete_json(llm, messages, EXTRACTION_MODELS[field.expected_type])
