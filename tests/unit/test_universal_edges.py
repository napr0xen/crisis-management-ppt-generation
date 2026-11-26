"""Phase 19 fixes 1-4, each on unrelated domains (cash, clinical trial, warehouse, school)."""

import json
import re
from io import BytesIO

import pytest
from lxml import etree
from pptx import Presentation
from pptx.util import Inches

from doc_intel.llm.gateway import LLMError
from doc_intel.llm.prompts import load_prompt
from doc_intel.mapping.engine import (
    _group_query,
    _rule_note,
    field_query,
    map_field,
    map_fields,
    map_group,
)
from doc_intel.mapping.extractor import _records_spec, _rows_spec
from doc_intel.mapping.schemas import Evidence
from doc_intel.templates.anchors_pptx import extract as extract_pptx
from doc_intel.templates.ir_builder import build_ir, validate
from doc_intel.templates.schemas import (
    Anchor,
    AnchorMap,
    Constraints,
    IRDraft,
    TableRef,
)
from doc_intel.writers.runs import A, fill_rows
from tests.fakes import FakeLLM
from tests.unit.test_ir_builder import TID, _reply
from tests.unit.test_mapping_engine import CTX, DOCS, FakeRetriever, _hit
from tests.unit.test_row_groups import _records_reply, _rows_reply, _slot, _table_field

# ---- fix 1: tokens in native table cells ------------------------------------------------------

GRIDS = {
    "cash": (
        ["Item", "Amount"],
        [("Opening balance", "[Opening Balance]"), ("Closing", "[Close]")],
    ),
    "trial": (["Arm", "Result"], [("Placebo", "{{placebo_n}}"), ("Treatment", "{{ treat n }}")]),
}


def _grid_map(name, extra_body=()):
    header, body = GRIDS[name]
    anchors = []
    rows = [[(h, []) for h in header]]
    for label, token in body:
        rows.append([(label, []), (token, [token])])
    rows += [list(r) for r in extra_body]
    for r, row in enumerate(rows):
        for c, (text, tokens) in enumerate(row):
            anchors.append(
                Anchor(
                    anchor_id=f"pptx:s1:sp4:r{r}c{c}",
                    kind="table_cell",
                    path=["Slide 1"],
                    text=text,
                    tokens=tokens,
                    is_placeholder=bool(tokens),
                    table=TableRef(row=r, col=c, header=header[c]),
                    context=f"{row[0][0]} / {header[c]}" if r else None,
                )
            )
    return AnchorMap(format="pptx", anchors=anchors)


def _rows_field():
    return _table_field("t").model_copy(
        update={"anchor_id": "pptx:s1:sp4:r1c0", "constraints": Constraints(columns=["a", "b"])}
    )


