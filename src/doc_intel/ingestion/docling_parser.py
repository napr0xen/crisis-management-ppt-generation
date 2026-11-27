"""PDF / DOCX / PPTX parsing through Docling (layout, TableFormer, RapidOCR) — ADR-003."""

import re
from collections import defaultdict
from functools import cache
from io import BytesIO

from docling.datamodel.base_models import DocumentStream, InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import (
    ContentLayer,
    CoordOrigin,
    DocItemLabel,
    ListItem,
    SectionHeaderItem,
    TableData,
    TableItem,
    TextItem,
    TitleItem,
)

from doc_intel.ingestion.phrases import (
    is_amount,
    is_carry_forward,
    is_page_number,
    is_strong_amount,
)
from doc_intel.ingestion.schemas import ParsedDocument, ParsedElement


@cache
def _converter() -> DocumentConverter:
    options = PdfPipelineOptions(
        do_ocr=True, do_table_structure=True, ocr_options=RapidOcrOptions()
    )
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.DOCX, InputFormat.PPTX],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
    )


# shortcut: a fixed page cap, not a time budget; add a per-job timeout if smaller files stall.
MAX_PAGES = 1000


def _clean(text: str) -> str:
    return " ".join(text.split())


def drop_repeated(elements: list[ParsedElement]) -> list[ParsedElement]:
    """Drop running headers/footers (text on >= 3 pages and on at least half of all pages),
    keeping the first copy: a vendor or project name repeated on every slide stays findable."""
    pages: dict[str, set[int]] = defaultdict(set)
    for e in elements:
        if e.element_type == "text" and e.page_number is not None:
            pages[e.text].add(e.page_number)
    total = len({e.page_number for e in elements if e.page_number is not None})
    repeated = {t for t, p in pages.items() if len(p) >= 3 and len(p) * 2 >= total}
    seen: set[str] = set()
    kept = []
    for e in elements:
        if e.element_type == "text" and e.text in repeated:
            if e.text in seen:
                continue
            seen.add(e.text)
        kept.append(e)
    return kept


def _table_rows(data: TableData) -> list[list[str]]:
    """Each cell's text once, at its top-left slot; `data.grid` repeats it in every spanned slot."""
    rows = [[""] * data.num_cols for _ in range(data.num_rows)]
    for cell in data.table_cells:
        r, c = cell.start_row_offset_idx, cell.start_col_offset_idx
        if r < data.num_rows and c < data.num_cols:
            rows[r][c] = _clean(cell.text)
    return rows


Box = tuple[float, float, float, float]

# fractions of the page height; shortcut: fixed bands, upgrade to per-document margins if
# a layout puts real content in the top or bottom 8%.
HEADER_BAND = 0.08
FOOTER_BAND = 0.92
_LABEL_MAX_WORDS = 6  # a longer item is usually several merged labels
_CODE = re.compile(r"^(?:[A-Z]{3}|CR|DR)$")  # a currency code or tag alone is no label


def _value_like(text: str) -> bool:
    return is_amount(text)


def _label_like(text: str) -> bool:
    return (
        any(c.isalpha() for c in text)
        and not _value_like(text)
        and not _CODE.match(text.strip())  # shortcut: a bare 3-capital label (VAT) is not paired
        and len(text.split()) <= _LABEL_MAX_WORDS
    )


def _same_line(a: Box, b: Box) -> bool:
    return min(a[3], b[3]) - max(a[1], b[1]) > 0.5 * min(a[3] - a[1], b[3] - b[1])


def _x_overlap(a: Box, b: Box) -> float:
    return min(a[2], b[2]) - max(a[0], b[0])


