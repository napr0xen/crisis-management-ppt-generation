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


def test_oversized_header_table_keeps_column_value_pairs_in_every_chunk():
    # a header too wide to repeat: each row is written as `header: cell` pairs instead, so a
    # chunk after the first still says which column every value belongs to
    rows = [[f"h{i}" for i in range(8)], [f"a{i}" for i in range(8)], [f"b{i}" for i in range(8)]]
    chunks = run(ParsedDocument(elements=[table(rows)]), 12)
    assert len(chunks) > 1
    body = " ".join(c.text for c in chunks)
    assert all(f"h{i}: a{i}" in body and f"h{i}: b{i}" in body for i in range(8))
    assert all(": " in c.text and c.rows is None for c in chunks)
    # a row wider than the header keeps its extra cells, named like the row keys (`col<j>`)
    wide = [[f"h{i}" for i in range(8)], [*(f"a{i}" for i in range(8)), "extra9"]]
    assert "col9: extra9" in " ".join(
        c.text for c in run(ParsedDocument(elements=[table(wide)]), 12)
    )


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


def _chunk(text="body", heading="Impact", filename="ops.pdf"):
    return ChunkPayload(
        document_id=DOC_ID,
        filename=filename,
        page_number=1,
        section_heading=heading,
        element_type="text",
        chunk_index=0,
        text=text,
    )


def test_embed_text_prefixes_filename_and_section():
    from doc_intel.ingestion.chunker import embed_text

    assert embed_text(_chunk()) == "ops.pdf > Impact\nbody"
    assert embed_text(_chunk(heading=None)) == "ops.pdf\nbody"


def _el(text, page):
    return ParsedElement(element_type="text", text=text, page_number=page)


def test_drop_repeated_removes_running_headers_only():
    from doc_intel.ingestion.docling_parser import drop_repeated

    header = [_el("ACME CONFIDENTIAL", p) for p in (1, 2, 3, 4)]
    body = [_el(f"body {p}", p) for p in (1, 2, 3, 4)]
    sparse = [_el("Appendix", p) for p in (1, 2, 3)]  # 3 pages but under half of 8
    tail = [_el(f"tail {p}", p) for p in (5, 6, 7, 8)]
    out = drop_repeated([*header, *body, *sparse, *tail])
    assert [e.text for e in out if e.text == "ACME CONFIDENTIAL"] == ["ACME CONFIDENTIAL"]
    assert out[0] == header[0]  # the first copy stays, so a repeated vendor name is findable
    assert len(out) == 1 + len(body) + len(sparse) + len(tail)


def test_drop_repeated_needs_three_pages_and_ignores_unpaged_elements():
    from doc_intel.ingestion.docling_parser import drop_repeated

    two_pages = [_el("Footer", 1), _el("Footer", 2)]
    assert drop_repeated(two_pages) == two_pages
    unpaged = [_el("Same", None) for _ in range(5)]
    assert drop_repeated(unpaged) == unpaged
    assert drop_repeated([]) == []


# --- Phase 23: provenance ids ---------------------------------------------------------------


def tbl(rows, page=None, section=None) -> ParsedElement:
    return ParsedElement(element_type="table", rows=rows, page_number=page, section_heading=section)


def test_table_rows_get_ids_per_page_and_count_tables_per_page():
    doc = ParsedDocument(
        elements=[
            tbl([["Item", "Amt"], ["a", "1"]], page=1),
            tbl([["Item", "Amt"], ["b", "2"], ["c", "3"]], page=1),
            tbl([["Item", "Amt"], ["d", "4"]], page=2),
        ]
    )
    chunks = run(doc, 100)
    assert [[r.id for r in c.rows] for c in chunks] == [
        ["P1-T1-R01"],
        ["P1-T2-R01", "P1-T2-R02"],
        ["P2-T1-R01"],
    ]
    assert chunks[1].rows[1].cells == {"Item": "c", "Amt": "3"}


def test_ids_drop_the_page_segment_without_pages_and_tables_count_document_wide():
    doc = ParsedDocument(
        elements=[
            text("intro words", "S"),
            tbl([["h"], ["x"]], section="S"),
            tbl([["h"], ["y"]], section="T"),
            text("outro", "T"),
        ]
    )
    chunks = run(doc, 100)
    assert chunks[0].block_ids == ["B01"]
    assert [r.id for r in chunks[1].rows] == ["T1-R01"]
    assert [r.id for r in chunks[2].rows] == ["T2-R01"]
    assert chunks[3].block_ids == ["B02"]


def test_split_table_pieces_carry_only_their_own_rows_and_repeat_the_header():
    rows = [["k", "v"]] + [[f"row{i}", "x y"] for i in range(1, 7)]
    chunks = run(ParsedDocument(elements=[tbl(rows, page=3)]), 20)
    assert len(chunks) > 1
    seen = []
    for c in chunks:
        lines = c.text.split("\n")
        assert lines[0] == "| k | v |"
        assert len(lines) - 2 == len(c.rows)
        assert [r.cells["k"] for r in c.rows] == [ln.split(" | ")[0][2:] for ln in lines[2:]]
        seen += [r.id for r in c.rows]
    assert seen == [f"P3-T1-R{i:02d}" for i in range(1, 7)]


def test_empty_and_duplicate_headers_become_col_j():
    chunks = run(ParsedDocument(elements=[tbl([["Name", "", "Name"], ["a", "b", "c"]])]), 100)
    assert chunks[0].rows[0].cells == {"Name": "a", "col2": "b", "col3": "c"}


