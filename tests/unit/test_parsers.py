import pytest

from doc_intel.ingestion.docling_parser import parse_docx, parse_pdf, parse_pptx
from doc_intel.ingestion.native_parsers import parse_csv, parse_md, parse_txt, parse_xlsx
from doc_intel.ingestion.schemas import ParsedDocument
from tests.fixtures import sources


def _texts(doc: ParsedDocument) -> list[str]:
    return [e.text for e in doc.elements if e.element_type == "text"]


def _tables(doc: ParsedDocument) -> list:
    return [e for e in doc.elements if e.element_type == "table"]


# --- Docling formats (slow: model download/load on first run) ---


@pytest.mark.slow
def test_pdf_text_pages_and_headings():
    doc = parse_pdf(sources.text_pdf())
    assert doc.page_count == 2
    joined = " ".join(_texts(doc))
    assert "flood response began on Monday" in joined
    assert "Recovery funding was approved" in joined
    # Headings are also emitted as text: mislabeled body text must stay searchable.
    heading = next(e for e in doc.elements if e.text == "Incident Overview")
    assert heading.section_heading == "Incident Overview"
    assert {e.page_number for e in doc.elements} == {1, 2}
    first = next(e for e in doc.elements if "flood response" in e.text)
    assert first.page_number == 1
    assert first.section_heading == "Incident Overview"
    assert all(e.text == e.text.strip() and "  " not in e.text for e in doc.elements)


@pytest.mark.slow
def test_scanned_pdf_is_ocrd():
    doc = parse_pdf(sources.scanned_pdf())
    assert doc.page_count == 1
    joined = " ".join(_texts(doc)).lower()
    assert "volunteers" in joined
    assert "blankets" in joined
    assert all(e.page_number == 1 for e in doc.elements)


@pytest.mark.slow
def test_docx_headings_tables_no_pages():
    doc = parse_docx(sources.docx())
    assert doc.page_count is None
    assert all(e.page_number is None for e in doc.elements)
    by_text = {e.text: e for e in doc.elements if e.element_type == "text"}
    assert by_text["The storm caused widespread outages."].section_heading == "Executive Summary"
    assert by_text["Each region reported its status below."].section_heading == "Regional Status"
    [table] = _tables(doc)
    assert table.rows == sources.TABLE
    assert table.text == ""
    assert table.section_heading == "Regional Status"


@pytest.mark.slow
def test_pptx_slides():
    doc = parse_pptx(sources.pptx())
    assert doc.page_count == 2
    [body] = [e for e in doc.elements if "Rivers peaked" in e.text]
    assert body.page_number == 1
    assert body.section_heading == "Situation Update"
    [table] = _tables(doc)
    assert table.rows == sources.TABLE
    assert table.page_number == 2
    assert table.section_heading == "Response Teams"


# --- Native formats ---


def test_xlsx_sheets_as_tables():
    doc = parse_xlsx(sources.xlsx())
    assert doc.page_count is None
    shelters, supplies = doc.elements  # empty sheet is skipped
    assert shelters.element_type == "table"
    assert shelters.section_heading == "Shelters"
    assert shelters.rows == [["Name", "Capacity"], ["Town Hall", "120"], ["School Gym", "80"]]
    assert supplies.section_heading == "Supplies"
    assert supplies.rows == [["Item", "Count"], ["Water", "500"]]
    assert all(e.page_number is None for e in doc.elements)


def test_csv_single_table():
    doc = parse_csv(sources.csv_())
    [table] = doc.elements
    assert table.rows == sources.TABLE  # BOM stripped
    assert table.page_number is None
    assert table.section_heading is None
    assert doc.page_count is None


def test_txt_paragraphs():
    doc = parse_txt(sources.txt())
    assert _texts(doc) == ["First paragraph line one.\nline two.", "Second paragraph."]
    assert all(e.page_number is None and e.section_heading is None for e in doc.elements)
    assert doc.page_count is None


def test_md_headings_become_sections():
    doc = parse_md(sources.md())
    got = [(e.section_heading, e.text) for e in doc.elements]
    assert got == [
        (None, "Intro before any heading."),
        ("Overview", "Overview"),
        ("Overview", "The overview text."),
        ("Overview", "More overview."),
        ("Details", "Details"),
        ("Details", "| a | b |\n|---|---|\n| 1 | 2 |"),
    ]
    assert all(e.element_type == "text" and e.page_number is None for e in doc.elements)


@pytest.mark.parametrize("parser", [parse_txt, parse_md, parse_csv])
def test_invalid_utf8_raises(parser):
    with pytest.raises(UnicodeDecodeError):
        parser(b"bad \xff\xfe bytes")


def test_empty_text_gives_no_elements():
    assert parse_txt(b"\n\n  \n").elements == []


@pytest.mark.parametrize(
    "line",
    ["# a" + " " * 1_000_000 + "b", "#" + " \t" * 500_000 + "x", "# a" + " #" * 500_000 + " x"],
)
def test_md_heading_parsing_is_linear_time(line):
    """A crafted 1 MB line must not stall the single job worker (was quadratic backtracking)."""
    import time

    start = time.perf_counter()
    parse_md(line.encode())
    assert time.perf_counter() - start < 0.5


@pytest.mark.parametrize(
    "line, heading",
    [
        ("# Title", "Title"),
        ("## Title ##", "Title"),
        ("   ### Indented", "Indented"),
        ("#\tTabbed  ", "Tabbed"),
        ("## C# basics", "C# basics"),
        ("# Ends with#", "Ends with#"),
        ("#", None),
        ("#hashtag", "NOT A HEADING"),
        ("    # four spaces is code", "NOT A HEADING"),
        ("####### seven", "NOT A HEADING"),
    ],
)
def test_md_atx_heading_rules(line, heading):
    doc = parse_md(f"{line}\n\nbody".encode())
    body = doc.elements[-1]
    if heading == "NOT A HEADING":
        assert body.section_heading is None
    else:
        assert body.section_heading == heading


def test_pdf_over_the_page_cap_fails(monkeypatch):
    from doc_intel.ingestion import docling_parser

    monkeypatch.setattr(docling_parser, "MAX_PAGES", 1)
    with pytest.raises(Exception):  # noqa: B017 (Docling's own error type; pipeline wraps it)
        parse_pdf(sources.text_pdf())
