"""Phase 23 items 2 and 3: a typed value is checked against its cited row's label
(`row_label_mismatch`), and a template row must come from one source row (`row_mixed`)."""

import json
import uuid

import pytest
from structlog.testing import capture_logs

from doc_intel.core.config import get_settings
from doc_intel.embeddings.schemas import Hit
from doc_intel.ingestion.schemas import TableRow
from doc_intel.mapping.engine import map_field, map_fields
from tests.fakes import FakeLLM
from tests.unit.test_mapping_engine import CTX, DOCS, FakeRetriever, _cand, _field
from tests.unit.test_row_groups import (
    _records_reply,
    _rows_reply,
    _slot_grid,
    _table_field,
)
from tests.unit.test_verifier_retry import _codes, _reply


@pytest.fixture(autouse=True)
def escalation(monkeypatch):
    monkeypatch.setenv("THINK_ON_ESCALATION", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


def _table(header, rows, structured=True):
    """A table chunk: Markdown text and, when `structured`, `[P1-T1-Rnn]` rows."""
    lines = [f"| {' | '.join(header)} |", "|" + " --- |" * len(header)]
    lines += [f"| {' | '.join(r)} |" for r in rows]
    return Hit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="ledger.pdf",
        page_number=1,
        section_heading=None,
        element_type="table",
        text="\n".join(lines),
        score=0.5,
        rows=[
            TableRow(id=f"P1-T1-R{i:02d}", cells=dict(zip(header, r, strict=True)))
            for i, r in enumerate(rows, 1)
        ]
        if structured
        else None,
    )


def _slot(label, etype="currency"):
    return _field(etype).model_copy(update={"label": label})


def _row(cells):
    return "| " + " | ".join(cells) + " |"


LEDGER = [["Balance brought forward", "4,200"], ["Closing balance", "5,900"]]
BF_Q, CLOSE_Q = _row(LEDGER[0]), _row(LEDGER[1])
HEADER = ["Opening balance", "Net movement", "Closing balance"]
SUMMARY = ["4,200", "1,700", "5,900"]


async def _run(slot, hit, *replies):
    llm = FakeLLM(*replies)
    with capture_logs() as logs:
        r = await map_field(CTX, slot, DOCS, FakeRetriever([hit]), llm)
    return r, llm, logs


async def test_a_value_from_another_labelled_row_is_rejected_and_the_retry_finds_the_true_row():
    hit = _table(["Item", "Amount"], LEDGER)
    wrong, right = _reply(_cand("4,200", (hit, BF_Q))), _reply(_cand("5,900", (hit, CLOSE_Q)))
    r, llm, logs = await _run(_slot("Closing balance"), hit, wrong, right)
    assert (r.status, r.value) == ("populated", 5900)
    assert [e.quote for e in r.evidence] == [CLOSE_Q]
    assert _codes(logs) == [["row_label_mismatch"]]
    note = llm.calls[1][0][-1]["content"]
    assert "row_label_mismatch" in note and "P1-T1-R01" in note and "P1-T1-R02" in note
    assert "Balance brought forward" in note and "Closing balance" in note


async def test_a_value_from_another_column_of_a_header_table_is_rejected():
    hit = _table(HEADER, [SUMMARY])
    q = _row(SUMMARY)
    r, llm, logs = await _run(
        _slot("NET MOVEMENT"),
        hit,
        _reply(_cand("5,900", (hit, q))),
        _reply(_cand("1,700", (hit, q))),
    )
    assert (r.status, r.value) == ("populated", 1700)
    assert _codes(logs) == [["row_label_mismatch"]]
    assert len(llm.calls) == 2


async def test_a_value_in_a_matching_row_populates_without_a_retry():
    hit = _table(["Item", "Amount"], LEDGER)
    r, llm, logs = await _run(_slot("Closing balance"), hit, _reply(_cand("5,900", (hit, CLOSE_Q))))
    assert (r.status, r.value) == ("populated", 5900) and len(llm.calls) == 1
    assert _codes(logs) == []


async def test_a_value_in_two_cells_one_of_them_matching_is_accepted():
    rows = [["Balance brought forward", "5,900"], ["Closing balance", "5,900"]]
    hit = _table(["Item", "Amount"], rows)
    r, llm, _ = await _run(
        _slot("Closing balance"), hit, _reply(_cand("5,900", (hit, _row(rows[0]))))
    )
    assert (r.status, r.value) == ("populated", 5900) and len(llm.calls) == 1


async def test_no_row_labelled_like_the_slot_keeps_today_s_behaviour():
    rows = [["Saldo anterior", "4,200"], ["Saldo final", "5,900"]]
    hit = _table(["Concepto", "Importe"], rows)
    r, llm, _ = await _run(
        _slot("Closing balance"), hit, _reply(_cand("4,200", (hit, _row(rows[0]))))
    )
    assert (r.status, r.value) == ("populated", 4200) and len(llm.calls) == 1