def test_header_whitespace_is_collapsed_in_cell_keys():
    chunks = run(ParsedDocument(elements=[tbl([["Net   \n movement"], ["5"]])]), 100)
    assert chunks[0].rows[0].cells == {"Net movement": "5"}


def test_packed_text_chunk_lists_its_blocks_and_a_split_block_repeats_its_id():
    doc = ParsedDocument(
        elements=[
            text("one two", "A", 1),
            text("three four", "A", 1),
            text("a b c d e f g h", "A", 1),
        ]
    )
    chunks = run(doc, 4)
    assert chunks[0].block_ids == ["P1-B01", "P1-B02"]
    assert [c.block_ids for c in chunks[1:]] == [["P1-B03"]] * (len(chunks) - 1)


def test_shortcut_chunks_have_no_rows():
    big_header = [["h1 h2 h3 h4 h5 h6", "h7"], ["a", "b"], ["c", "d"]]
    assert all(c.rows is None for c in run(ParsedDocument(elements=[tbl(big_header)]), 8))
    oversized = [["k"], ["one two three four five six seven eight"], ["z"]]
    chunks = run(ParsedDocument(elements=[tbl(oversized)]), 6)
    assert len(chunks) > 1
    assert any(c.rows is None for c in chunks)
    assert all(c.rows is None or c.rows for c in chunks)


def test_chunk_text_has_no_ids_and_text_chunks_have_no_rows():
    chunks = run(
        ParsedDocument(elements=[text("hello world", None, 1), tbl([["h"], ["x"]], 1)]), 50
    )
    assert chunks[0].rows is None
    assert chunks[1].block_ids == []
    assert chunks[1].text == "| h |\n| --- |\n| x |"


def test_parse_and_chunk_gives_a_csv_table_row_ids():
    from doc_intel.ingestion.pipeline import parse_and_chunk

    _, chunks = parse_and_chunk(
        b"name,amount\nrent,10\ntax,20\n",
        "csv",
        document_id=DOC_ID,
        filename="f.csv",
        max_tokens=100,
        tokenizer_model="Qwen/Qwen3-Embedding-0.6B",
    )
    rows = [r for c in chunks if c.rows for r in c.rows]
    assert [r.id for r in rows] == ["T1-R01", "T1-R02"]
    assert rows[1].cells == {"name": "tax", "amount": "20"}


# ---- Phase 27 -----------------------------------------------------------------------------------
def test_footer_not_chunked_kv_packed_as_lines():
    kv = lambda label, value: ParsedElement(  # noqa: E731
        element_type="kv", text=f"{label}: {value}", label=label, value=value, region="kv"
    )
    foot = ParsedElement(element_type="text", text="Pay soon", region="footer")
    doc = ParsedDocument(elements=[kv("Opening", "1,00"), kv("Closing", "2,00"), foot])
    (c,) = run(doc, 100)
    assert c.element_type == "text" and c.text == "Opening: 1,00\nClosing: 2,00"
    assert c.block_ids == ["B01", "B02"]


def test_caption_is_table_heading():
    e = ParsedElement(
        element_type="table", rows=[["A", "B"], ["1", "2"]], section_heading="S", caption="Cap"
    )
    (c,) = run(ParsedDocument(elements=[e]), 100)
    assert c.section_heading == "Cap"


def test_row_roles_tagged():
    rows = [
        ["Item", "Amt"],
        ["Fees", "1"],
        ["Total charged this period", "1"],
        ["Total Energies", "5"],
        ["Balance Brought Forward:", "2"],
        ["Saldo anterior", "3"],
    ]
    (c,) = run(ParsedDocument(elements=[table(rows)]), 200)
    assert [r.row_role for r in c.rows] == [
        "data",
        "total",
        "data",
        "carry_forward",
        "carry_forward",
    ]


def test_headerless_banner_table_total_not_double_counted():
    from doc_intel.embeddings.schemas import Hit
    from doc_intel.mapping.derived import Computed, compute, tables
    from doc_intel.templates.schemas import Derivation

    rows = [["", ""], ["Fees", "10,00 $"], ["Total charged", "10,00 $"]]
    e = ParsedElement(element_type="table", rows=rows, caption="Closing", page_number=1)
    (c,) = run(ParsedDocument(elements=[e]), 100)
    hit = Hit(**c.model_dump(), chunk_id=uuid.uuid4(), score=0.0)
    (t,) = tables([hit])
    out = compute(t, Derivation(op="sum", column="col2"), "currency")
    assert isinstance(out, Computed) and out.value == "10,00 $"


def _roles(*rows):
    (c,) = run(ParsedDocument(elements=[table([["A", "B", "C"], *rows])]), 200)
    return [r.row_role for r in c.rows]


def test_total_role_needs_amount_only_siblings():
    assert _roles(
        ["01/02", "TOTAL", "45,00"],  # a dated merchant row
        ["Total charged", "10,00 $", ""],
        ["", "Total", "100,00"],
        ["", "Total", "Rent"],
    ) == ["data", "total", "total", "data"]


def test_split_joined_table_chunks_cite_the_page_of_their_first_row():
    rows = [["k", "v"]] + [[f"row{i}", "x y"] for i in range(1, 7)]
    joined = tbl(rows, page=3).model_copy(update={"row_pages": [3, 3, 3, 4, 4, 5]})
    chunks = run(ParsedDocument(elements=[joined]), 20)
    assert len(chunks) > 1
    pages = {int(r.cells["k"][3:]): c.page_number for c in chunks for r in c.rows[:1]}
    assert pages == {i: [3, 3, 3, 4, 4, 5][i - 1] for i in pages}
    assert {c.page_number for c in chunks} != {3}
