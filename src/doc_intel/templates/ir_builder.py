"""Build a TemplateIR from an AnchorMap in per-slide/sheet batches (§ 5).

Each batch is one LLM call plus at most one validation retry, so a prompt plus its reply fits
LLM_NUM_CTX; the merged result is validated again against the whole map.
"""

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from itertools import groupby

import structlog

from doc_intel.core.errors import IRInvalid
from doc_intel.llm.gateway import RETRY_ECHO_CHARS, LLMClient, Messages, complete_json
from doc_intel.llm.prompts import load_prompt
from doc_intel.templates.schemas import (
    WRITE_MODE_KINDS,
    Anchor,
    AnchorMap,
    IRDraft,
    IRField,
    TemplateIR,
)

log = structlog.get_logger()

# Logged ids must be structural: xlsx ids hold the sheet name, sdt ids the control's tag, and an
# unknown anchor_id is free LLM text; anything else in quotes is logged as '?'.
_QUOTED = re.compile(r"'([^']*)'")
_LOGGABLE = re.compile(
    r"[a-z][a-z0-9_]{0,63}|pptx:s\d+:sp\d+(?:_\d+)?(?::r\d+c\d+)?"
    r"|docx:(?:body:(?:p\d+|t\d+:r\d+c\d+)|sec\d+:(?:header|footer):p\d+)"
)


def _loggable(errors: list[str]) -> list[str]:
    return [_QUOTED.sub(lambda m: m[0] if _LOGGABLE.fullmatch(m[1]) else "'?'", e) for e in errors]


_TEXT_CUT = 200
# A batch prompt stays <= ~3.5k tokens (12,000 JSON chars), so it, the system prompt and a
# num_ctx//2 reply fit LLM_NUM_CTX=8192; the anchor cap also bounds the reply's field count.
MAX_BATCH_ANCHORS = 40
MAX_BATCH_CHARS = 12_000
_FRAME_CHARS = 40  # '{"format": "pptx", "anchors": []}' around the items
_CELL = re.compile(r":r\d+c\d+$")  # pptx:s5:sp4:r1c2 -> table pptx:s5:sp4


def _table_of(anchor_id: str) -> str:
    return _CELL.sub("", anchor_id)


def validate(draft: IRDraft, amap: AnchorMap) -> list[str]:
    """Human-readable errors (ids and modes only, no document text); empty means valid."""
    anchors = {a.anchor_id: a for a in amap.anchors}
    errors: list[str] = []
    # anchor_id -> its fields; several only when every one is a replace_token on its own token
    by_anchor: dict[str, list[IRField]] = {}
    seen_fields: set[str] = set()
    # fill_table_rows writes every body row, so no other field may target that table.
    rows_owner = {
        _table_of(f.anchor_id): f.field_id
        for f in draft.fields
        if f.write_mode == "fill_table_rows" and f.anchor_id in anchors
    }
    for f in draft.fields:
        if f.field_id in seen_fields:
            errors.append(f"field_id '{f.field_id}' is used more than once.")
        seen_fields.add(f.field_id)
        anchor = anchors.get(f.anchor_id)
        if anchor is None:
            errors.append(f"field '{f.field_id}': anchor_id '{f.anchor_id}' does not exist.")
            continue
        earlier = by_anchor.setdefault(f.anchor_id, [])
        for other in earlier:
            if f.write_mode != "replace_token" or other.write_mode != "replace_token":
                errors.append(
                    f"field '{f.field_id}': anchor_id '{f.anchor_id}' is already used by field "
                    f"'{other.field_id}'; drop one of them."
                )
                break
            if f.constraints.token == other.constraints.token:
                errors.append(
                    f"field '{f.field_id}': anchor_id '{f.anchor_id}' has the same token as "
                    f"field '{other.field_id}'; drop one of them."
                )
                break
        earlier.append(f)
        owner = rows_owner.get(_table_of(f.anchor_id))
        if anchor.kind == "table_cell" and owner not in (None, f.field_id):
            errors.append(
                f"field '{f.field_id}': its table is already filled by fill_table_rows field "
                f"'{owner}'; drop one of them."
            )
        if anchor.kind not in WRITE_MODE_KINDS[f.write_mode]:
            errors.append(
                f"field '{f.field_id}': write_mode '{f.write_mode}' is not allowed "
                f"for anchor kind '{anchor.kind}'."
            )
        elif (
            f.write_mode in ("replace_text", "fill_cell")
            and anchor.kind != "sheet_cell"
            and anchor.tokens
            and anchor.text.strip() not in anchor.tokens
        ):
            errors.append(
                f"field '{f.field_id}': anchor '{anchor.anchor_id}' has tokens; use "
                "replace_token so the text around the token is kept."
            )
        elif f.write_mode == "replace_token" and f.constraints.token not in anchor.tokens:
            errors.append(
                f"field '{f.field_id}': replace_token needs constraints.token to be one "
                f"of the anchor's tokens (anchor '{anchor.anchor_id}' has {len(anchor.tokens)})."
            )
        elif f.write_mode == "fill_table_rows" and not (
            anchor.table and (anchor.table.row, anchor.table.col) == (1, 0)
        ):
            errors.append(
                f"field '{f.field_id}': fill_table_rows needs a table anchor at row 1, col 0."
            )
    return errors