def _kv(label: ParsedElement, value: ParsedElement, region: str) -> ParsedElement:
    name = label.text.rstrip(":").rstrip()
    lb, vb = label.bbox, value.bbox
    if lb is None or vb is None:
        raise ValueError("kv needs bboxes")  # _pairable guarantees them
    box = (min(lb[0], vb[0]), min(lb[1], vb[1]), max(lb[2], vb[2]), max(lb[3], vb[3]))
    return ParsedElement(
        element_type="kv",
        text=f"{name}: {value.text}",
        label=name,
        value=value.text,
        page_number=label.page_number,
        section_heading=label.section_heading,
        region=region,
        bbox=box,
    )


def _pairable(e: ParsedElement, headings: set[int]) -> bool:
    return (
        e.element_type == "text"
        and e.region == "body"
        and e.bbox is not None
        and e.page_number is not None
        and id(e) not in headings
    )


def _line(run: list[ParsedElement], i: int) -> list[ParsedElement]:
    """Items from `i` on that sit on the same line, left to right."""
    out = [run[i]]
    for e in run[i + 1 :]:
        if not _same_line(out[0].bbox, e.bbox) or e.bbox[0] < out[-1].bbox[0]:
            break
        out.append(e)
    return out


def _columns(labels: list[ParsedElement], values: list[ParsedElement]) -> bool:
    """Each label has its own value under it (x-overlap, one to one)."""
    best = [
        max(range(len(values)), key=lambda j: _x_overlap(lb.bbox, values[j].bbox)) for lb in labels
    ]
    return (
        len(set(best)) == len(labels)
        and all(_x_overlap(lb.bbox, values[j].bbox) > 0 for lb, j in zip(labels, best, strict=True))
        and best == sorted(best)
    )


# shortcut: more cells than this on one line fail closed (no split), upgrade if real statements
# put more than a few dozen text cells on one line.
MAX_LINE_CELLS = 200
Cells = list[tuple[str, Box]]  # a page's text cells (layout clusters), top-left origin


def _split_merged(
    label: ParsedElement, vals: list[ParsedElement], cells: Cells
) -> list[ParsedElement] | None:
    """A label item that Docling merged from several columns, split by the real text cells inside
    it: each cell must overlap exactly one value column, in x order, every column getting a cell,
    and the cells must spell the item's text. None (fail closed) otherwise; text is never split
    by guessing, so no amount can land under the wrong label."""
    line = sorted((c for c in cells if _same_line(label.bbox, c[1])), key=lambda c: c[1][0])
    if len(line) > MAX_LINE_CELLS:
        return None
    runs = []
    for i in range(len(line)):
        text = ""
        for j in range(i, len(line)):
            text = f"{text} {line[j][0]}" if j > i else line[j][0]
            if not label.text.startswith(text) or len(text) > len(label.text):
                break  # no longer a prefix of the label: no longer run can match
            if text == label.text:
                runs.append(line[i : j + 1])
    if len(runs) != 1:  # no cell run spells the text, or it is ambiguous
        return None
    inside = runs[0]
    cols: list[list[tuple[str, Box]]] = [[] for _ in vals]
    for t, b in inside:
        hit = [j for j, v in enumerate(vals) if _x_overlap(b, v.bbox) > 0]
        if len(hit) != 1:
            return None
        cols[hit[0]].append((t, b))
    if not all(cols) or [c[0][1][0] for c in cols] != sorted(c[0][1][0] for c in cols):
        return None
    out = []
    for col, v in zip(cols, vals, strict=True):
        bs = [b for _, b in col]
        part = ParsedElement(
            element_type="text",
            text=" ".join(t for t, _ in col),
            page_number=label.page_number,
            section_heading=label.section_heading,
            bbox=(
                min(b[0] for b in bs),
                min(b[1] for b in bs),
                max(b[2] for b in bs),
                max(b[3] for b in bs),
            ),
        )
        out.append(_kv(part, v, "summary"))
    return out


