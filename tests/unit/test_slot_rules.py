"""Phase 18 live test 3: quote recovery, the heading check, count slots and cell-sized text.

Every rule is tested on two or more unrelated domains.
"""

import json
import uuid

import pytest

from doc_intel.llm.prompts import load_prompt
from doc_intel.mapping.engine import map_field, map_fields
from doc_intel.templates.schemas import Constraints, IRField
from tests.fakes import FakeLLM, slot_values_reply
from tests.unit.test_mapping_engine import CTX, DOCS, FakeRetriever, _hit
from tests.unit.test_row_groups import _rows_reply, _table_field


def _doc(*texts):
    """Consecutive chunks of one document (chunk_index 0..n-1)."""
    doc = uuid.uuid4()
    return [
        _hit(t).model_copy(update={"document_id": doc, "chunk_index": i})
        for i, t in enumerate(texts)
    ]


def _field(label, etype="text", context=None, token="[X]", fid="f"):  # noqa: S107
    return IRField(
        field_id=fid,
        anchor_id="pptx:s1:sp4",
        section_path=["Slide 1"],
        label=label,
        description=f"The {label}.",
        expected_type=etype,
        required=True,
        write_mode="replace_token",
        constraints=Constraints(token=token),
        context=context,
    )


def _ext(*cands, not_found=False):
    return json.dumps(
        {
            "candidates": [
                {"value": v, "quotes": [{"chunk_id": str(h.chunk_id), "quote": q} for h, q in qs]}
                for v, qs in cands
            ],
            "not_found": not_found,
            "incomplete": False,
        }
    )


YES = '{"checks": [{"about": "x", "match": true}]}'

# ---- quote recovery ---------------------------------------------------------------------------

NEIGHBOURS = {
    "risks": (
        "4. Hazards\n\nThe hazards we track are listed below.",
        "| Hazard | Level |\n| --- | --- |\n| Flooding | High |\n| Drought | Low |",
        "| Flooding | High |",
        "High",
    ),
    "library": (
        "Branch report\n\nOpening hours by branch follow.",
        "| Branch | Hours |\n| --- | --- |\n| Eastside | 9am to 5pm |\n| Riverside | 10am to 4pm |",
        "| Riverside | 10am to 4pm |",
        "10am to 4pm",
    ),
}


@pytest.mark.parametrize("name", NEIGHBOURS)
async def test_a_quote_cited_to_the_adjacent_chunk_is_credited_to_the_chunk_holding_it(name):
    intro, table, quote, value = NEIGHBOURS[name]
    before, holder = _doc(intro, table)
    llm = FakeLLM(_ext((value, [(before, quote)])))
    r = await map_field(CTX, _field("Level"), DOCS, FakeRetriever([before, holder]), llm)
    assert r.status == "populated" and r.value == value
    assert [(e.chunk_id, e.quote) for e in r.evidence] == [(holder.chunk_id, quote)]


@pytest.mark.parametrize("name", NEIGHBOURS)
async def test_recovery_never_reads_a_non_adjacent_or_unretrieved_chunk(name):
    intro, table, quote, value = NEIGHBOURS[name]
    before, gap, holder = _doc(intro, "Unrelated text about parking.", table)
    llm = FakeLLM(_ext((value, [(before, quote)])))
    r = await map_field(CTX, _field("Level"), DOCS, FakeRetriever([before, gap, holder]), llm)
    assert (r.status, r.missing_reason) == ("missing", "evidence_failed_verification")
    ghost = _hit(table)  # holds the quote but was never retrieved
    llm = FakeLLM(_ext((value, [(ghost, quote)])))
    r = await map_field(CTX, _field("Level"), DOCS, FakeRetriever([before, holder]), llm)
    assert r.status == "missing"