def _item(a: Anchor) -> dict:
    item: dict = {
        "anchor_id": a.anchor_id,
        "kind": a.kind,
        "path": a.path,
        "text": a.text[:_TEXT_CUT],
        "tokens": a.tokens,
        "is_placeholder": a.is_placeholder,
    }
    if a.pos:
        item["pos"] = list(a.pos)
    if a.context:
        item["context"] = a.context
    if a.table:
        item["table"] = {"row": a.table.row, "col": a.table.col, "header": a.table.header}
    return item


def _compact(amap: AnchorMap) -> str:
    anchors = [_item(a) for a in amap.anchors]
    return json.dumps({"format": amap.format, "anchors": anchors}, ensure_ascii=False)


def _size(a: Anchor) -> int:
    return len(json.dumps(_item(a), ensure_ascii=False)) + 2  # + ", " between items


_SLIDE = re.compile(r"^pptx:s(\d+):")


class BatchDraft(IRDraft):
    """One batch's reply: a slide of finished text has no fields, so none are forced."""

    fields: list[IRField]


def _group_of(fmt: str, anchor_id: str) -> str:
    if fmt == "pptx":
        m = _SLIDE.match(anchor_id)
        return m[1] if m else ""
    return anchor_id.rsplit("!", 1)[0] if fmt == "xlsx" else ""


def _batches(amap: AnchorMap) -> list[tuple[str | None, list[Anchor]]]:
    """Anchors split per slide/sheet (docx: one group), tables kept whole, <= the cap each."""
    out: list[tuple[str | None, list[Anchor]]] = []
    for index, (key, members) in enumerate(
        groupby(amap.anchors, lambda a: _group_of(amap.format, a.anchor_id)), start=1
    ):
        group = list(members)
        # never the sheet name: it is template content
        label = {"pptx": f"slide {key}" if key else None, "xlsx": f"sheet {index}"}.get(amap.format)
        current: list[Anchor] = []
        chars = _FRAME_CHARS

        def fits(n: int, size: int) -> bool:
            return n <= MAX_BATCH_ANCHORS and size <= MAX_BATCH_CHARS

        for _, unit_iter in groupby(group, lambda a: _table_of(a.anchor_id)):
            unit = list(unit_iter)
            sizes = [_size(a) for a in unit]
            # a non-cell anchor is its own unit (its key is its own id), a table's cells share one
            if current and not fits(len(current) + len(unit), chars + sum(sizes)):
                out.append((label, current))
                current, chars = [], _FRAME_CHARS
            for a, size in zip(unit, sizes, strict=True):
                if current and not fits(len(current) + 1, chars + size):  # a unit over a cap
                    out.append((label, current))  # is split in row-major order
                    current, chars = [], _FRAME_CHARS
                current.append(a)
                chars += size
        if current:
            out.append((label, current))
    return out