async def test_a_hit_without_rows_is_unchanged_and_logs_carry_codes_only():
    plain = _table(["Item", "Amount"], LEDGER, structured=False)
    r, llm, _ = await _run(_slot("Closing balance"), plain, _reply(_cand("4,200", (plain, BF_Q))))
    assert (r.status, r.value) == ("populated", 4200) and len(llm.calls) == 1
    hit = _table(["Item", "Amount"], LEDGER)
    _, _, logs = await _run(
        _slot("Closing balance"),
        hit,
        _reply(_cand("4,200", (hit, BF_Q))),
        _reply(_cand("5,900", (hit, CLOSE_Q))),
    )
    text = json.dumps(logs, default=str)
    assert "row_label_mismatch" in text
    assert not any(s in text for s in ("4,200", "5,900", "Balance brought", "Closing balance"))


# ---- item 3 ------------------------------------------------------------------------------------

VENDORS = [["Acme Ltd", "400", "5"], ["Borel Inc", "900", "7"]]
VENDOR_HEADER = ["Vendor", "Price", "Days"]
BOTH_Q = _row(VENDORS[0]) + "\n" + _row(VENDORS[1])


def _table_run(hit, *rows):
    reply = _rows_reply([(cells, [(hit, q)]) for cells, q in rows])
    field = _table_field(columns=VENDOR_HEADER)
    return map_fields(CTX, [field], DOCS, FakeRetriever([hit]), FakeLLM(reply))


async def test_a_table_row_mixing_two_source_rows_is_dropped_and_counted():
    hit = _table(VENDOR_HEADER, VENDORS)
    mixed = ["Acme Ltd", "900", "5"]
    with capture_logs() as logs:
        r = (await _table_run(hit, (VENDORS[1], _row(VENDORS[1])), (mixed, BOTH_Q)))["t"]
    assert (r.status, r.value) == ("partial", [VENDORS[1]])
    assert [e["codes"] for e in logs if e["event"] == "verification_rejected"] == [["row_mixed"]]


async def test_an_aligned_row_is_kept():
    hit = _table(VENDOR_HEADER, VENDORS)
    r = (await _table_run(hit, (VENDORS[0], BOTH_Q), (VENDORS[1], _row(VENDORS[1]))))["t"]
    assert (r.status, r.value) == ("populated", VENDORS)


async def test_a_mixed_table_row_without_structured_rows_is_unchanged():
    hit = _table(VENDOR_HEADER, VENDORS, structured=False)
    mixed = ["Acme Ltd", "900", "5"]
    r = (await _table_run(hit, (mixed, BOTH_Q)))["t"]
    assert r.value == [mixed]


async def test_a_slot_grid_record_mixing_two_rows_keeps_the_majority_row_only():
    hit = _table(VENDOR_HEADER, VENDORS)
    fields = _slot_grid([("Vendor", "text"), ("Price", "number"), ("Days", "number")], 2)
    reply = _records_reply(
        [(1, ["Acme Ltd", "400", "7"], [(hit, BOTH_Q)]), (2, VENDORS[1], [(hit, _row(VENDORS[1]))])]
    )
    llm = FakeLLM(reply, reply)  # the dropped cell earns one retry, which repeats the reply
    out = await map_fields(CTX, fields, DOCS, FakeRetriever([hit]), llm)
    assert (out["g1_0"].value, out["g1_1"].value) == ("Acme Ltd", 400)
    assert out["g1_2"].status == "missing" and out["g1_0"].status == "partial"
    assert [out[f"g2_{c}"].status for c in range(3)] == ["populated"] * 3


async def test_a_keyed_card_keeps_slots_taken_from_different_source_rows():
    from tests.unit.test_row_groups import _slot as _tok

    hit = _table(["Item", "Amount"], LEDGER)

    def card(n, head):
        return [
            _tok(f"o{n}", "[Opening balance]", etype="currency", card=f"1.{n}", context=head),
            _tok(f"c{n}", "[Closing balance]", etype="currency", card=f"1.{n}", context=head),
        ]

    reply = _records_reply([(1, ["4,200", "5,900"], [(hit, BF_Q), (hit, CLOSE_Q)])])
    ok = json.dumps({"checks": [{"about": "r", "belongs": "this"}]})
    llm = FakeLLM(
        reply, ok, reply, ok, reply, ok, reply, ok
    )  # spare replies: a dropped cell retries
    out = await map_fields(
        CTX, [*card(1, "Alpha"), *card(2, "Beta")], DOCS, FakeRetriever([hit]), llm
    )
    assert (out["o1"].value, out["c1"].value) == (4200, 5900)
    assert out["o1"].status != "missing" and out["c1"].status != "missing"


async def test_a_text_only_row_labelled_like_the_slot_is_no_rival_to_the_value_row():
    rows = [["Balance at 31 Dec", "5,900"], ["Closing balance", "see reconciliation"]]
    hit = _table(["Item", "Amount"], rows)
    r, llm, logs = await _run(
        _slot("Closing balance"), hit, _reply(_cand("5,900", (hit, _row(rows[0]))))
    )
    assert (r.status, r.value) == ("populated", 5900) and len(llm.calls) == 1
    assert _codes(logs) == []
