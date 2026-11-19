"""Build a TemplateIR from an AnchorMap with one LLM call plus one validation retry (§ 5)."""

import json
import re
import uuid

from doc_intel.core.errors import IRInvalid
from doc_intel.llm.gateway import LLMClient, Messages, complete_json
from doc_intel.llm.prompts import load_prompt
from doc_intel.templates.schemas import (
    WRITE_MODE_KINDS,
    AnchorMap,
    IRDraft,
    TemplateIR,
)

_TEXT_CUT = 200
_CELL = re.compile(r":r\d+c\d+$")  # pptx:s5:sp4:r1c2 -> table pptx:s5:sp4


def _table_of(anchor_id: str) -> str:
    return _CELL.sub("", anchor_id)


def validate(draft: IRDraft, amap: AnchorMap) -> list[str]:
    """Human-readable errors (ids and modes only, no document text); empty means valid."""
    anchors = {a.anchor_id: a for a in amap.anchors}
    errors: list[str] = []
    seen_anchors: dict[str, str] = {}
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
        if f.anchor_id in seen_anchors:
            errors.append(
                f"field '{f.field_id}': anchor_id '{f.anchor_id}' is already used by field "
                f"'{seen_anchors[f.anchor_id]}'; drop one of them."
            )
        else:
            seen_anchors[f.anchor_id] = f.field_id
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


def _compact(amap: AnchorMap) -> str:
    anchors = []
    for a in amap.anchors:
        item: dict = {
            "anchor_id": a.anchor_id,
            "kind": a.kind,
            "path": a.path,
            "text": a.text[:_TEXT_CUT],
            "tokens": a.tokens,
            "is_placeholder": a.is_placeholder,
        }
        if a.table:
            item["table"] = {"row": a.table.row, "col": a.table.col, "header": a.table.header}
        anchors.append(item)
    return json.dumps({"format": amap.format, "anchors": anchors}, ensure_ascii=False)


async def build_ir(llm: LLMClient, amap: AnchorMap, template_id: uuid.UUID) -> TemplateIR:
    # shortcut: the whole AnchorMap goes in one prompt (<= MAX_ANCHORS, text cut to 200 chars);
    # split by slide/section when templates outgrow LLM_NUM_CTX.
    messages: Messages = [
        {"role": "system", "content": load_prompt("template_ir")},
        {"role": "user", "content": f"Template anchors:\n\n{_compact(amap)}"},
    ]
    draft = await complete_json(llm, messages, IRDraft)
    errors = validate(draft, amap)
    if errors:
        listing = "\n".join(f"- {e}" for e in errors)
        messages = [
            *messages,
            {"role": "assistant", "content": draft.model_dump_json()},
            {
                "role": "user",
                "content": f"The fields have these errors:\n{listing}\n"
                "Reply with the corrected full JSON.",
            },
        ]
        draft = await complete_json(llm, messages, IRDraft)
        if validate(draft, amap):
            raise IRInvalid()
    return TemplateIR(fields=draft.fields, template_id=template_id, version=1, format=amap.format)
