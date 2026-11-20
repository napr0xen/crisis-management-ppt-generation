"""PDF / DOCX / PPTX parsing through Docling (layout, TableFormer, RapidOCR) — ADR-003."""

from functools import cache
from io import BytesIO

from docling.datamodel.base_models import DocumentStream, InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import SectionHeaderItem, TableItem, TextItem, TitleItem

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
            rows = [[_clean(cell.text) for cell in row] for row in item.data.grid]
            if any(any(row) for row in rows):
                elements.append(
                    ParsedElement(
                        element_type="table", rows=rows, page_number=page, section_heading=heading
                    )
                )
        elif isinstance(item, TextItem) and (text := _clean(item.text)):
            elements.append(
                ParsedElement(
                    element_type="text", text=text, page_number=page, section_heading=heading
                )
            )
    return ParsedDocument(elements=elements, page_count=doc.num_pages() if ext != "docx" else None)


def parse_pdf(data: bytes) -> ParsedDocument:
    return _parse(data, "pdf")


def parse_docx(data: bytes) -> ParsedDocument:
    return _parse(data, "docx")


def parse_pptx(data: bytes) -> ParsedDocument:
    return _parse(data, "pptx")
