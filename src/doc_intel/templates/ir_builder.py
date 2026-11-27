"""Build a TemplateIR from an AnchorMap in per-slide/sheet batches (§ 5).

Each batch is one LLM call plus at most one validation retry, so a prompt plus its reply fits
LLM_NUM_CTX; the merged result is validated again against the whole map.
"""

import json
import re
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from itertools import groupby
from typing import Literal

import structlog
from pydantic import Field

from doc_intel.core.errors import IRInvalid
from doc_intel.llm.gateway import RETRY_ECHO_CHARS, LLMClient, Messages, complete_json
from doc_intel.llm.prompts import load_prompt
from doc_intel.templates.anchors import has_letter
from doc_intel.templates.schemas import (
    SLOT_NUMBER,
    WRITE_MODE_KINDS,
    Anchor,
    AnchorMap,
    Constraints,
    FieldMode,
    IRDraft,
    IRField,
    TemplateIR,
)

log = structlog.get_logger()

IR_BUILDER_VERSION = 7  # bump when build_ir output changes, so library templates are re-extracted

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
        if (f.expected_type == "chart") != (f.write_mode in ("fill_chart", "insert_chart")):
            errors.append(
                f"field '{f.field_id}': expected_type 'chart' and write_mode fill_chart or "
                "insert_chart go together."
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
                f"field '{f.field_id}': replace_token needs constraints.token to be one of the "
                f"tokens of anchor '{anchor.anchor_id}' ({len(anchor.tokens)} tokens)."
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
    if a.chart:
        item["chart"] = a.chart.model_dump()
    if a.table:
        item["table"] = {"row": a.table.row, "col": a.table.col, "header": a.table.header}
    return item


def _compact(amap: AnchorMap) -> str:
    anchors = [_item(a) for a in amap.anchors]
    return json.dumps({"format": amap.format, "anchors": anchors}, ensure_ascii=False)


def _size(a: Anchor) -> int:
    return len(json.dumps(_item(a), ensure_ascii=False)) + 2  # + ", " between items


_SLIDE = re.compile(r"^pptx:s(\d+):")


class LLMIRField(IRField):
    """A field as the IR model writes it: `mode` has no default, so every slot is classified
    (Phase 19 finance run: with a default the model set it on none of 116 fields)."""

    mode: FieldMode = Field(
        description="extractive: the value is copied verbatim from the source (names, ids, "
        "printed amounts and balances, dates, periods, list or table items). synthesis: the "
        "value is written from the source (a summary, trend, pattern, observation, commentary, "
        "finding or note). derived: a total, net movement (inflows - outflows), count, "
        "minimum, maximum or category breakdown computed over the rows of a source table "
        "(computed in code, not written)."
    )


class BatchDraft(IRDraft):
    """One batch's reply: a slide of finished text has no fields, so none are forced."""

    fields: list[IRField]


class BatchReply(BatchDraft):
    """`BatchDraft` as the IR model must write it (the schema it is given and validated by)."""

    fields: list[LLMIRField]  # type: ignore[assignment]


async def _reply(llm: LLMClient, messages: Messages) -> BatchDraft:
    reply = await complete_json(llm, messages, BatchReply)
    return BatchDraft.model_validate(reply.model_dump())


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


def _cells_of(amap: AnchorMap) -> dict[str, dict[tuple[int, int], Anchor]]:
    tables: dict[str, dict[tuple[int, int], Anchor]] = {}
    for a in amap.anchors:
        if a.kind == "table_cell" and a.table and _CELL.search(a.anchor_id):
            tables.setdefault(_table_of(a.anchor_id), {})[(a.table.row, a.table.col)] = a
    return tables


def _label_grids(amap: AnchorMap) -> set[str]:
    """Native tables whose every body row is a static label in column 0 plus a token cell: a
    label/value grid, each value its own `replace_token`, never one `fill_table_rows`."""
    grids = set()
    for table, cells in _cells_of(amap).items():
        rows: dict[int, list[Anchor]] = {}
        for (r, _), a in sorted(cells.items()):
            if r:
                rows.setdefault(r, []).append(a)
        if (
            rows
            and all(
                row[0].table.col == 0  # type: ignore[union-attr]
                and not row[0].tokens
                and has_letter(row[0].text)
                and any(a.tokens for a in row[1:])
                for row in rows.values()
            )
        ):
            grids.add(table)
    return grids


def _token_key(token: str) -> str:
    return " ".join(token.strip("[]{}<> \t\r\n").split()).casefold()


# A picture slot ("[Company Logo 1]", "[Logo]", "[Image 2]", "{{photo}}") is filled by hand with
# an image, never with text; "[Image caption]" is text.
_PICTURE = re.compile(
    r"^(?:[^\W\d_]+ )?(?:logo|image|picture|photo|icon)(?: (?:\d+|placeholder))?$"
)


def _is_picture(token: str | None) -> bool:
    return token is not None and bool(_PICTURE.match(_token_key(token)))


def _snap_token(f: IRField, a: Anchor | None) -> IRField:
    """The model often drops the brackets of a token; adopt the anchor's real token when exactly
    one matches, else leave it for validate() to report."""
    token = f.constraints.token
    if not (a and f.write_mode == "replace_token" and a.tokens and token is not None):
        return f
    if token in a.tokens:
        return f
    hits = [t for t in a.tokens if _token_key(t) == _token_key(token)]
    if len(hits) != 1:
        return f
    return f.model_copy(update={"constraints": f.constraints.model_copy(update={"token": hits[0]})})


def _repair(draft: BatchDraft, amap: AnchorMap) -> BatchDraft:
    """Fix or drop model slips that are safe: removing a field never fabricates one."""
    anchors = {a.anchor_id: a for a in amap.anchors}
    grids = _label_grids(amap)
    kept: list[IRField] = []
    seen: set[tuple[str, str, str | None]] = set()
    changed = False
    for original in draft.fields:
        a = anchors.get(original.anchor_id)
        f = _snap_token(original, a)
        changed |= f is not original
        if a and f.write_mode == "fill_table_rows" and a.kind != "table_cell":
            continue
        if f.write_mode == "fill_table_rows" and _table_of(f.anchor_id) in grids:
            continue
        if a and f.write_mode == "replace_token" and not a.tokens and not a.is_placeholder:
            continue
        if f.write_mode == "replace_token" and _is_picture(f.constraints.token):
            continue
        if a and a.kind == "table_cell" and a.table and a.table.row == 0 and a.text.strip():
            if not a.tokens:  # a header label, never a field; an empty row-0 cell stays fillable
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
    return (
        draft.model_copy(update={"fields": kept})
        if changed or len(kept) < len(draft.fields)
        else draft
    )


async def _build_batch(llm: LLMClient, sub: AnchorMap) -> BatchDraft:
    messages: Messages = [
        {"role": "system", "content": load_prompt("template_ir")},
        {"role": "user", "content": f"Template anchors:\n\n{_compact(sub)}"},
    ]
    draft = _repair(await _reply(llm, messages), sub)
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
        draft = _repair(await _reply(llm, messages), sub)
        errors = validate(draft, sub)
        if errors:
            log.warning("ir_invalid", errors=_loggable(errors), anchor_count=len(sub.anchors))
            raise IRInvalid()
    return draft


# placeholder spellings of a month and a year (English, Spanish), and of a day
_MONTH_YEAR = re.compile(r"(?i)^(?=.*\b(?:month|mon|mmm?|mes)\b)(?=.*\b(?:yyyy|yy|aaaa|aa)\b)")
_DAY = re.compile(r"(?i)\b(?:dd?|day|d[ií]a)\b")


def _with_anchor_data(f: IRField, a: Anchor) -> IRField:
    """context, card, fit_chars and a native chart's series columns come from the anchor, never
    the LLM; a `#` token (`[# 1]`, `[# items]`) is a count, so it is a number, and a month-year
    token (`[Month YYYY]`, `[MM/YYYY]`) is text, whatever type the LLM chose."""
    update: dict = {"context": a.context, "card": a.card, "fit_chars": a.fit_chars}
    token = (f.constraints.token or "").strip("{[< ")
    if f.write_mode == "replace_token" and token.startswith("#"):
        update["expected_type"] = "number"
    elif f.expected_type == "date" and _MONTH_YEAR.search(token) and not _DAY.search(token):
        update["expected_type"] = "text"  # a day-less date is copied verbatim, never given a day
    if f.write_mode == "fill_chart" and a.chart:
        update["constraints"] = f.constraints.model_copy(
            update={"columns": list(a.chart.series) or None}
        )
    elif f.write_mode == "insert_chart" and not f.constraints.columns:
        update["constraints"] = f.constraints.model_copy(update={"columns": [f.label]})
    return f.model_copy(update=update)


_TRAILING_ORDINAL = re.compile(r"[\s_#-]*(?<!\d)\d{1,3}$")  # a year ("FY2025") is no ordinal


def _caption_types(fields: list[IRField], by_id: dict[str, Anchor]) -> list[IRField]:
    """In a card, a numeric token with no ordinal beside a numbered token (`[Revenue]` under
    `[Value 1]`) is the tile's caption, the measure's name: text, not a number the source lacks."""
    unit_of = {}
    for f in fields:
        a = by_id[f.anchor_id]
        if f.write_mode == "replace_token" and a.card and f.constraints.token:
            slide = _SLIDE.match(a.anchor_id)
            unit_of[f.field_id] = (slide[1] if slide else "", a.card)
    numbered = {
        unit_of[f.field_id]
        for f in fields
        if f.field_id in unit_of
        and _TRAILING_ORDINAL.search((f.constraints.token or "").strip("{}[]<> "))
    }
    return [
        f.model_copy(update={"expected_type": "text"})
        if f.field_id in unit_of
        and unit_of[f.field_id] in numbered
        and f.expected_type in ("number", "currency", "percent")
        and not (f.constraints.token or "").strip("{[< ").startswith("#")
        and not _TRAILING_ORDINAL.search((f.constraints.token or "").strip("{}[]<> "))
        else f
        for f in fields
    ]


def _empty_tables(amap: AnchorMap, fields: list[IRField], used: set[str]) -> list[IRField]:
    """A `fill_table_rows` field for each native table no field touches whose header row names
    every column and whose body is empty: such a table is a slot the IR model sometimes skips."""
    # shortcut: a table meant to be filled by hand (a sign-off grid) also gets a field and a
    # missing marker when the sources state no rows; add an IR-edit opt-out if users hit it.
    touched = {_table_of(f.anchor_id) for f in fields}
    added = []
    for table, cells in _cells_of(amap).items():
        headers = [" ".join(cells[rc].text.split()) for rc in sorted(cells) if rc[0] == 0]
        body = [a for rc, a in cells.items() if rc[0] > 0]
        first = cells.get((1, 0))
        if table in touched or first is None or not headers or not all(map(has_letter, headers)):
            continue
        if any(a.text.strip() for a in body):
            continue
        n = 1
        while f"table_rows_{n}" in used:
            n += 1
        used.add(f"table_rows_{n}")
        added.append(
            IRField(
                field_id=f"table_rows_{n}",
                anchor_id=first.anchor_id,
                section_path=first.path,
                label=" / ".join(headers)[:120],
                description=f"One row per record, columns: {', '.join(headers)}."[:500],
                expected_type="table",
                required=False,
                write_mode="fill_table_rows",
                constraints=Constraints(
                    columns=headers, max_items=len({rc[0] for rc in cells if rc[0] > 0})
                ),
            )
        )
    return added


_NOT_ID = re.compile(r"[^a-z0-9]+")


def _token_cells(amap: AnchorMap, fields: list[IRField], used: set[str]) -> list[IRField]:
    """A `replace_token` field for each token no field covers, on any anchor a token can be written
    to, so no bracket token stays literal in the output (the IR model sometimes skips cells and
    repeated text boxes). A copy of a token another field already maps on the same slide or
    sheet takes that field's label, description, type and mode."""
    # shortcut: instruction-style bracket text in a text box ("[Add presenter name]") becomes a
    # field and renders missing, upgrade if templates with author instruction boxes show up.
    owned = {_table_of(f.anchor_id) for f in fields if f.write_mode == "fill_table_rows"}
    covered = {
        (f.anchor_id, f.constraints.token) for f in fields if f.write_mode == "replace_token"
    }
    other = {f.anchor_id for f in fields if f.write_mode != "replace_token"}
    twins = {
        (_group_of(amap.format, f.anchor_id), f.constraints.token): f
        for f in reversed(fields)
        if f.write_mode == "replace_token"
    }
    added = []
    for a in amap.anchors:
        if (
            a.kind not in WRITE_MODE_KINDS["replace_token"]
            or a.anchor_id in other
            or _table_of(a.anchor_id) in owned
        ):
            continue
        for token in a.tokens:
            if (a.anchor_id, token) in covered or _is_picture(token):
                continue
            # a bare blank in prose ("status TBD") may be the author's text; a bracketed name and
            # any table-cell token are always slots
            if a.kind != "table_cell" and not token.startswith(("{{", "[", "<")):
                continue
            inner = token.strip("{}[]<> _") or "value"
            base = _NOT_ID.sub("_", inner.casefold()).strip("_")
            base = (base if base[:1].isalpha() else f"cell_{base}".strip("_"))[:56] or "cell"
            n, field_id = 1, base
            while field_id in used:
                n += 1
                field_id = f"{base}_{n}"
            used.add(field_id)
            data = {
                "label": (a.context if has_letter(a.context) else inner)[:120],
                "description": f"The value for {inner}."[:500],
                "expected_type": "text",
                "mode": "extractive",
            }
            if twin := twins.get((_group_of(amap.format, a.anchor_id), token)):
                data = {k: getattr(twin, k) for k in data}
            added.append(
                IRField(
                    field_id=field_id,
                    anchor_id=a.anchor_id,
                    section_path=a.path,
                    required=False,
                    write_mode="replace_token",
                    constraints=Constraints(token=token),
                    **data,
                )
            )
    return added


# shortcut: English/Spanish terms only, add terms per language when a template needs them.
_NET_TERMS = tuple(
    f" {t} "
    for t in (
        "net movement",
        "net cash movement",
        "net change",
        "net cash flow",
        "net flow",
        "movimiento neto",
        "flujo neto",
        "variacion neta",
    )
)


def _names_net_movement(f: IRField) -> bool:
    """A number slot named a net movement: the LLM often calls it extractive, but no source
    states a figure that is the difference of two others, so it is computed."""
    if f.expected_type not in ("currency", "number"):
        return False
    return any(t in _padded(name) for name in _names(f) for t in _NET_TERMS)


def _names(f: IRField) -> tuple[str, ...]:
    """Label, id and, only for a bare label ("Value", "Total"), the context: a cell under a
    "NET CASH FLOW" heading is not itself the net."""
    rest = [w for w in _padded(f.label).split() if w not in _AMOUNT_WORDS | _TOTAL_WORDS]
    bare = all(w.isdigit() for w in rest)
    return (f.label, f.field_id, f.context or "" if bare else "")


def _padded(name: str) -> str:
    plain = unicodedata.normalize("NFKD", name.casefold()).encode("ascii", "ignore").decode()
    return f" {' '.join(re.findall(r'[a-z0-9]+', plain))} "


# shortcut: English/Spanish words only, add words per language when a template needs them.
_RANK_WORDS = frozenset(
    "largest top biggest highest category categories breakdown mayores principales categoria "
    "categorias desglose".split()
)


def _slot_key(f: IRField) -> tuple[str, str] | None:
    """(first word of the field_id, slot number) of a numbered slot, else None."""
    m = SLOT_NUMBER.search(f.field_id)
    return (f.field_id.split("_")[0], m[1]) if m else None


def _names_ranking(f: IRField) -> bool:
    """A numbered slot (`receipt_date_2`, `category_1_amount`) of a ranking or a breakdown: no
    source states which row is the k-th largest or a category's total, so code computes it."""
    names = (f.label, f.field_id, *f.section_path, f.context or "")
    return _slot_key(f) is not None and any(_RANK_WORDS & set(_padded(n).split()) for n in names)


def _ranking_slots(fields: list[IRField]) -> set[str]:
    """The ranking slots whose numbered grid (same field_id first word and number) holds a number
    or currency slot: a text-only list (`Top risks 1..3`) is not a computation."""
    numeric = {_slot_key(f) for f in fields if f.expected_type in ("number", "currency")}
    return {f.field_id for f in fields if _names_ranking(f) and _slot_key(f) in numeric}


# shortcut: English/Spanish words only, add words per language when a template needs them.
_IN_WORDS = frozenset(
    "deposit deposits credit credits receipt receipts inflow inflows income ingreso ingresos "
    "deposito depositos abono abonos entrada entradas cobro cobros".split()
)
_OUT_WORDS = frozenset(
    "withdrawal withdrawals debit debits payment payments outflow outflows expense expenses "
    "spending retiro retiros cargos egreso egresos salida salidas pago pagos gasto "
    "gastos".split()
)
_TOTAL_WORDS = frozenset("total totals suma totales".split())
_LARGEST_WORDS = frozenset("largest biggest highest top mayor".split())
_GROUP_WORDS = frozenset("category categories breakdown categoria categorias desglose".split())
_AMOUNT_WORDS = frozenset("amount value total importe monto valor".split())


def _words(names: tuple[str, ...]) -> tuple[set[str], str]:
    text = _padded(" ".join(names))
    return set(text.split()), text


# shortcut: English/Spanish phrases only, add phrases when a template trips a direction word.
_NOT_DIRECTION = tuple(
    f" {p} "
    for p in (
        "credit card",
        "credit limit",
        "available credit",
        "credit line",
        "debit card",
        "payment due",
        "minimum payment",
        "income statement",
        "tarjeta de credito",
        "limite de credito",
    )
)
_COUNT_WORDS = frozenset("number count cantidad numero num no".split())


def _direction(names: tuple[str, ...]) -> Literal["CR", "DR"] | None:
    """CR (money in) or DR (money out) when the names state exactly one direction."""
    words, text = _words(names)
    for phrase in _NOT_DIRECTION:
        text = text.replace(phrase, " ")
    words = set(text.split())
    ins = bool(_IN_WORDS & words) or " money in " in text
    outs = bool(_OUT_WORDS & words) or " money out " in text
    return "CR" if ins and not outs else ("DR" if outs and not ins else None)


def _is_total(f: IRField) -> bool:
    return (
        f.expected_type in ("currency", "number")
        and not _COUNT_WORDS & _words((f.label, f.field_id))[0]  # "Total number of deposits"
        and bool(_TOTAL_WORDS & _words(_names(f))[0])
        and _direction(_names(f)) is not None
    )


def _is_largest(f: IRField) -> bool:
    """An un-numbered 'largest credit' style slot: the top row of one direction."""
    return (
        f.expected_type in ("currency", "number")
        and _slot_key(f) is None
        and bool(_LARGEST_WORDS & _words(_names(f))[0])
        and _direction(_names(f)) is not None
    )


def derivation_intent(f: IRField) -> tuple[str, Literal["CR", "DR"] | None] | None:
    """(op, tag) a derived slot's names fix, or None: the binder still picks the table and the
    headers, code fixes what the slot means. A numbered slot's direction may come from its
    heading (section_path)."""
    if _names_net_movement(f):
        return "diff", None
    if _slot_key(f) is not None:
        names = (*_names(f), *f.section_path)
        words = _words(names)[0]
        if _GROUP_WORDS & words:
            return "group_sum", _direction(names)
        if _RANK_WORDS & words:
            return "top", _direction(names)
    if _is_total(f):
        return "sum", _direction(_names(f))
    if _is_largest(f):
        return "top", _direction(_names(f))
    return None


def _retype_amounts(fields: list[IRField]) -> list[IRField]:
    """A numbered ranking slot named an amount but typed text becomes currency, so the grid has
    the numeric slot that `_ranking_slots` needs (the category-name slots stay text)."""
    return [
        f.model_copy(update={"expected_type": "currency"})
        if f.expected_type == "text"
        and _names_ranking(f)
        and _GROUP_WORDS & _words((*_names(f), *f.section_path))[0]
        and _AMOUNT_WORDS & _words((f.label, f.field_id))[0]
        else f
        for f in fields
    ]


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
    fields += _empty_tables(amap, fields, used)
    fields += _token_cells(amap, fields, used)
    errors = validate(IRDraft(fields=fields), amap) if fields else []
    if not fields or errors:
        log.warning("ir_invalid", errors=_loggable(errors), anchor_count=len(amap.anchors))
        raise IRInvalid()
    # context and card are code-set, never the LLM's
    by_id = {a.anchor_id: a for a in amap.anchors}
    fields = [_with_anchor_data(f, by_id[f.anchor_id]) for f in _caption_types(fields, by_id)]
    fields = _retype_amounts(fields)
    ranking = _ranking_slots(fields)
    fields = [
        f.model_copy(update={"mode": "derived"})
        if _names_net_movement(f) or _is_total(f) or _is_largest(f) or f.field_id in ranking
        else f
        for f in fields
    ]
    return TemplateIR(fields=fields, template_id=template_id, version=1, format=amap.format)
