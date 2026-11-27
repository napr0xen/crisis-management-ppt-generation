"""FieldMappingResult and the field-extraction LLM contract (docs/system-design.md § 6-7).

Phase 7 writers, Phase 8 summary and Phase 11 edits read these shapes; change them only with docs.
"""

import re
import uuid
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from doc_intel.mapping.verifier import MIN_QUOTE_CHARS
from doc_intel.templates.schemas import Derivation

FieldStatus = Literal["populated", "partial", "missing", "conflict"]
MissingReason = Literal[
    "no_relevant_chunks",
    "not_found_by_llm",
    "evidence_failed_verification",
    "insufficient_evidence",
    "llm_error",
    "cleared_by_user",
]
Provenance = Literal["rag", "user"]
# max_chars_exceeded: mapping; formula_cell, table_overflow: Phase 7 writers (system-design § 8).
WarningCode = Literal["max_chars_exceeded", "formula_cell", "table_overflow"]
# JSON string | number | array (system-design § 6 `Value`). By expected_type: text -> str,
# paragraphs/bullet_list -> list[str], number/currency/percent -> int | float, date -> ISO str,
# table -> list[list[str]] (cells in constraints.columns order).
Value = str | int | float | list[str] | list[list[str]]

MAX_QUOTE_CHARS = 1000


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# LLM output (system-design § 7.2). One model per value shape keeps Ollama's JSON grammar free of
# unions: LLMExtraction[str], LLMExtraction[list[str]], LLMExtraction[list[list[str]]].
# No length limits: one bad quote must drop that quote, not fail the whole reply.
_PROVENANCE_ID = re.compile(r"\[(?:P\d+-)?T\d+-R\d+\] ?")


class LLMQuote(_Strict):
    chunk_id: str
    quote: str

    @field_validator("quote")
    @classmethod
    def _without_provenance_ids(cls, v: str) -> str:
        """`format_hit` shows `[P1-T2-R04]` before a row; the chunk text has none, and no id digit
        may verify a number."""
        return _PROVENANCE_ID.sub("", v)


class LLMCandidate[V](_Strict):
    value: V
    quotes: list[LLMQuote]


class LLMExtraction[V](_Strict):
    candidates: list[LLMCandidate[V]]
    not_found: bool
    incomplete: bool


class LLMCell(_Strict):
    """One value bound to the label of the slot or column it fills (positional lists shift)."""

    slot: str
    value: str


class LLMRow(_Strict):
    """One row per source record; `value` holds its cells in column order."""

    value: list[LLMCell]
    quotes: list[LLMQuote]

    def cells(self) -> list[str]:
        return [c.value for c in self.value]


class LLMRows(_Strict):
    rows: list[LLMRow]
    not_found: bool


class LLMRecord(_Strict):
    """One record of a slot group: `n` is the record's number in the spec, `value` has one cell
    per slot in slot order."""

    n: int
    value: list[LLMCell]
    quotes: list[LLMQuote]

    def cells(self) -> list[str]:
        return [c.value for c in self.value]


class LLMRecords(_Strict):
    records: list[LLMRecord]
    not_found: bool


class LLMSlotValue(_Strict):
    """The candidates for slot `n` of a batch call (extractor.extract_batch); like LLMExtraction."""

    n: int
    candidates: list[LLMCandidate[str | list[str]]]
    not_found: bool
    incomplete: bool


class LLMSlotValues(_Strict):
    slots: list[LLMSlotValue]


class LLMSynthesis(_Strict):
    """Items written from the chunks (extractor.synthesize); each is grounded by its quotes."""

    items: list[LLMCandidate[str]]
    not_found: bool


class LLMCheck(_Strict):
    about: str  # reasoning aid only: never logged, stored or read by code
    match: bool


class LLMCandidateCheck(_Strict):
    """One entry per numbered quote of the candidate check (extractor.check_candidates)."""

    checks: list[LLMCheck]


class LLMRowCheck(_Strict):
    about: str  # reasoning aid only: never logged, stored or read by code
    belongs: Literal["this", "other", "unknown"]


class LLMRowChecks(_Strict):
    """One entry per numbered row of the row check (extractor.check_rows)."""

    checks: list[LLMRowCheck]


class LLMSlotRoles(_Strict):
    """Which numbered slots of one template box a value belongs to (extractor.check_roles)."""

    about: str  # reasoning aid only: never logged, stored or read by code
    slots: list[int]


class Evidence(_Strict):
    """Every field except `quote` is copied from the verified hit, never from the LLM."""

    document_id: uuid.UUID
    chunk_id: uuid.UUID
    filename: str
    page_number: int | None
    section_heading: str | None
    quote: str = Field(min_length=MIN_QUOTE_CHARS, max_length=MAX_QUOTE_CHARS)
    item: int | None = Field(None, ge=0)  # synthesized values: index in `value` the quote backs
    row_id: str | None = None  # derived values: provenance id of the table row, e.g. P1-T2-R04


class Candidate(_Strict):
    value: Value
    evidence: list[Evidence] = Field(min_length=1)


class ConflictDetails(_Strict):
    candidates: list[Candidate] = Field(min_length=2)


class FieldMappingResult(_Strict):
    field_id: str
    status: FieldStatus
    value: Value | None
    evidence: list[Evidence]
    confidence: float = Field(ge=0, le=1)
    missing_reason: MissingReason | None
    checked_documents: list[uuid.UUID]
    conflict_details: ConflictDetails | None
    provenance: Provenance

    @model_validator(mode="after")
    def _status_invariants(self) -> Self:
        # Messages name the rule only; never log str(ValidationError): it carries input values.
        status = self.status
        if status == "populated" and self.provenance == "rag" and not self.evidence:
            raise ValueError("populated (rag) needs evidence")
        if status in ("populated", "partial") and self.value is None:
            raise ValueError(f"{status} needs a value")
        if status == "partial" and not self.evidence:
            raise ValueError("partial needs evidence")
        if status == "missing" and (
            self.value is not None or self.evidence or self.missing_reason is None
        ):
            raise ValueError("missing has no value, no evidence and a reason")
        if status == "conflict" and (self.value is not None or self.conflict_details is None):
            raise ValueError("conflict has no value and has candidates")
        return self


class MappingWarning(_Strict):
    field_id: str
    code: WarningCode


class MappingSummary(_Strict):
    """`mapping_runs.summary`; all four counts are always present."""

    populated: int
    partial: int
    missing: int
    conflict: int
    warnings: list[MappingWarning]


class RowCategory(_Strict):
    id: str  # a row id as shown in the prompt, e.g. P1-T2-R04
    category: str = Field(min_length=1, max_length=40)


class LLMRowCategories(_Strict):
    """A category per table row, chosen by the LLM from the row's text when the source has no
    category column (Phase 29); labels only, never a number."""

    rows: list[RowCategory]


class LLMDerivation(_Strict):
    """The binding of a derived slot (ADR-018): a table label and exact headers, never a number."""

    table: str = ""  # the label shown in the prompt, e.g. "t1"
    not_found: bool = False
    derivation: Derivation | None = None
