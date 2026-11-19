"""Turns a field mapping result into the content a writer puts at the anchor (system-design § 8)."""

import re
from datetime import date

from doc_intel.mapping.schemas import FieldMappingResult
from doc_intel.templates.schemas import IRField, TemplateFormat
from doc_intel.writers import Content

# XML 1.0 forbids these; lxml raises on them and Word/PowerPoint refuse the file.
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")


def _clean(text: str) -> str:
    return _ILLEGAL.sub("", text)


def _paragraphs(field: IRField, value: object) -> list[str]:
    """The value as one string per paragraph, for text targets."""
    t, unit = field.expected_type, field.constraints.unit
    if t in ("paragraphs", "bullet_list"):
        return [str(v) for v in value]  # type: ignore[attr-defined]
    if t == "table":
        return [" | ".join(row) for row in value]  # type: ignore[attr-defined]
    if t in ("number", "currency"):
        return [f"{value} {unit}" if unit else str(value)]
    if t == "percent":
        return [f"{value}%"]
    # shortcut: date_format is free text from the LLM, not a strftime pattern; dates are written
    # ISO, upgrade when the IR emits a strftime pattern.
    return [str(value)]


def content(
    field: IRField,
    result: FieldMappingResult,
    fmt: TemplateFormat,
    missing_marker: str,
    conflict_marker: str,
) -> Content:
    value, t = result.value, field.expected_type
    if result.status == "missing":
        # replace, never str.format: the label comes from an LLM.
        items = [missing_marker.replace("{label}", field.label)]
    elif result.status == "conflict":
        items = [conflict_marker]
    elif t == "table" and field.write_mode == "fill_table_rows":
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
        items = _paragraphs(field, value)
    items = [_clean(i) for i in items]
    return ["; ".join(items)] if fmt == "xlsx" else items
