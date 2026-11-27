"""Turns a field mapping result into the content a writer puts at the anchor (system-design § 8)."""

import re
import unicodedata
from datetime import date

from doc_intel.mapping.derived import render_like
from doc_intel.mapping.schemas import FieldMappingResult
from doc_intel.mapping.verifier import source_text
from doc_intel.templates.schemas import IRField, TemplateFormat
from doc_intel.writers import Content

# XML 1.0 forbids these; lxml raises on them and Word/PowerPoint refuse the file.
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")


_CODE = re.compile(r"\b[A-Z]{3}\b")


def _has_marker(span: str, unit: str) -> bool:
    """The span already shows a currency symbol, a currency code or the unit itself."""
    return (
        unit in span
        or bool(_CODE.search(span))
        or any(unicodedata.category(c) == "Sc" for c in span)
    )


def _clean(text: str) -> str:
    return _ILLEGAL.sub("", text)


def _paragraphs(field: IRField, result: FieldMappingResult) -> list[str]:
    """The value as one string per paragraph, for text targets."""
    t, unit, value = field.expected_type, field.constraints.unit, result.value
    if t in ("paragraphs", "bullet_list"):
        return [str(v) for v in value]  # type: ignore[attr-defined]
    if t == "table":
        return [" | ".join(row) for row in value]  # type: ignore[attr-defined]
    # Typed values are written as the source wrote them; no evidence span (a user-set value)
    # falls back to the canonical value.
    span = source_text(value, t, [e.quote for e in result.evidence])  # type: ignore[arg-type]
    if t in ("number", "currency"):
        # shortcut: only currency; a computed count has no cell convention and plain numbers keep
        # str(value), upgrade when the result carries the rendered text.
        if not span and t == "currency" and isinstance(value, int | float):
            # a computed amount (no quote states it) in the convention and affix of the cited cells
            span = render_like(value, [p for e in result.evidence for p in e.quote.split("|")])
        text = span or str(value)
        return [f"{text} {unit}" if unit and not (span and _has_marker(span, unit)) else text]
    if t == "percent":
        return [span if span and "%" in span else f"{span or value}%"]
    # shortcut: dates follow the source span (any format that reads as the ISO date); a user-set
    # value has none and is written ISO, upgrade when the IR emits a strftime pattern.
    return [span or str(value)]


def content(
    field: IRField,
    result: FieldMappingResult,
    fmt: TemplateFormat,
    missing_marker: str,
    conflict_marker: str,
) -> Content:
    value, t = result.value, field.expected_type
    if t in ("number", "currency", "percent") and isinstance(value, str):
        t = "text"  # a table cell's word ("Preferred" under a number-typed Rating), verbatim
    if result.status == "missing":
        # replace, never str.format: the label comes from an LLM.
        items = [missing_marker.replace("{label}", field.label)]
    elif result.status == "conflict":
        items = [conflict_marker]
    elif (t == "table" and field.write_mode == "fill_table_rows") or t == "chart":
        return [[_clean(cell) for cell in row] for row in value]  # type: ignore[union-attr]
    elif fmt == "xlsx" and t in ("number", "currency"):
        return value  # type: ignore[return-value]
    elif fmt == "xlsx" and t == "percent":
        return value / 100  # type: ignore[operator]  # Excel stores 50% as 0.5
    elif fmt == "xlsx" and t == "date":
        # shortcut: 1900 date system only, date1904 workbooks get dates 4 years off; read
        # workbookPr when such templates appear.
        return (date.fromisoformat(str(value)) - date(1899, 12, 30)).days
    elif t == "text":
        items = [str(value)]
    else:
        items = _paragraphs(field, result)
    items = [_clean(i) for i in items]
    return ["; ".join(items)] if fmt == "xlsx" else items