def _pair_run(run: list[ParsedElement], headings: set[int], cells: Cells) -> list[ParsedElement]:
    out: list[ParsedElement] = []
    i = 0
    while i < len(run):
        e = run[i]
        if not _pairable(e, headings) or _value_like(e.text):
            out.append(e)
            i += 1
            continue
        nxt = run[i + 1] if i + 1 < len(run) else None
        below_line = _line(run, i + 1) if nxt else []
        if len(below_line) >= 2 and all(
            _pairable(v, headings) and _value_like(v.text) and not _same_line(e.bbox, v.bbox)
            for v in below_line
        ):
            if parts := _split_merged(e, below_line, cells):
                out += parts
                i += 1 + len(below_line)
                continue
            out.append(e)  # values in several columns we cannot split: pair none
            i += 1
            continue
        if not _label_like(e.text):
            out.append(e)
            i += 1
            continue
        line = _line(run, i)
        n = len(line)
        if n >= 2 and all(_label_like(x.text) and _pairable(x, headings) for x in line):
            vals = run[i + n : i + 2 * n]
            if (
                len(vals) == n
                and all(_pairable(v, headings) and _value_like(v.text) for v in vals)
                and all(_same_line(vals[0].bbox, v.bbox) for v in vals)
                and _columns(line, vals)
            ):
                out += [_kv(lb, v, "summary") for lb, v in zip(line, vals, strict=True)]
                i += 2 * n
                continue
        nxt = run[i + 1] if i + 1 < len(run) else None
        before = run[i - 1] if i else None
        crowded = bool(  # another label sits to its left on the line: which one owns the value?
            before and _label_like(before.text) and _same_line(before.bbox, e.bbox)
        )
        if nxt and _pairable(nxt, headings) and _value_like(nxt.text):
            lb, vb = e.bbox, nxt.bbox
            right = _same_line(lb, vb) and vb[0] >= lb[0] and not crowded
            below = (
                not _same_line(lb, vb)
                and _x_overlap(lb, vb) > 0
                and 0 <= vb[1] - lb[3] <= 3 * (lb[3] - lb[1])
            )
            if right or below:
                out.append(_kv(e, nxt, "kv"))
                i += 2
                continue
        out.append(e)
        i += 1
    return out


def pair_kv(
    elements: list[ParsedElement], headings: set[int], cells: dict[int, Cells]
) -> list[ParsedElement]:
    """Replace label/value text items that sit together on a page by `kv` elements."""
    out: list[ParsedElement] = []
    run: list[ParsedElement] = []

    def flush() -> None:
        out.extend(_pair_run(run, headings, cells.get(run[0].page_number, []) if run else []))
        run.clear()

    for e in elements:
        if run and e.page_number != run[0].page_number:
            flush()
        if _pairable(e, headings):
            run.append(e)
        else:
            flush()
            out.append(e)
    flush()
    return out


def strip_banner(rows: list[list[str]], data: TableData) -> tuple[str | None, list[list[str]]]:
    """A row 0 with one non-empty cell over 2+ columns names the table: (caption, rows after it).
    Docling must agree: that cell spans 2+ columns or is the only cell of row 0, so a header row
    with an empty cell (`Item | `) stays a header."""
    top = [c for c in data.table_cells if c.start_row_offset_idx == 0]
    spans = len(top) == 1 or any(c.end_col_offset_idx - c.start_col_offset_idx >= 2 for c in top)
    filled = [c for c in rows[0] if c]
    if len(filled) == 1 and len(rows[0]) >= 2 and len(rows) >= 3 and spans:
        rest = rows[1:]
        if any(is_strong_amount(c) for c in rest[0]):  # an amount row is data: no header
            rest = [[""] * len(rest[0]), *rest]
        return filled[0], rest
    return None, rows


def _skippable(e: ParsedElement, running: set[str]) -> bool:
    """May sit between two parts of one table: a footer, a running header (same text on another
    page) or page number, a carry-forward / continuation line. A one-off header is a new section."""
    if e.region == "header":
        return e.text in running or is_page_number(e.text)
    return e.region == "footer" or (e.element_type in ("text", "kv") and is_carry_forward(e.text))


