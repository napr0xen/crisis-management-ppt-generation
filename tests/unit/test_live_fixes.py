"""Live-test fixes (run e9f05471): a word in a number-typed table cell, a KPI caption that only
restates its tile's number, and a sideways table's count verified in the batch. Each on two
unrelated domains."""

import pytest

from doc_intel.llm.prompts import load_prompt
from doc_intel.mapping.engine import _rule_note, map_fields
from tests.fakes import FakeLLM
from tests.unit.test_batch_mapping import _count, _entry, _ev, _reply
from tests.unit.test_mapping_engine import CTX, DOCS, FakeRetriever, _hit
from tests.unit.test_row_groups import _cell_grid, _records_reply, _slot

# ---- a word in a number-typed table cell ------------------------------------------------------

TABLES = {
    "suppliers": (
        ["text", "currency", "percent", "number"],
        [
            ["Azure Peak", "AED 24.8M", "97%", "Preferred"],
            ["Falcon Freight", "AED 17.9M", "86%", "Watch"],
        ],
        [24.8, 97],
    ),
    "lab": (
        ["text", "number", "percent", "number"],
        [["Sample A", "12.5 mg", "98%", "Pass"], ["Sample B", "9.1 mg", "91%", "Retest"]],
        [12.5, 98],
    ),
}


def _table_setup(name):
    types, rows, _ = TABLES[name]
    lines = [" | ".join(r) for r in rows]
    hit = _hit("\n".join(f"| {line} |" for line in lines))
    return _cell_grid(types, len(rows)), hit, lines, rows


@pytest.mark.parametrize("name", TABLES)
async def test_a_word_in_a_number_typed_cell_is_verbatim_text_and_the_row_stays_populated(name):
    fields, hit, lines, rows = _table_setup(name)
    reply = _records_reply([(n, row, [(hit, lines[n - 1])]) for n, row in enumerate(rows, 1)])
    results = await map_fields(CTX, fields, DOCS, FakeRetriever([hit]), FakeLLM(reply))
    assert {r.status for r in results.values()} == {"populated"}
    assert [results[f"c1_{j}"].value for j in (1, 2)] == TABLES[name][2]
    assert results["c1_3"].value == rows[0][3] and results["c2_3"].value == rows[1][3]


@pytest.mark.parametrize("name", TABLES)
async def test_a_word_the_row_does_not_state_still_fails_and_keeps_the_row_partial(name):
    fields, hit, lines, rows = _table_setup(name)
    reply = _records_reply([(1, [*rows[0][:3], "Excellent"], [(hit, lines[0])])])
    results = await map_fields(CTX, fields, DOCS, FakeRetriever([hit]), FakeLLM(reply))
    assert results["c1_3"].missing_reason == "evidence_failed_verification"
    assert {results[f"c1_{j}"].status for j in range(3)} == {"partial"}


async def test_a_word_in_a_number_typed_card_slot_is_still_rejected():
    # only a table cell's type is the IR's guess; a card's value slot stays a number
    hit = _hit("Total addressable spend: AED 182.4M")
    fields = [
        _slot("v1", "[Value 1]", etype="number", card="1.1"),
        _slot("n1", "[Name]", card="1.1"),
        _slot("v2", "[Value 2]", etype="number", card="1.2"),
        _slot("n2", "[Name]", card="1.2"),
    ]
    reply = _records_reply([(1, ["Total addressable spend", ""], [(hit, hit.text)])])
    results = await map_fields(CTX, fields, DOCS, FakeRetriever([hit]), FakeLLM(reply))
    assert results["v1"].status == "missing"


# ---- a KPI caption that only restates its tile's number ---------------------------------------

KPIS = {
    "procurement": [
        ("| Total addressable spend | AED 182.4M |", "182.4", "Total addressable spend"),
        ("| Average PR-to-PO cycle time | 4.6 days |", "4.6", "Average PR-to-PO cycle time"),
    ],
    "hospital": [
        ("| Bed occupancy | 91% |", "91", "Bed occupancy"),
        ("| Mean wait | USD 12.5K |", "12.5", "Mean wait"),
    ],
}


def _kpi_tiles():
    return [
        f
        for m in (1, 2)
        for f in (
            _slot(f"v{m}", f"[Value {m}]", etype="number", card=f"1.{m}"),
            _slot(f"n{m}", "[Total spend]" if m == 1 else "[Cycle time]", card=f"1.{m}"),
        )
    ]


