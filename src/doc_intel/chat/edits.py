"""Conversational edits (system-design § 10): validate and apply edit ops, parse them from chat.

Messages never echo a value or a field id: they name the op index and the rule only.
"""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from doc_intel.chat.router import ChatMessage
from doc_intel.core.errors import ValidationFailed
from doc_intel.llm.gateway import LLMClient, complete_json
from doc_intel.llm.prompts import load_prompt
from doc_intel.mapping.engine import CONF_TEXT, CONF_TYPED, shared_facts
from doc_intel.mapping.schemas import FieldMappingResult, Value
from doc_intel.mapping.verifier import TYPED_TYPES, canonical_value, normalize
from doc_intel.templates.schemas import IRField, TemplateIR

VALUE_CHARS = 80


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


FieldId = Annotated[str, Field(max_length=64)]  # the IR field_id pattern's limit
MAX_VALUE_CHARS = 10_000


class SetValue(_Strict):
    op: Literal["set_value"]
    field_id: FieldId
    value: Value


class ChooseCandidate(_Strict):
    op: Literal["choose_candidate"]
    field_id: FieldId
    candidate_index: int = Field(ge=0)


class Clear(_Strict):
    op: Literal["clear"]
    field_id: FieldId


EditOp = Annotated[SetValue | ChooseCandidate | Clear, Field(discriminator="op")]


class EditsIn(_Strict):
    edits: list[EditOp] = Field(min_length=1, max_length=50)


def _bad(index: int, rule: str) -> ValidationFailed:
    return ValidationFailed(f"edits[{index}]: {rule}")


def _strings(value: Value) -> list[str] | None:
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not items or not all(isinstance(i, str) for i in items):
        return None
    return items if all(i.strip() for i in items) else None


def _chart_row(row: object) -> bool:
    """A data point: a non-empty category, then one or more cells that each state one number."""
    return (
        isinstance(row, list)
        and len(row) >= 2
        and all(isinstance(c, str) for c in row)
        and bool(row[0].strip())
        and all(canonical_value(c, "number") is not None for c in row[1:])
    )


def _coerce(index: int, value: Value, expected_type: str) -> Value:
    if len(str(value)) > MAX_VALUE_CHARS:
        raise _bad(index, "the value is too long.")
    if expected_type == "text":
        if isinstance(value, str) and value.strip():
            return value
    elif expected_type in ("paragraphs", "bullet_list"):
        if (items := _strings(value)) is not None:
            return items
    elif expected_type in TYPED_TYPES:
        if isinstance(value, str | int | float) and not isinstance(value, bool):
            if (canon := canonical_value(value, expected_type)) is not None:
                return canon
    elif expected_type == "chart":
        if isinstance(value, list) and value and all(_chart_row(r) for r in value):
            return [[r[0], *(str(canonical_value(c, "number")) for c in r[1:])] for r in value]
    elif isinstance(value, list) and value and all(isinstance(r, list) for r in value):
        return value  # table: rows of cell strings, same shape as an extracted table
    raise _bad(index, "the value does not fit the field type.")


def _apply(
    index: int, op: EditOp, field: IRField, result: FieldMappingResult
) -> FieldMappingResult:
    if isinstance(op, SetValue):
        value = _coerce(index, op.value, field.expected_type)
        update = {"status": "populated", "value": value, "evidence": [], "provenance": "user"}
        update |= {"confidence": 1.0, "missing_reason": None, "conflict_details": None}
    elif isinstance(op, ChooseCandidate):
        candidates = result.conflict_details.candidates if result.conflict_details else []
        if result.status != "conflict":
            raise _bad(index, "choose_candidate needs a field with a conflict.")
        if op.candidate_index >= len(candidates):
            raise _bad(index, "candidate_index is out of range.")
        chosen = candidates[op.candidate_index]
        typed = field.expected_type in TYPED_TYPES
        update = {"status": "populated", "value": chosen.value, "evidence": chosen.evidence}
        update |= {"provenance": "rag", "conflict_details": None, "missing_reason": None}
        update["confidence"] = CONF_TYPED if typed else CONF_TEXT
    else:
        update = {"status": "missing", "value": None, "evidence": [], "provenance": "user"}
        update |= {"missing_reason": "cleared_by_user", "conflict_details": None}
        update["confidence"] = 0.0
    return result.model_copy(update=update)