def join_split_tables(elements: list[ParsedElement]) -> list[ParsedElement]:
    """Merge a table into the previous one when it continues on the next page (same header row),
    across running headers/footers and carry-forward lines (which stay after the merged table)."""
    out: list[ParsedElement] = []
    head: int | None = None  # index in `out` of the newest table
    last_page: int | None = None  # page of the newest rows in it; chains span 3+ pages
    last_box: Box | None = None  # its bbox
    settled = 0  # elements before this index were checked when the newest part joined
    header_pages: dict[str, set[int | None]] = defaultdict(set)
    for x in elements:
        if x.region == "header":
            header_pages[x.text].add(x.page_number)
    # shortcut: exact text, so a header that changes per page ("… Page 2 of 4") blocks a join;
    # loosening it could merge two accounts' tables, upgrade with a page-number-aware key.
    running = {t for t, p in header_pages.items() if len(p) >= 2}

    def between(x: ParsedElement) -> bool:
        # text above the table on its own page is only emitted after it by reading order
        above = x.page_number == last_page and x.bbox and last_box and x.bbox[3] <= last_box[1]
        return not (_skippable(x, running) or above)

    for e in elements:
        if e.element_type != "table":
            out.append(e)
            continue
        prev = out[head] if head is not None else None
        if (
            prev is not None
            and last_page is not None
            and e.page_number == last_page + 1
            and any(prev.rows[0])  # headerless tables are unrelated
            and e.caption in (None, prev.caption)  # another title is another table
            and prev.rows[0] == e.rows[0]
            and not any(between(x) for x in out[settled:])
        ):
            pages = prev.row_pages or [prev.page_number] * (len(prev.rows) - 1)
            out[head] = prev.model_copy(
                update={
                    "rows": prev.rows + e.rows[1:],
                    "row_pages": pages + [e.page_number] * (len(e.rows) - 1),
                }
            )
        else:
            out.append(e)
            head = len(out) - 1
        last_page, last_box, settled = e.page_number, e.bbox, len(out)
    return out


def _page_cells(result) -> dict[int, Cells]:
    """Text cells with bboxes that the layout step keeps per page (`result.pages`)."""
    out: dict[int, Cells] = {}
    for pg in getattr(result, "pages", None) or []:
        layout = pg.predictions.layout
        seen: set[int] = set()
        for cluster in layout.clusters if layout else []:
            for c in cluster.cells:
                if c.index in seen:
                    continue
                seen.add(c.index)
                b = c.rect.to_bounding_box()
                if b.coord_origin != CoordOrigin.TOPLEFT:
                    b = b.to_top_left_origin(pg.size.height)
                out.setdefault(pg.page_no, []).append((_clean(c.text), (b.l, b.t, b.r, b.b)))
    return out


def _box(item, doc, page: int | None) -> Box | None:
    """`prov[0].bbox` in PDF points with a top-left origin."""
    if page is None:
        return None
    b = item.prov[0].bbox
    if b.coord_origin != CoordOrigin.TOPLEFT:
        b = b.to_top_left_origin(doc.pages[page].size.height)
    return (b.l, b.t, b.r, b.b)


def _region(item) -> str:
    """Docling's own page header / footer label; band candidates are settled by `confirm_bands`."""
    if item.label == DocItemLabel.PAGE_HEADER:
        return "header"
    return "footer" if item.label == DocItemLabel.PAGE_FOOTER else "body"


def _band(box: Box | None, height: float | None) -> str | None:
    if box and height:
        if box[3] <= HEADER_BAND * height:
            return "header"
        if box[1] >= FOOTER_BAND * height:
            return "footer"
    return None


def confirm_bands(elements: list[ParsedElement], bands: dict[int, str]) -> list[ParsedElement]:
    """A text in the top / bottom band is a header / footer only when it is a page number or its
    text without digits sits in that band on 2+ pages; a one-off line there stays body text."""
    key = lambda e: re.sub(r"\d+", "", e.text)  # noqa: E731
    pages: dict[tuple[str, str], set[int | None]] = defaultdict(set)
    for e in elements:
        if id(e) in bands:
            pages[bands[id(e)], key(e)].add(e.page_number)
    return [
        e.model_copy(update={"region": bands[id(e)]})
        if id(e) in bands and (is_page_number(e.text) or len(pages[bands[id(e)], key(e)]) >= 2)
        else e
        for e in elements
    ]