def _repair(draft: BatchDraft, amap: AnchorMap) -> BatchDraft:
    """Drop model slips that are safe to drop: removing a field never fabricates one."""
    anchors = {a.anchor_id: a for a in amap.anchors}
    kept: list[IRField] = []
    seen: set[tuple[str, str, str | None]] = set()
    for f in draft.fields:
        a = anchors.get(f.anchor_id)
        if a and f.write_mode == "fill_table_rows" and a.kind != "table_cell":
            continue
        if a and f.write_mode == "replace_token" and not a.tokens and not a.is_placeholder:
            continue
        key = (f.anchor_id, f.write_mode, f.constraints.token)
        if a and key in seen:
            continue
        seen.add(key)
        kept.append(f)
    if len(kept) < len(draft.fields):
        log.info(
            "ir_repaired", dropped=len(draft.fields) - len(kept), anchor_count=len(amap.anchors)
        )
        return draft.model_copy(update={"fields": kept})
    return draft


async def _build_batch(llm: LLMClient, sub: AnchorMap) -> BatchDraft:
    messages: Messages = [
        {"role": "system", "content": load_prompt("template_ir")},
        {"role": "user", "content": f"Template anchors:\n\n{_compact(sub)}"},
    ]
    draft = _repair(await complete_json(llm, messages, BatchDraft), sub)
    errors = validate(draft, sub)
    if errors:
        listing = "\n".join(f"- {e}" for e in errors)
        messages = [
            *messages,
            # truncated so the retry never outgrows num_ctx
            {"role": "assistant", "content": draft.model_dump_json()[:RETRY_ECHO_CHARS]},
            {
                "role": "user",
                "content": f"The fields have these errors:\n{listing}\n"
                "Reply with the corrected full JSON.",
            },
        ]
        draft = _repair(await complete_json(llm, messages, BatchDraft), sub)
        errors = validate(draft, sub)
        if errors:
            log.warning("ir_invalid", errors=_loggable(errors), anchor_count=len(sub.anchors))
            raise IRInvalid()
    return draft


async def build_ir(
    llm: LLMClient,
    amap: AnchorMap,
    template_id: uuid.UUID,
    *,
    on_batch: Callable[[int, int, str | None], Awaitable[None]] | None = None,
) -> TemplateIR:
    # shortcut: a table over MAX_BATCH_ANCHORS is judged chunk by chunk, so a later chunk may
    # still get fill_cell fields when the first chunk did not pick fill_table_rows; the final
    # validate() rejects only conflicts, upgrade by sending the table header row with every chunk.
    batches = _batches(amap)
    fields: list[IRField] = []
    owned: set[str] = set()  # tables already written whole by a fill_table_rows field
    used: set[str] = set()
    for done, (part, anchors) in enumerate(batches):
        if on_batch:
            await on_batch(done, len(batches), part)
        anchors = [a for a in anchors if _table_of(a.anchor_id) not in owned]
        if not anchors:
            continue
        draft = await _build_batch(llm, AnchorMap(format=amap.format, anchors=anchors))
        for f in draft.fields:
            if f.field_id in used:
                n = 2
                while f"{f.field_id[:60]}_{n}" in used:
                    n += 1
                f = f.model_copy(update={"field_id": f"{f.field_id[:60]}_{n}"})
            used.add(f.field_id)
            fields.append(f)
            if f.write_mode == "fill_table_rows":
                owned.add(_table_of(f.anchor_id))
    errors = validate(IRDraft(fields=fields), amap) if fields else []
    if not fields or errors:
        log.warning("ir_invalid", errors=_loggable(errors), anchor_count=len(amap.anchors))
        raise IRInvalid()
    # context and card are code-set, never the LLM's
    by_id = {a.anchor_id: a for a in amap.anchors}
    fields = [
        f.model_copy(
            update={"context": by_id[f.anchor_id].context, "card": by_id[f.anchor_id].card}
        )
        for f in fields
    ]
    return TemplateIR(fields=fields, template_id=template_id, version=1, format=amap.format)