ROWS = {
    "next steps": (
        ["Action", "Owner", "Due"],
        [
            ["Repaint the hall", "Facilities lead", "4 March 2026"],
            ["Renew the lease", "Office manager", "9 April 2026"],
            ["Audit the stock", "Store keeper", "2 May 2026"],
        ],
    ),
    "fixtures": (
        ["Team", "Opponent", "Venue"],
        [
            ["Under 12s", "Hillcrest Rovers", "North Park"],
            ["Under 14s", "Bay City Juniors", "Harbour Field"],
        ],
    ),
}


def _md(columns, rows):
    head = f"| {' | '.join(columns)} |\n| {' | '.join(['---'] * len(columns))} |"
    return head, [f"| {' | '.join(r)} |" for r in rows]


@pytest.mark.parametrize("name", ROWS)
async def test_table_rows_quoted_with_the_header_glued_on_still_verify_line_by_line(name):
    columns, rows = ROWS[name]
    head, lines = _md(columns, rows)
    (hit,) = _doc("\n".join([head, *lines]))
    # rows 2..N repeat the header above their own line: that text is not contiguous in the source
    reply = _rows_reply(
        [(r, [(hit, f"{head}\n{line}")]) for r, line in zip(rows, lines, strict=True)]
    )
    r = await map_field(
        CTX, _table_field(columns=columns), DOCS, FakeRetriever([hit]), FakeLLM(reply)
    )
    assert r.status == "populated"
    assert r.value == rows
    assert all(any(line in e.quote for e in r.evidence) for line in lines)


async def test_a_partly_invented_multi_line_quote_keeps_only_its_real_lines():
    (hit,) = _doc("| Ward | Beds |\n| --- | --- |\n| Ward Alder | 24 |")
    quote = "| Ward | Beds |\n| Ward Alder | 24 |\n| Ward Zed | 99 |"
    llm = FakeLLM(_ext(("24", [(hit, quote)])))
    r = await map_field(CTX, _field("Beds", "number"), DOCS, FakeRetriever([hit]), llm)
    assert r.status == "populated" and r.value == 24
    assert "Ward Zed" not in " ".join(e.quote for e in r.evidence)
    llm = FakeLLM(_ext(("99", [(hit, quote)])))
    r = await map_field(CTX, _field("Beds", "number"), DOCS, FakeRetriever([hit]), llm)
    assert r.status == "missing"


# ---- the heading check needs sibling headings -------------------------------------------------

LONE = {
    "contact": (
        "Questions & Discussion",
        "Email address",
        "Contact: ana@example.org",
        "ana@example.org",
    ),
    "venue": ("See you there", "Venue", "Venue: Old Corn Exchange", "Old Corn Exchange"),
}


@pytest.mark.parametrize("name", LONE)
async def test_a_slot_without_sibling_headings_is_not_heading_checked(name):
    context, label, quote, value = LONE[name]
    h = _hit(quote)
    llm = FakeLLM(_ext((value, [(h, quote)])))
    r = await map_field(CTX, _field(label, context=context), DOCS, FakeRetriever([h]), llm)
    assert r.status == "populated" and r.value == value and len(llm.calls) == 1


@pytest.mark.parametrize("name", LONE)
async def test_map_fields_checks_only_slots_that_have_sibling_headings(name):
    context, label, quote, value = LONE[name]
    h = _hit(quote)
    llm = FakeLLM(slot_values_reply((value, h, quote)))
    await map_fields(CTX, [_field(label, context=context)], DOCS, FakeRetriever([h]), llm)
    assert [m[0]["content"] for m, _ in llm.calls] == [load_prompt("slot_values")]
    pair = [
        _field(label, context=context, fid="a"),
        _field(label, context="Another heading", fid="b").model_copy(
            update={"anchor_id": "pptx:s1:sp9"}
        ),
    ]
    llm = FakeLLM(slot_values_reply((value, h, quote), (value, h, quote)), YES, YES)
    await map_fields(CTX, pair, DOCS, FakeRetriever([h]), llm)
    prompts = [m[0]["content"] for m, _ in llm.calls]
    assert prompts.count(load_prompt("candidate_check")) == 2


