"""Parsers for formats that need no layout model: XLSX, CSV, TXT, MD (ADR-003)."""

import csv
import re
from io import BytesIO, StringIO

from openpyxl import load_workbook

from doc_intel.ingestion.schemas import ParsedDocument, ParsedElement

_NOT_HEADING = object()


def _atx_heading(line: str) -> str | object | None:
    """CommonMark ATX heading text (None for an empty heading), or `_NOT_HEADING`.

    Plain string operations, linear in the line length: a regex with overlapping quantifiers
    backtracked quadratically and let one crafted line stall the single job worker.
    """
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3:
        return _NOT_HEADING
    rest = stripped.lstrip("#")
    level = len(stripped) - len(rest)
    if not 1 <= level <= 6 or (rest and rest[0] not in " \t"):
        return _NOT_HEADING
    text = rest.strip(" \t")
    closed = text.rstrip("#")
    if not closed or closed[-1] in " \t":  # optional closing `#` run must follow a space
        text = closed.rstrip(" \t")
    return text or None


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n[ \t]*\n", text) if p.strip()]


def _table(rows: list[list[str]], section_heading: str | None = None) -> ParsedElement:
    return ParsedElement(element_type="table", rows=rows, section_heading=section_heading)


def parse_xlsx(data: bytes) -> ParsedDocument:
    wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
    try:
        elements = []
        for ws in wb.worksheets:
            rows = [
                ["" if v is None else str(v) for v in row]
                for row in ws.iter_rows(values_only=True)
                if any(v is not None and str(v).strip() for v in row)
            ]
            if rows:
                elements.append(_table(rows, ws.title))
    finally:
        wb.close()  # read-only workbooks keep the archive open until closed
    return ParsedDocument(elements=elements)


def parse_csv(data: bytes) -> ParsedDocument:
    rows = [row for row in csv.reader(StringIO(data.decode("utf-8-sig"))) if any(row)]
    return ParsedDocument(elements=[_table(rows)] if rows else [])


def parse_txt(data: bytes) -> ParsedDocument:
    paragraphs = _paragraphs(data.decode("utf-8-sig"))
    return ParsedDocument(elements=[ParsedElement(element_type="text", text=p) for p in paragraphs])


# shortcut: Markdown tables stay plain-text paragraphs, parse them as tables if retrieval quality
# on MD tables matters. Setext headings and `#` lines inside code fences are not special-cased.
def parse_md(data: bytes) -> ParsedDocument:
    elements: list[ParsedElement] = []
    heading: str | None = None
    block: list[str] = []

    def flush() -> None:
        elements.extend(
            ParsedElement(element_type="text", text=p, section_heading=heading)
            for p in _paragraphs("\n".join(block))
        )
        block.clear()

    for line in data.decode("utf-8-sig").splitlines():
        if (found := _atx_heading(line)) is not _NOT_HEADING:
            flush()
            heading = found
            if heading:  # heading text stays searchable, like Docling headings
                elements.append(
                    ParsedElement(element_type="text", text=heading, section_heading=heading)
                )
        else:
            block.append(line)
    flush()
    return ParsedDocument(elements=elements)
