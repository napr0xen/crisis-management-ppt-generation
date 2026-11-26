"""PDF / DOCX / PPTX parsing through Docling (layout, TableFormer, RapidOCR) — ADR-003."""

from collections import defaultdict
from functools import cache
from io import BytesIO

from docling.datamodel.base_models import DocumentStream, InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import (
    ListItem,
    SectionHeaderItem,
    TableData,
    TableItem,
    TextItem,
    TitleItem,
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


def join_split_tables(elements: list[ParsedElement]) -> list[ParsedElement]:
    """Merge a table into the one right before it when it continues on the next page
    (same header row), so a table split across pages is one table."""
    out: list[ParsedElement] = []
    last_page: int | None = None  # page of the newest rows in out[-1]; chains span 3+ pages
    for e in elements:
        prev = out[-1] if out else None
        if (
            prev is not None
            and prev.element_type == e.element_type == "table"
            and last_page is not None
            and e.page_number == last_page + 1
            and prev.rows[0] == e.rows[0]
        ):
            out[-1] = prev.model_copy(update={"rows": prev.rows + e.rows[1:]})
        else:
            out.append(e)
        last_page = e.page_number
    return out


def _parse(data: bytes, ext: str) -> ParsedDocument:
    # The name only tells Docling the format; the real filename never reaches the parser.
    # Over MAX_PAGES Docling raises, so the document fails instead of holding the single worker
    # for hours (layout + OCR per page).
    stream = DocumentStream(name=f"document.{ext}", stream=BytesIO(data))
    doc = _converter().convert(stream, max_num_pages=MAX_PAGES).document
    elements: list[ParsedElement] = []
    heading: str | None = None
    for item, _level in doc.iterate_items():
        # PDF: page number; PPTX: slide number; DOCX: no provenance.
        page = item.prov[0].page_no if getattr(item, "prov", None) else None
        if isinstance(item, SectionHeaderItem | TitleItem):
            # Also emitted as text: the layout model sometimes labels body text as a header,
            # which would otherwise vanish from retrieval.
            if text := _clean(item.text):
                heading = text
                elements.append(
                    ParsedElement(
                        element_type="text", text=text, page_number=page, section_heading=heading
                    )
                )
        elif isinstance(item, TableItem):
            rows = _table_rows(item.data)
            if any(any(row) for row in rows):
                elements.append(
                    ParsedElement(
                        element_type="table", rows=rows, page_number=page, section_heading=heading
                    )
                )
        elif isinstance(item, TextItem) and (text := _clean(item.text)):
            if isinstance(item, ListItem):
                text = f"- {text}"  # keeps the list structure in the chunk
            elements.append(
                ParsedElement(
                    element_type="text", text=text, page_number=page, section_heading=heading
                )
            )
    return ParsedDocument(
        elements=join_split_tables(drop_repeated(elements)),
        page_count=doc.num_pages() if ext != "docx" else None,
    )


def parse_pdf(data: bytes) -> ParsedDocument:
    return _parse(data, "pdf")


def parse_docx(data: bytes) -> ParsedDocument:
    return _parse(data, "docx")


def parse_pptx(data: bytes) -> ParsedDocument:
    return _parse(data, "pptx")