def _parse(data: bytes, ext: str) -> ParsedDocument:
    # The name only tells Docling the format; the real filename never reaches the parser.
    # Over MAX_PAGES Docling raises, so the document fails instead of holding the single worker
    # for hours (layout + OCR per page).
    stream = DocumentStream(name=f"document.{ext}", stream=BytesIO(data))
    result = _converter().convert(stream, max_num_pages=MAX_PAGES)
    doc = result.document
    elements: list[ParsedElement] = []
    headings: set[int] = set()  # ids of heading elements: never paired
    bands: dict[int, str] = {}  # ids of text in the top / bottom band, to be confirmed
    heading: str | None = None
    pdf = ext == "pdf"  # regions, bbox and kv need PDF page geometry
    # Page headers/footers are Docling's furniture layer, left out of iterate_items by default.
    layers = {ContentLayer.BODY, ContentLayer.FURNITURE} if pdf else {ContentLayer.BODY}
    for item, _level in doc.iterate_items(included_content_layers=layers):
        # PDF: page number; PPTX: slide number; DOCX: no provenance.
        page = item.prov[0].page_no if getattr(item, "prov", None) else None
        box = _box(item, doc, page) if pdf else None
        height = doc.pages[page].size.height if box else None
        if isinstance(item, SectionHeaderItem | TitleItem):
            # Also emitted as text: the layout model sometimes labels body text as a header,
            # which would otherwise vanish from retrieval.
            if text := _clean(item.text):
                heading = text
                elements.append(
                    ParsedElement(
                        element_type="text",
                        text=text,
                        page_number=page,
                        section_heading=heading,
                        bbox=box,
                    )
                )
                headings.add(id(elements[-1]))
        elif isinstance(item, TableItem):
            rows = _table_rows(item.data)
            if any(any(row) for row in rows):
                caption, rows = strip_banner(rows, item.data)
                if (
                    pdf
                    and caption is None
                    and not any(c.column_header for c in item.data.table_cells)
                ):
                    # shortcut: trusts TableFormer's header flags; a real header it misses loses its
                    # names (derived slots fail closed), upgrade if live tables show that.
                    rows = [[""] * len(rows[0]), *rows]  # no header row (a key/value block)
                elements.append(
                    ParsedElement(
                        element_type="table",
                        rows=rows,
                        page_number=page,
                        section_heading=heading,
                        region="table" if pdf else "body",
                        bbox=box,
                        caption=caption,
                    )
                )
        elif isinstance(item, TextItem) and (text := _clean(item.text)):
            if isinstance(item, ListItem):
                text = f"- {text}"  # keeps the list structure in the chunk
            elements.append(
                ParsedElement(
                    element_type="text",
                    text=text,
                    page_number=page,
                    section_heading=heading,
                    region=_region(item) if pdf else "body",
                    bbox=box,
                )
            )
            if pdf and elements[-1].region == "body" and (band := _band(box, height)):
                bands[id(elements[-1])] = band
    if pdf:
        # a confirmed element is a copy: ids of unchanged ones (headings) stay valid
        elements = pair_kv(confirm_bands(elements, bands), headings, _page_cells(result))
    return ParsedDocument(
        # join first: it needs every copy of a running header to see that it is running
        elements=drop_repeated(join_split_tables(elements)),
        page_count=doc.num_pages() if ext != "docx" else None,
    )


def parse_pdf(data: bytes) -> ParsedDocument:
    return _parse(data, "pdf")


def parse_docx(data: bytes) -> ParsedDocument:
    return _parse(data, "docx")


def parse_pptx(data: bytes) -> ParsedDocument:
    return _parse(data, "pptx")