def apply_edits(
    ir: TemplateIR, results: dict[str, FieldMappingResult], ops: list[EditOp]
) -> dict[str, FieldMappingResult]:
    """A new results dict with the ops applied; the inputs are never mutated. Any violation
    raises `ValidationFailed` before anything is returned."""
    fields = {f.field_id: f for f in ir.fields}
    named: set[str] = set()
    edited: dict[str, FieldMappingResult] = {}
    for i, op in enumerate(ops):
        if op.field_id not in fields or op.field_id not in results:
            raise _bad(i, "unknown field_id.")
        if op.field_id in named:
            raise _bad(i, "a field can be edited once per request.")
        named.add(op.field_id)
        edited[op.field_id] = _apply(i, op, fields[op.field_id], results[op.field_id])
    out = dict(results)
    out.update(edited)
    shared = shared_facts(ir.fields)
    for i, op in enumerate(ops):  # shared-fact siblings follow the edit (map_run maps them once)
        key = shared.get(op.field_id)
        for sibling in ir.fields:
            if key is None or sibling.field_id == op.field_id:
                continue
            if shared.get(sibling.field_id) != key:
                continue
            if sibling.field_id in named:
                raise _bad(i, "a field sharing this fact is also edited.")
            if sibling.field_id in results:
                out[sibling.field_id] = edited[op.field_id].model_copy(
                    update={"field_id": sibling.field_id}
                )
    return out


class _LLMEdit(BaseModel):
    op: Literal["set_value", "choose_candidate", "clear"]
    field_id: str
    value: str | None = None
    candidate_index: int | None = None


class _LLMEdits(BaseModel):
    edits: list[_LLMEdit]


def _clip(value: object) -> str:
    text = " ".join(str(value).split())
    return text[:VALUE_CHARS] + "..." if len(text) > VALUE_CHARS else text


async def parse_edits(
    llm: LLMClient,
    messages: list[ChatMessage],
    ir: TemplateIR,
    results: dict[str, FieldMappingResult],
) -> list[EditOp]:
    """One LLM call: the last user message as edit ops. `LLMInvalidOutput` propagates; an op
    without its argument, or a new value the user did not write, is `ValidationFailed`."""
    # One line per shared-fact group: an edit follows to its siblings, and listing every copy
    # would grow the prompt and invite duplicate ops.
    # shortcut: no token budget for the field list; a template with hundreds of distinct fields
    # can overflow LLM_NUM_CTX, add a budget (or a field pre-filter) if that happens.
    lines, seen = [], set()
    shared = shared_facts(ir.fields)
    for f in ir.fields:
        r, key = results.get(f.field_id), shared.get(f.field_id, f.field_id)
        if r is None or key in seen:
            continue
        seen.add(key)
        current = _clip(r.value) if r.value is not None else ""
        lines.append(f"{f.field_id} | {f.label} | {r.status} | {current}")
        if r.conflict_details:
            lines += [
                f"  {n}: {_clip(c.value)} ({c.evidence[0].filename})"
                for n, c in enumerate(r.conflict_details.candidates, start=1)
            ]
    # last 2,000 chars, as for Q&A questions: the field list must stay inside LLM_NUM_CTX
    last = next((m.content for m in reversed(messages) if m.role == "user"), "")[-2000:]
    prompt = [
        {"role": "system", "content": load_prompt("edits")},
        {"role": "user", "content": "Fields:\n" + "\n".join(lines) + f"\n\nMessage:\n{last}"},
    ]
    ops: list[EditOp] = []
    keys: set[object] = set()
    for i, e in enumerate((await complete_json(llm, prompt, _LLMEdits)).edits):
        if len(e.field_id) > 64:  # never a field: keep pydantic errors (and the text) out
            raise _bad(i, "unknown field_id.")
        key = shared.get(e.field_id, e.field_id)
        if key in keys:  # the model repeated a field or a shared-fact sibling: the first op wins
            continue
        keys.add(key)
        if e.op == "set_value":
            # shortcut: chat values are strings, so a `table` field can't be set from chat (422);
            # use POST .../edits with rows, add a row syntax if users need it in chat.
            if not e.value or not e.value.strip():
                raise _bad(i, "set_value needs a value.")
            # whole words only: a cut-down "1200" must not pass inside "12000"
            if not re.search(rf"(?<!\w){re.escape(normalize(e.value))}(?!\w)", normalize(last)):
                raise ValidationFailed(
                    f"edits[{i}]: the new value must be written in your message."
                )
            ops.append(SetValue(op="set_value", field_id=e.field_id, value=e.value))
        elif e.op == "choose_candidate":
            # the chat summary and the prompt number candidates from 1; the API index is 0-based
            if e.candidate_index is None or e.candidate_index < 1:
                raise _bad(i, "choose_candidate needs a candidate index.")
            ops.append(
                ChooseCandidate(
                    op="choose_candidate",
                    field_id=e.field_id,
                    candidate_index=e.candidate_index - 1,
                )
            )
        else:
            ops.append(Clear(op="clear", field_id=e.field_id))
    return ops