@pytest.mark.parametrize("name", KPIS)
async def test_a_caption_that_only_restates_its_tiles_number_is_missing(name):
    rows = KPIS[name]
    hit = _hit("\n".join(r[0] for r in rows))
    # the slip: the caption copies the value cell ("AED 182.4M", "4.6 days", "91%")
    dupes = [r[0].strip("| ").split(" | ")[1] for r in rows]
    reply = _records_reply(
        [(m, [rows[m - 1][1], dupes[m - 1]], [(hit, rows[m - 1][0])]) for m in (1, 2)]
    )
    results = await map_fields(CTX, _kpi_tiles(), DOCS, FakeRetriever([hit]), FakeLLM(reply))
    for m in (1, 2):
        assert results[f"v{m}"].status == "populated"
        assert (results[f"n{m}"].status, results[f"n{m}"].value) == ("missing", None)


@pytest.mark.parametrize("name", KPIS)
async def test_a_caption_holding_the_measures_name_is_populated(name):
    rows = KPIS[name]
    hit = _hit("\n".join(r[0] for r in rows))
    reply = _records_reply(
        [(m, [rows[m - 1][1], rows[m - 1][2]], [(hit, rows[m - 1][0])]) for m in (1, 2)]
    )
    results = await map_fields(CTX, _kpi_tiles(), DOCS, FakeRetriever([hit]), FakeLLM(reply))
    assert {r.status for r in results.values()} == {"populated"}
    assert results["n1"].value == rows[0][2]


async def test_a_caption_naming_its_number_among_words_is_kept():
    hit = _hit("| Top 5 suppliers | 5 |\n| Open tenders | 12 |")
    reply = _records_reply([(1, ["5", "Top 5 suppliers"], [(hit, "| Top 5 suppliers | 5 |")])])
    results = await map_fields(CTX, _kpi_tiles(), DOCS, FakeRetriever([hit]), FakeLLM(reply))
    assert (results["n1"].status, results["n1"].value) == ("populated", "Top 5 suppliers")


def test_the_records_prompt_says_a_caption_names_the_measure_never_its_number():
    prompt = load_prompt("slot_records")
    assert "measure" in prompt and "never its number" in prompt


# ---- a sideways table's count verifies in the batch, no per-slot escalation -----------------

PIPES = {
    "procurement": (
        "| Stage | Requiremen t | RFx | Award |\n| --- | --- | --- | --- |\n"
        "| No. of events | 7 | 6 | 2 |",
        ["Requirement", "RFx", "Award"],
        ["7", "6", "2"],
        "| No. of events | 7 | 6 | 2 |",
    ),
    "ferry": (
        "| Route | Morning sailings | Evening sailings |\n| --- | --- | --- |\n"
        "| Crossings | 14 | 9 |",
        ["Morning sailings", "Evening sailings"],
        ["14", "9"],
        "| Crossings | 14 | 9 |",
    ),
}


@pytest.mark.parametrize("name", PIPES)
def test_a_count_quoted_as_a_row_reads_its_column_names_from_that_tables_header(name):
    text, labels, values, row = PIPES[name]
    hit = _hit(text)
    field = _count(f"Pipeline / {labels[0]}")
    assert _rule_note(field, values[0], [_ev(hit, row)], [hit]) is None
    # the header lives in another chunk: the rule still breaks
    other = _hit(row)
    assert _rule_note(field, values[0], [_ev(other, row)], [other, _hit(text)])


@pytest.mark.parametrize("name", PIPES)
async def test_counts_quoted_as_a_row_are_mapped_in_one_batch_call(name):
    text, labels, values, row = PIPES[name]
    hit = _hit(text)
    fields = [
        _count(f"Pipeline / {label}").model_copy(
            update={"field_id": f"f{i}", "anchor_id": f"pptx:s2:sp{i}"}
        )
        for i, label in enumerate(labels, 1)
    ]
    llm = FakeLLM(_reply(*(_entry(i, v, hit, row) for i, v in enumerate(values, 1))))
    out = await map_fields(CTX, fields, DOCS, FakeRetriever([hit]), llm)
    assert len(llm.calls) == 1
    assert [out[f.field_id].value for f in fields] == [int(v) for v in values]
    assert {r.status for r in out.values()} == {"populated"}


async def test_a_word_in_a_date_typed_cell_stays_missing():
    # a date is stored ISO and written as an xlsx serial: "Ongoing" there is never a value
    lines = ["| Fit-out | Ongoing |", "| Handover | 4 March 2026 |"]
    hit = _hit("\n".join(lines))
    fields = _cell_grid(["text", "date"], 2)
    reply = _records_reply(
        [
            (1, ["Fit-out", "Ongoing"], [(hit, lines[0])]),
            (2, ["Handover", "4 March 2026"], [(hit, lines[1])]),
        ]
    )
    results = await map_fields(CTX, fields, DOCS, FakeRetriever([hit]), FakeLLM(reply))
    assert results["c1_1"].missing_reason == "evidence_failed_verification"
    assert (results["c2_1"].status, results["c2_1"].value) == ("populated", "2026-03-04")
