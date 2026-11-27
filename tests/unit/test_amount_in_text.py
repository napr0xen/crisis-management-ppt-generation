"""Live finance bug: a text slot under an identifier/description header never takes an amount."""

import json

import pytest
from structlog.testing import capture_logs

from doc_intel.core.config import get_settings
from doc_intel.mapping.engine import map_fields
from doc_intel.mapping.verifier import is_amount
from tests.fakes import FakeLLM
from tests.unit.test_mapping_engine import CTX, DOCS, FakeRetriever, _hit
from tests.unit.test_row_groups import _records_reply, _rows_reply, _slot, _table_field


@pytest.fixture(autouse=True)
def escalation(monkeypatch):
    monkeypatch.setenv("THINK_ON_ESCALATION", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


AMOUNT_NOTE = "takes an identifier from the same source row, not an amount"


@pytest.mark.parametrize(
    "text", ["1.183,51 DR", "$ 500.00", "402.031,45 $", "45.015,15 CR", "1,234.56", "USD 1,234.56"]
)
def test_is_amount_true(text):
    assert is_amount(text)


@pytest.mark.parametrize(
    "text",
    [
        "012180001234567890",
        "****1234",
        "INV-2022-0042",
        "6 junio 2022",
        "12/03/2026",
        "2022-06-14",
        "Transfer to ACME",
        "Pago de 1.183,51 DR a ACME",
        "REF 1234",
        "INV 2022",
        "USD 500",
        "",
    ],
)
def test_is_amount_false(text):
    assert not is_amount(text)


LINE = "14/06/2022 | 14/06/2022 | Pago INV-2022-0042 ACME | 1.183,51 DR | 9.000,00 CR"
HEADERS = [("Description", "text"), ("Reference", "text"), ("Amount", "currency")]


def _grid():
    fields = []
    for r in (1, 2):
        for stem, etype in HEADERS:
            f = _slot(f"g{r}_{stem.lower()}", f"[{stem} {r}]", etype=etype, label=stem)
            fields.append(f)
    return fields


def _reply(hit, ref):
    cells = ["Pago INV-2022-0042 ACME", ref, "1.183,51 DR"]
    return _records_reply([(1, cells, [(hit, LINE)]), (2, ["", "", ""], [])])


async def test_a_reference_slot_filled_with_the_amount_is_rejected_and_retried_with_the_note():
    hit = _hit(LINE)
    llm = FakeLLM(_reply(hit, "1.183,51 DR"), _reply(hit, "INV-2022-0042"))
    with capture_logs() as logs:
        r = await map_fields(CTX, _grid(), DOCS, FakeRetriever([hit]), llm)
    assert len(llm.calls) == 2 and AMOUNT_NOTE in llm.calls[1][0][-1]["content"]
    assert "'Reference'" in llm.calls[1][0][-1]["content"]
    assert r["g1_reference"].status == "populated" and r["g1_reference"].value == "INV-2022-0042"
    first = [e for e in logs if e["event"] == "verification_rejected"]
    assert first and first[0]["codes"] == ["amount_in_text_column"]
    assert set(first[0]) == {"event", "log_level", "slot_id", "codes"}
    assert "1.183" not in json.dumps(logs) and "INV-2022" not in json.dumps(logs)


async def test_a_retry_that_still_returns_the_amount_leaves_the_cell_unpopulated():
    hit = _hit(LINE)
    llm = FakeLLM(_reply(hit, "1.183,51 DR"), _reply(hit, "1.183,51 DR"))
    r = await map_fields(CTX, _grid(), DOCS, FakeRetriever([hit]), llm)
    assert len(llm.calls) == 2
    ref = r["g1_reference"]
    assert ref.status == "missing" and ref.missing_reason == "evidence_failed_verification"
    assert r["g1_description"].value == "Pago INV-2022-0042 ACME"  # the row's other cells stay
    assert r["g1_amount"].status in ("populated", "partial")


async def test_an_amount_column_takes_an_amount_without_retry():
    hit = _hit(LINE)
    llm = FakeLLM(_reply(hit, "INV-2022-0042"))
    r = await map_fields(CTX, _grid(), DOCS, FakeRetriever([hit]), llm)
    assert len(llm.calls) == 1
    assert r["g1_amount"].status == "populated" and r["g1_reference"].status == "populated"


async def test_a_table_cell_under_a_reference_column_rejects_an_amount_row():
    hit = _hit(LINE)
    cols = ("Description", "Reference", "Amount")
    good = ["Pago INV-2022-0042 ACME", "INV-2022-0042", "1.183,51 DR"]
    bad = ["Pago INV-2022-0042 ACME", "1.183,51 DR", "1.183,51 DR"]
    llm = FakeLLM(_rows_reply([(bad, [(hit, LINE)])]), _rows_reply([(good, [(hit, LINE)])]))
    f = _table_field(columns=cols)
    r = (await map_fields(CTX, [f], DOCS, FakeRetriever([hit]), llm))["t"]
    assert AMOUNT_NOTE in llm.calls[1][0][-1]["content"]
    assert r.value == [good] and r.status == "populated"
    # an Amount column keeps its amount: no rejection, one call
    llm2 = FakeLLM(_rows_reply([(good, [(hit, LINE)])]))
    r2 = (await map_fields(CTX, [f], DOCS, FakeRetriever([hit]), llm2))["t"]
    assert len(llm2.calls) == 1 and r2.value == [good]


@pytest.mark.parametrize(
    "header, etype, expected",
    [
        ("Reference", "text", True),
        ("Descripci\u00f3n", "text", True),
        ("N\u00famero de referencia", "text", True),
        ("Account balance", "text", False),
        ("Card spend", "text", False),
        ("Amount", "text", False),
        ("Reference", "currency", False),
        (None, "text", False),
    ],
)
def test_only_text_cells_under_an_identifier_header_reject_an_amount(header, etype, expected):
    from doc_intel.mapping.engine import _amount_in_text

    assert _amount_in_text(etype, header, "1.183,51 DR") is expected