@pytest.mark.parametrize("name", GRIDS)
async def test_label_value_grid_drops_fill_table_rows_and_covers_every_token(name):
    amap = _grid_map(name)
    ir = await build_ir(FakeLLM(_reply(_rows_field())), amap, TID)
    assert [f.write_mode for f in ir.fields] == ["replace_token"] * 2
    tokens = {a.anchor_id: a.tokens[0] for a in amap.anchors if a.tokens}
    assert {f.anchor_id: f.constraints.token for f in ir.fields} == tokens
    assert all(f.expected_type == "text" and not f.required for f in ir.fields)
    assert all(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", f.field_id) for f in ir.fields)
    assert len({f.field_id for f in ir.fields}) == 2
    assert all(f.context and f.label == f.context for f in ir.fields)
    assert validate(IRDraft(fields=ir.fields), amap) == []


async def test_a_skipped_cell_is_covered_and_a_covered_one_is_not_duplicated():
    amap = _grid_map("cash")
    one = _slot("known", "[Opening Balance]", anchor="pptx:s1:sp4:r1c1", unit="pptx:s1")
    ir = await build_ir(FakeLLM(_reply(one)), amap, TID)
    assert ir.fields[0].field_id == "known" and len(ir.fields) == 2
    assert ir.fields[1].constraints.token == "[Close]"


def _plain_map(cells):
    """cells: (row, col, text) of one pptx table whose header is Name / Qty."""
    header = ["Name", "Qty"]
    return AnchorMap(
        format="pptx",
        anchors=[
            Anchor(
                anchor_id=f"pptx:s1:sp4:r{r}c{c}",
                kind="table_cell",
                path=["Slide 1"],
                text=t,
                tokens=[t] if re.fullmatch(r"\[\w+\]|_{4,}", t) else [],
                is_placeholder=not t or t.startswith("["),
                table=TableRef(row=r, col=c, header=header[c]),
            )
            for r, c, t in cells
        ],
    )


async def test_a_records_table_keeps_fill_table_rows_and_gets_no_token_fields():
    # body rows have no static label in column 0, so it is a records table, not a grid
    amap = _plain_map([(0, 0, "Name"), (0, 1, "Qty"), (1, 0, "[Name]"), (1, 1, "[Qty]")])
    field = _rows_field()
    ir = await build_ir(FakeLLM(_reply(field)), amap, TID)
    assert [f.write_mode for f in ir.fields] == ["fill_table_rows"]


async def test_code_made_field_ids_are_unique_valid_and_safe_for_empty_slugs():
    amap = _plain_map(
        [
            *[(0, 0, "Name"), (0, 1, "Qty")],
            *[(1, 0, "Alpha"), (1, 1, "____")],
            *[(2, 0, "Beta"), (2, 1, "[Alpha]")],
            *[(3, 0, "Gamma"), (3, 1, "[alpha]")],
        ]
    )
    ir = await build_ir(FakeLLM(_reply(_rows_field())), amap, TID)
    ids = [f.field_id for f in ir.fields]
    assert len(ids) == len(set(ids)) == 3
    assert all(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", i) for i in ids)
    assert validate(IRDraft(fields=ir.fields), amap) == []


def _cell_el(text):
    cell = etree.Element(A.q("tc"))
    run = etree.SubElement(etree.SubElement(cell, A.q("p")), A.q("r"))
    etree.SubElement(run, A.q("t")).text = text
    return cell


def _cell_text(cell):
    return "".join(t.text for t in cell.iter(A.q("t")))


ROWS_ROWS = {
    "cash": [["Opening", "[Opening Balance]"], ["Net", "Net: [Net Movement]"], ["End", "[Close]"]],
    "warehouse": [["Dock", "[Pallets]"], ["Rack", "{{racks}}"], ["Yard", "<count>"]],
}


@pytest.mark.parametrize("name", ROWS_ROWS)
def test_fill_rows_marker_clears_tokens_of_every_other_body_cell(name):
    body = ROWS_ROWS[name]
    cells = [[_cell_el(t) for t in row] for row in body]
    fill_rows(lambda r: cells[r - 1], len(cells) + 1, ["Not stated"], A)
    assert _cell_text(cells[0][0]) == "Not stated"
    assert _cell_text(cells[0][1]) == ""
    assert "[" not in "".join(_cell_text(c) for row in cells for c in row)
    assert _cell_text(cells[1][0]) == body[1][0]  # static text kept
    if name == "cash":
        assert _cell_text(cells[1][1]) == "Net: "


@pytest.mark.parametrize("name", ROWS_ROWS)
def test_fill_rows_clears_tokens_in_rows_past_the_content_and_unwritten_cells(name):
    body = ROWS_ROWS[name]
    cells = [[_cell_el(t) for t in row] for row in body]
    assert fill_rows(lambda r: cells[r - 1], len(cells) + 1, [["a"]], A) is False
    assert _cell_text(cells[0][0]) == "a"
    assert _cell_text(cells[0][1]) == ""  # the row had one value for two cells
    assert _cell_text(cells[1][0]) == body[1][0]
    assert not any(re.search(r"[\[{<]", _cell_text(c)) for c in cells[1][1:] + cells[2][1:])


# ---- fix 2: side-by-side tables carry their own heading -----------------------------------------


def _two_tables(left, right, headers):
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for text, x in ((left, 0.5), (right, 5.5)):
        slide.shapes.add_textbox(
            Inches(x), Inches(0.5), Inches(4), Inches(0.5)
        ).text_frame.text = text
    for x in (0.5, 5.5):
        t = slide.shapes.add_table(2, 2, Inches(x), Inches(1.2), Inches(4), Inches(1)).table
        for c, h in enumerate(headers):
            t.cell(0, c).text = h
    out = BytesIO()
    prs.save(out)
    return extract_pptx(out.getvalue())


TWO = {
    "cash": ("LARGEST RECEIPTS", "LARGEST PAYMENTS", ["Date", "Amount"]),
    "ward": ("Admitted patients", "Discharged patients", ["Name", "Day"]),
}


@pytest.mark.parametrize("name", TWO)
def test_each_side_by_side_table_gets_only_its_own_heading(name):
    left, right, headers = TWO[name]
    anchors = [a for a in _two_tables(left, right, headers) if a.kind == "table_cell"]
    by_table = {}
    for a in anchors:
        by_table.setdefault(a.anchor_id.rsplit(":", 1)[0], []).append(a)
    first, second = by_table.values()
    assert all(a.context.startswith(left) and right not in a.context for a in first)
    assert all(a.context.startswith(right) and left not in a.context for a in second)
    assert {a.table.row: a.context for a in first if a.table.col == 0} == {
        0: f"{left}",
        1: f"{left} / {headers[0]}",
    }


@pytest.mark.parametrize("name", TWO)
def test_sibling_tables_have_distinct_contexts_queries_and_prompts(name):
    left, right, headers = TWO[name]
    anchors = [a for a in _two_tables(left, right, headers) if a.kind == "table_cell"]
    fields = []
    for a in (x for x in anchors if (x.table.row, x.table.col) == (1, 0)):
        fields.append(
            _table_field(f"t{len(fields)}", columns=tuple(headers)).model_copy(
                update={"anchor_id": a.anchor_id, "context": a.context}
            )
        )
    a, b = fields
    assert a.context != b.context and field_query(a) != field_query(b)
    assert _group_query([[a]]) != _group_query([[b]])
    assert _rows_spec(a)["context"] != _rows_spec(b)["context"]


# ---- fix 3: direction and sibling headings ------------------------------------------------------


@pytest.mark.parametrize("prompt", ["table_rows", "slot_records"])
def test_row_prompts_state_the_direction_and_sibling_heading_rules(prompt):
    text = load_prompt(prompt)
    assert "other_headings" in text and "opposite" in text and "direction" in text


def test_row_check_prompt_exists_and_names_the_verdicts():
    text = load_prompt("row_check")
    assert all(w in text for w in ('"this"', '"other"', '"unknown"'))


def test_specs_carry_other_headings_only_when_given():
    f = _table_field().model_copy(update={"context": "Dispatched / Date"})
    assert "other_headings" not in _rows_spec(f)
    assert _rows_spec(f, ["Received / Date"])["other_headings"] == ["Received / Date"]
    g = [[_slot("a", "[A 1]", context="Out / A")], [_slot("b", "[A 2]", context="Out / A")]]
    assert _records_spec(g, ["In / A"])["other_headings"] == ["In / A"]
    assert "other_headings" not in _records_spec(g)


def _check(*verdicts):
    return json.dumps({"checks": [{"about": "r", "belongs": v} for v in verdicts]})


DIRECTION = {
    "cash": (
        "LARGEST PAYMENTS / Date",
        "LARGEST RECEIPTS / Date",
        ["Mar 1 vendor paid 4,000", "Mar 2 customer credit received 9,000"],
        [["Mar 1", "4,000"], ["Mar 2", "9,000"]],
    ),
    "warehouse": (
        "Outbound shipments / Date",
        "Inbound shipments / Date",
        ["Mar 1 truck dispatched 40 pallets", "Mar 2 truck arrived 70 pallets"],
        [["Mar 1", "40"], ["Mar 2", "70"]],
    ),
}


def _dir_setup(name):
    ctx, other, quotes, rows = DIRECTION[name]
    hit = _hit("\n".join(quotes))
    reply = _rows_reply([(r, [(hit, q)]) for r, q in zip(rows, quotes, strict=True)])
    field = _table_field(columns=("Date", "Amount")).model_copy(update={"context": ctx})
    return field, other, hit, reply, rows


@pytest.mark.parametrize("name", DIRECTION)
async def test_row_check_drops_only_the_row_judged_other(name):
    field, _, hit, reply, rows = _dir_setup(name)
    llm = FakeLLM(reply, _check("this", "other"))
    out = await map_fields(CTX, [field], DOCS, FakeRetriever([hit]), llm)
    assert out["t"].value == [rows[0]] and out["t"].status == "populated"
    user = json.loads(llm.calls[1][0][1]["content"])
    assert len(user["rows"]) == 2 and user["context"] == field.context


@pytest.mark.parametrize("name", DIRECTION)
async def test_row_check_unknown_keeps_the_row_and_invalid_output_keeps_all(name):
    field, _, hit, reply, rows = _dir_setup(name)
    out = await map_fields(
        CTX, [field], DOCS, FakeRetriever([hit]), FakeLLM(reply, _check("unknown", "this"))
    )
    assert out["t"].value == rows
    out = await map_fields(
        CTX, [field], DOCS, FakeRetriever([hit]), FakeLLM(reply, "not json", "not json", "not json")
    )
    assert out["t"].value == rows
    out = await map_fields(
        CTX, [field], DOCS, FakeRetriever([hit]), FakeLLM(reply, _check("other"))
    )
    assert out["t"].value == rows  # a verdict count that does not match keeps every row


async def test_row_check_all_other_leaves_the_table_missing_and_transport_error_propagates():
    field, _, hit, reply, _ = _dir_setup("cash")
    out = await map_fields(
        CTX, [field], DOCS, FakeRetriever([hit]), FakeLLM(reply, _check("other", "other"))
    )
    assert out["t"].status == "missing"

    class Boom(FakeLLM):
        async def complete(self, messages, json_schema=None, think=False, think_budget=None):
            if self.calls:
                raise LLMError("down")
            return await super().complete(messages, json_schema)

    with pytest.raises(LLMError):
        await map_fields(CTX, [field], DOCS, FakeRetriever([hit]), Boom(reply))


async def test_no_row_check_without_a_heading_with_a_letter():
    field, _, hit, reply, rows = _dir_setup("cash")
    field = field.model_copy(update={"context": "123"})
    llm = FakeLLM(reply)
    out = await map_fields(CTX, [field], DOCS, FakeRetriever([hit]), llm)
    assert len(llm.calls) == 1 and out["t"].value == rows


async def test_sibling_headings_reach_the_row_call():
    field, other, hit, reply, _ = _dir_setup("warehouse")
    sib = _slot("s", "[Zed]", unit="pptx:s1", context=other).model_copy(
        update={"anchor_id": "pptx:s1:sp9"}
    )
    field = field.model_copy(update={"anchor_id": "pptx:s1:sp4:r1c0"})
    nothing = '{"slots": []}'  # the lone loose slot of the opening unit is a batch
    llm = FakeLLM(reply, _check("this", "this"), nothing)
    hits = [hit]
    await map_fields(
        CTX,
        [field, sib],
        DOCS,
        FakeRetriever(hits),
        llm,
    )
    assert f'"other_headings": ["{other}"]' in llm.calls[0][0][1]["content"]


RECORDS = {
    "school": ("Won matches", "Lost matches", ["Eagles beat Owls 3-1", "Owls lost to Hawks 0-2"]),
    "clinic": ("Admitted", "Discharged", ["Ann admitted to ward 4", "Bob discharged from ward 2"]),
}


@pytest.mark.parametrize("name", RECORDS)
async def test_non_keyed_records_drop_a_record_judged_other(name):
    ctx, other, quotes = RECORDS[name]
    hit = _hit("\n".join(quotes))
    group = [
        [_slot(f"r{i}_{k}", f"[{j} {i}]", context=ctx) for k, j in enumerate(("Who", "Note"))]
        for i in (1, 2)
    ]
    reply = _records_reply([(i, [q[:5], q[6:]], [(hit, q)]) for i, q in enumerate(quotes, start=1)])
    llm = FakeLLM(reply, _check("this", "other"))
    out = await map_group(CTX, group, DOCS, FakeRetriever([hit]), llm, [other])
    assert out["r1_0"].status == "populated" and out["r2_0"].status == "missing"
    assert f'"other_headings": ["{other}"]' in llm.calls[0][0][1]["content"]
    # without sibling headings a plain group is not checked
    llm = FakeLLM(reply)
    out = await map_group(CTX, group, DOCS, FakeRetriever([hit]), llm)
    assert len(llm.calls) == 1 and out["r2_0"].status == "populated"


# ---- fix 4: heading echo on a keyed card slot ---------------------------------------------------


def _card_slot(context, label="Risk"):
    slot = _slot("s", f"[{label}]", unit="pptx:s1", label=label, context=context)
    return slot.model_copy(update={"card": "1.1"})


ECHO = {
    "procurement": (
        "Price inflation / Risk",
        "Price inflation",
        "Suppliers raised unit prices by 12% since March, squeezing the budget.",
    ),
    "ward": (
        "Ward Birch / Status",
        "Ward Birch",
        "Closed to new admissions until the ventilation repair is signed off.",
    ),
}


def _echo_reply(value, hit, quote):
    return json.dumps(
        {
            "candidates": [
                {"value": value, "quotes": [{"chunk_id": str(hit.chunk_id), "quote": quote}]}
            ],
            "not_found": False,
            "incomplete": False,
        }
    )


@pytest.mark.parametrize("name", ECHO)
async def test_heading_echo_is_retried_once_and_the_real_description_populates(name):
    context, echo, real = ECHO[name]
    hit = _hit(f"{echo}\n{real}")
    field = _card_slot(context, context.rsplit(" / ", 1)[1])
    llm = FakeLLM(_echo_reply(echo, hit, echo + "\n" + real[:20]), _echo_reply(real, hit, real))
    out = await map_field(CTX, field, DOCS, FakeRetriever([hit]), llm)
    assert out.status == "populated" and out.value == real
    assert "heading" in llm.calls[1][0][-1]["content"]


@pytest.mark.parametrize("name", ECHO)
async def test_heading_echo_after_the_retry_is_missing_never_fabricated(name):
    context, echo, real = ECHO[name]
    hit = _hit(f"{echo}\n{real}")
    field = _card_slot(context, context.rsplit(" / ", 1)[1])
    reply = _echo_reply(echo, hit, echo + "\n" + real[:20])
    out = await map_field(CTX, field, DOCS, FakeRetriever([hit]), FakeLLM(reply, reply))
    assert out.status == "missing" and out.value is None


_HIT = _hit("a quote of some length")
_EVIDENCE = [
    Evidence(
        document_id=_HIT.document_id,
        chunk_id=_HIT.chunk_id,
        filename="a.pdf",
        page_number=None,
        section_heading=None,
        quote="a quote of some length",
    )
]


async def test_a_list_of_echoes_is_rejected_but_a_real_item_among_them_passes():
    f = _card_slot("Price inflation / Risk").model_copy(update={"expected_type": "bullet_list"})
    assert _rule_note(f, ["Price inflation", "Risk"], _EVIDENCE)
    assert _rule_note(f, ["Price inflation", "Costs rose 12%"], _EVIDENCE) is None
    assert _rule_note(_card_slot(None), "Risk", _EVIDENCE) is None
    assert _rule_note(_card_slot("Risk"), "", _EVIDENCE) is None


@pytest.mark.parametrize(
    ("context", "value"),
    [("North region sales / Region", "North"), ("Phase II trial / Arm B / Phase", "Phase II")],
)
def test_a_table_cell_repeating_its_heading_words_is_not_an_echo(context, value):
    # fix 2 puts the slide heading into every cell's context: only card slots are echo-checked
    cell = _card_slot(context).model_copy(update={"card": None})
    assert _rule_note(cell, value, _EVIDENCE) is None