# ---- count slots ------------------------------------------------------------------------------

COUNTS = {
    "pipeline": (
        "Pipeline / Requirement",
        "There are 25 open events worth AED 64.8M in total.",
        "| Stage | Requiremen t | Review | Done |\n| --- | --- | --- | --- |\n"
        "| Events | 7 | 6 | 1 |",
        "7",
        "25",
        "64.8M",
    ),
    "ferry": (
        "Crossings / Trips to the island",
        "The line ran 140 sailings with 12.5K passengers.",
        "Island trips: 18 this month, up from 15.",
        "18",
        "140",
        "12.5K",
    ),
}


@pytest.mark.parametrize("name", COUNTS)
async def test_a_count_slot_rejects_a_headline_total_and_retries_once(name):
    context, headline, own, value, total, _ = COUNTS[name]
    a, b = _hit(headline), _hit(own)
    field = _field(context, "number", context=context, token="[# 1]")
    llm = FakeLLM(_ext((total, [(a, headline)])), _ext((value, [(b, own)])), YES)
    r = await map_field(CTX, field, DOCS, FakeRetriever([a, b]), llm, ["Another stage"])
    assert r.status == "populated" and r.value == int(value)
    retry = llm.calls[1][0]
    assert retry[0]["content"] == load_prompt("field_extract") and len(retry) == 4
    assert context.rsplit(" / ")[-1] in retry[-1]["content"]


@pytest.mark.parametrize("name", COUNTS)
async def test_a_count_slot_rejects_decimals_units_and_magnitudes(name):
    context, headline, own, _, _, scaled = COUNTS[name]
    text = f"{own} {headline}"
    h = _hit(text)
    field = _field(context, "number", context=context, token="[# 4]")
    llm = FakeLLM(_ext((scaled, [(h, text)])), _ext((scaled, [(h, text)])))
    r = await map_field(CTX, field, DOCS, FakeRetriever([h]), llm)
    assert (r.status, r.missing_reason) == ("missing", "evidence_failed_verification")
    assert len(llm.calls) == 2  # one retry, never a loop


@pytest.mark.parametrize("token", ["[Value 1]", "{{count}}"])
async def test_other_number_slots_keep_decimals_and_magnitudes(token):
    quote = "The line ran 140 sailings with 12.5K passengers."
    h = _hit(quote)
    llm = FakeLLM(_ext(("12.5K", [(h, quote)])))
    field = _field("Passengers", "number", token=token)
    r = await map_field(CTX, field, DOCS, FakeRetriever([h]), llm)
    assert r.status == "populated" and r.value == 12.5 and len(llm.calls) == 1


# ---- cell-sized text --------------------------------------------------------------------------

ROW_VALUES = {
    "risks": (
        "Drought risk",
        "| Drought | Medium | Wells run dry by August. | Ration irrigation. |",
        "Wells run dry by August.",
    ),
    "menu": ("Starter", "| Monday | Leek soup | Fish pie | Apple crumble |", "Leek soup"),
}


@pytest.mark.parametrize("name", ROW_VALUES)
async def test_a_text_value_spanning_table_cells_is_retried_for_its_own_cell(name):
    label, row, cell = ROW_VALUES[name]
    h = _hit(row)
    bad = row.strip("| ")
    llm = FakeLLM(_ext((bad, [(h, row)])), _ext((cell, [(h, row)])))
    r = await map_field(CTX, _field(label), DOCS, FakeRetriever([h]), llm)
    assert r.status == "populated" and r.value == cell and len(llm.calls) == 2
    llm = FakeLLM(_ext((bad, [(h, row)])), _ext((bad, [(h, row)])))
    r = await map_field(CTX, _field(label), DOCS, FakeRetriever([h]), llm)
    assert (r.status, r.missing_reason) == ("missing", "evidence_failed_verification")
