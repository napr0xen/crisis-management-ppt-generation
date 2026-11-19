import uuid

import pytest

from doc_intel.ingestion.chunker import chunk
from doc_intel.ingestion.docling_parser import parse_docx, parse_pdf, parse_pptx
from doc_intel.ingestion.native_parsers import parse_csv, parse_md, parse_txt, parse_xlsx
from doc_intel.ingestion.schemas import ChunkPayload, ParsedDocument, ParsedElement
from tests.fixtures import sources

DOC_ID = uuid.uuid4()


def words(text: str) -> int:
    return len(text.split())


def run(doc: ParsedDocument, max_tokens: int, count=words) -> list[ChunkPayload]:
    chunks = chunk(
        doc, document_id=DOC_ID, filename="f.pdf", max_tokens=max_tokens, count_tokens=count
    )
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert all(c.text.strip() and count(c.text) <= max_tokens for c in chunks)
    return chunks


def text(t: str, section: str | None = None, page: int | None = None) -> ParsedElement:
    return ParsedElement(element_type="text", text=t, section_heading=section, page_number=page)


def table(rows: list[list[str]], section: str | None = None) -> ParsedElement:
    return ParsedElement(element_type="table", rows=rows, section_heading=section)


def test_packs_within_section_and_never_crosses_it():
    doc = ParsedDocument(
        elements=[
            text("one two", "A", 1),
            text("three four", "A", 2),
            text("five", "B", 2),
        ]
    )
    chunks = run(doc, 100)
    assert [(c.text, c.section_heading, c.page_number) for c in chunks] == [
        ("one two\n\nthree four", "A", 1),  # page of the first element
        ("five", "B", 2),
    ]
    assert all(c.element_type == "text" for c in chunks)


def test_packing_stops_at_max_tokens():
    doc = ParsedDocument(elements=[text("a b c d"), text("e f g h"), text("i j k l")])
    assert [c.text for c in run(doc, 10)] == ["a b c d\n\ne f g h", "i j k l"]


def test_metadata_carried():
    [c] = run(ParsedDocument(elements=[text("hello", "S", 3)]), 10)
    assert c.document_id == DOC_ID
    assert c.filename == "f.pdf"
    assert (c.page_number, c.section_heading, c.element_type, c.chunk_index) == (3, "S", "text", 0)


def test_table_is_its_own_markdown_chunk():
    doc = ParsedDocument(
        elements=[
            text("before", "S"),
            table([["h1", "h2"], ["a|b", "line\nbreak"]], "S"),
            text("after", "S"),
        ]
    )
    chunks = run(doc, 100)
    assert [c.element_type for c in chunks] == ["text", "table", "text"]
    assert chunks[1].text == "| h1 | h2 |\n| --- | --- |\n| a\\|b | line break |"
    assert chunks[1].section_heading == "S"


def test_long_table_split_repeats_header():
    rows = [["k", "v"]] + [[f"r{i}", str(i)] for i in range(10)]
    chunks = run(ParsedDocument(elements=[table(rows)]), 25)
    assert len(chunks) > 1
    header = "| k | v |\n| --- | --- |\n"
    assert all(c.text.startswith(header) and c.element_type == "table" for c in chunks)
    body = [line for c in chunks for line in c.text.splitlines()[2:]]
    assert body == [f"| r{i} | {i} |" for i in range(10)]


def test_oversized_row_is_split():
    rows = [["k", "v"], ["key", " ".join(f"w{i}" for i in range(60))]]
    chunks = run(ParsedDocument(elements=[table(rows)]), 20)
    assert len(chunks) > 1
    assert all(c.text.startswith("| k | v |\n| --- | --- |\n") for c in chunks)
    assert "w0" in chunks[0].text
    assert "w59" in chunks[-1].text


def test_oversized_header_still_fits():
    rows = [[f"h{i}" for i in range(20)], [str(i) for i in range(20)]]
    run(ParsedDocument(elements=[table(rows)]), 15)


def test_long_paragraph_split_on_sentences_then_words():
    sentences = "One two three four five. Six seven eight nine ten. Eleven twelve."
    chunks = run(ParsedDocument(elements=[text(sentences)]), 6)
    assert [c.text for c in chunks] == [
        "One two three four five.",
        "Six seven eight nine ten.",
        "Eleven twelve.",
    ]
    no_stops = " ".join(f"w{i}" for i in range(25))
    chunks = run(ParsedDocument(elements=[text(no_stops, page=4)]), 10)
    assert " ".join(c.text for c in chunks) == no_stops
    assert all(c.page_number == 4 for c in chunks)


def test_unbreakable_token_is_split_by_characters():
    blob = "x" * 100
    chunks = run(ParsedDocument(elements=[text(blob)]), 10, count=lambda t: len(t) // 10 + 1)
    assert "".join(c.text for c in chunks) == blob


def test_empty_elements_give_no_chunks():
    doc = ParsedDocument(elements=[text("  "), table([]), table([["h"]])])
    assert [c.text for c in run(doc, 10)] == ["| h |\n| --- |"]


def test_long_synthetic_document():
    elements = []
    for s in range(5):
        for p in range(20):
            elements.append(text(" ".join(f"s{s}p{p}w{w}." for w in range(30)), f"Sec {s}", s + 1))
        elements.append(table([["a", "b"]] + [[str(r), "x " * 10] for r in range(40)], f"Sec {s}"))
    chunks = run(ParsedDocument(elements=elements), 64)
    assert {c.section_heading for c in chunks} == {f"Sec {s}" for s in range(5)}
    for c in chunks:  # no chunk mixes sections
        assert all(w.startswith(f"s{c.section_heading[-1]}") for w in c.text.split() if "p" in w)


NATIVE = [
    (parse_xlsx, sources.xlsx),
    (parse_csv, sources.csv_),
    (parse_txt, sources.txt),
    (parse_md, sources.md),
]
DOCLING = [
    (parse_pdf, sources.text_pdf),
    (parse_pdf, sources.scanned_pdf),
    (parse_docx, sources.docx),
    (parse_pptx, sources.pptx),
]


@pytest.mark.parametrize("max_tokens", [3, 8, 512])
@pytest.mark.parametrize(("parser", "source"), NATIVE)
def test_native_fixtures_fit(parser, source, max_tokens):
    assert run(parser(source()), max_tokens)


@pytest.mark.slow
@pytest.mark.parametrize(("parser", "source"), DOCLING)
def test_docling_fixtures_fit(parser, source):
    for max_tokens in (3, 8, 512):
        assert run(parser(source()), max_tokens)


@pytest.mark.slow
def test_production_tokenizer_limit():
    from tokenizers import Tokenizer

    from doc_intel.core.config import get_settings

    tok = Tokenizer.from_pretrained(get_settings().chunk_tokenizer)

    def count(t: str) -> int:
        return len(tok.encode(t).ids)

    long_doc = ParsedDocument(
        elements=[
            text(" ".join(["Emergency logistics coordination"] * 400), "S"),
            table([["Item", "Notes"]] + [[str(i), "detail " * 30] for i in range(60)], "S"),
        ]
    )
    docs = [parser(source()) for parser, source in NATIVE + DOCLING] + [long_doc]
    for doc in docs:
        assert run(doc, 512, count)
