"""Phase 21 item 3: verifier rejection reasons go back to the retry, codes only in the logs."""

import json
import uuid

import pytest
from structlog.testing import capture_logs

from doc_intel.core.config import get_settings
from doc_intel.mapping.engine import _near_cited, map_field, map_fields
from tests.fakes import FakeLLM
from tests.unit.test_mapping_engine import (
    CTX,
    DOCS,
    NOT_FOUND,
    FakeRetriever,
    _cand,
    _ext,
    _field,
    _hit,
    _slot_field,
)


@pytest.fixture(autouse=True)
def escalation(monkeypatch):
    monkeypatch.setenv("THINK_ON_ESCALATION", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


def _reply(*cands):
    return json.dumps({"candidates": list(cands), "not_found": False, "incomplete": False})


def _last_user(llm, call):
    return llm.calls[call][0][-1]["content"]


def _codes(logs):
    return [e["codes"] for e in logs if e["event"] == "verification_rejected"]


OUTAGE = _hit("Approximately 12,000 customers were affected by the outage.")
OUTAGE_Q = "12,000 customers were affected"
AUDIT = _hit("The site audit closed on 14 March 2025 with two findings.")
AUDIT_Q = "closed on 14 March 2025"
FAR = _hit("Unrelated: the cafeteria menu changed in spring.")

# (field type, the wrong first reply, the code, the fixed reply, the verified value, the hit)
CASES = {
    "missing_number": (
        "number",
        _reply(_cand("9000", (OUTAGE, OUTAGE_Q))),
        "number_not_in_quote",
        _reply(_cand("12000", (OUTAGE, OUTAGE_Q))),
        12000,
        OUTAGE,
    ),
    "date_mismatch": (
        "date",
        _reply(_cand("2025-03-15", (AUDIT, AUDIT_Q))),
        "date_not_in_quote",
        _reply(_cand("2025-03-14", (AUDIT, AUDIT_Q))),
        "2025-03-14",
        AUDIT,
    ),
    "quote_mismatch": (
        "text",
        _reply(_cand("two findings", (AUDIT, "with three findings"))),
        "quote_not_in_chunk",
        _reply(_cand("two findings", (AUDIT, "with two findings"))),
        "two findings",
        AUDIT,
    ),
}


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
async def test_a_rejection_reason_is_logged_and_sent_to_the_retry_over_the_cited_chunks(case):
    expected_type, wrong, code, fixed, value, hit = case
    llm = FakeLLM(wrong, fixed)
    with capture_logs() as logs:
        r = await map_field(CTX, _field(expected_type), DOCS, FakeRetriever([hit, FAR]), llm)
    assert (r.status, r.value) == ("populated", value)
    assert llm.budgets == [None, 2048]  # the thinking retry
    assert _codes(logs) == [[code]]
    retry = llm.calls[1][0]
    assert code in _last_user(llm, 1)  # the reason goes back to the model
    assert retry[-2]["role"] == "assistant"  # with its earlier reply echoed
    assert FAR.text in llm.calls[0][0][1]["content"]
    assert FAR.text not in retry[1]["content"]  # narrowed to the cited chunk


async def test_reason_codes_are_logged_without_values_or_quotes():
    llm = FakeLLM(CASES["missing_number"][1], NOT_FOUND)
    with capture_logs() as logs:
        await map_field(CTX, _field("number"), DOCS, FakeRetriever([OUTAGE]), llm)
    [event] = [e for e in logs if e["event"] == "verification_rejected"]
    assert set(event) == {"event", "log_level", "slot_id", "codes"}
    assert "9000" not in json.dumps(event) and "customers" not in json.dumps(event)


async def test_an_uncited_chunk_id_retries_over_every_chunk():
    stray = _reply({"value": "12000", "quotes": [{"chunk_id": "nope", "quote": OUTAGE_Q}]})
    llm = FakeLLM(stray, CASES["missing_number"][3])
    with capture_logs() as logs:
        r = await map_field(CTX, _field("number"), DOCS, FakeRetriever([OUTAGE, FAR]), llm)
    assert r.status == "populated" and _codes(logs) == [["quote_not_in_chunk"]]
    assert FAR.text in llm.calls[1][0][1]["content"]


async def test_conflicts_retry_without_reasons():
    two = _reply(_cand("12000", (OUTAGE, OUTAGE_Q)), _cand("2025", (AUDIT, "March 2025")))
    llm = FakeLLM(two, NOT_FOUND)
    with capture_logs() as logs:
        await map_field(CTX, _field("number"), DOCS, FakeRetriever([OUTAGE, AUDIT]), llm)
    assert _codes(logs) == []
    assert len(llm.calls[1][0]) == 2  # a plain escalated call: no echo, no notes


# ---- one value, two roles: the misassigned role is retried with the reason --------------------

FIN = _hit(
    "Opening balance 389.531,45 $ ; net movement 12.500,00 $ ; closing balance 402.031,45 $",
    section="Cash flow",
)
CLOSING_Q = "closing balance 402.031,45 $"
NET = _slot_field("net", "Net Cash Movement", "pptx:s2:sp14", "currency", slide=2)
CLOSING = _slot_field("closing", "Closing Balance", "pptx:s2:sp17", "currency", slide=2)
KPI = _hit("Target completion 90 % ; actual completion reached 87 % this quarter.", section="KPI")
TARGET = _slot_field("target", "Target", "pptx:s3:sp4", "percent", slide=3)
ACTUAL = _slot_field("actual", "Actual", "pptx:s3:sp5", "percent", slide=3)

ROLE_CASES = {
    "finance": (
        FIN,
        NET,
        CLOSING,
        "402.031,45 $",
        CLOSING_Q,
        "12.500,00 $",
        "movement 12.500,00 $",
    ),
    "kpi": (KPI, TARGET, ACTUAL, "87 %", "reached 87 % this quarter", "90 %", "completion 90 %"),
}


def _both(hit, value, quote):
    cand = _cand(value, (hit, quote))
    entries = [{"n": n, "candidates": [cand], "not_found": False} for n in (1, 2)]
    return json.dumps({"slots": [e | {"incomplete": False} for e in entries]})


KEEP_SECOND = '{"about": "the stated role", "slots": [2]}'


@pytest.mark.parametrize("case", ROLE_CASES.values(), ids=ROLE_CASES.keys())
async def test_the_misassigned_role_is_retried_with_the_role_reason(case):
    hit, wrong, kept, value, quote, own, own_quote = case
    llm = FakeLLM(_both(hit, value, quote), KEEP_SECOND, _reply(_cand(own, (hit, own_quote))))
    with capture_logs() as logs:
        out = await map_fields(CTX, [wrong, kept], DOCS, FakeRetriever([hit]), llm)
    assert out[kept.field_id].status == "populated"  # the stated role is never dropped
    assert out[wrong.field_id].status == "populated"
    assert out[wrong.field_id].value != out[kept.field_id].value
    note = _last_user(llm, 2)
    assert "slot_role" in note and kept.label in note
    assert llm.calls[2][0][-2]["role"] == "assistant"  # the rejected value is echoed
    assert _codes(logs) == [["slot_role"]]
    assert len(llm.calls) == 3  # batch, role check, one retry: no second role check


async def test_a_role_retry_repeating_the_rejected_value_stays_missing():
    first = _both(FIN, "402.031,45 $", CLOSING_Q)
    llm = FakeLLM(first, KEEP_SECOND, _reply(_cand("402.031,45 $", (FIN, CLOSING_Q))))
    out = await map_fields(CTX, [NET, CLOSING], DOCS, FakeRetriever([FIN]), llm)
    assert out["closing"].status == "populated"
    assert (out["net"].status, out["net"].missing_reason) == (
        "missing",
        "evidence_failed_verification",
    )
    assert len(llm.calls) == 3


async def test_a_role_retry_that_finds_nothing_stays_missing():
    llm = FakeLLM(_both(FIN, "402.031,45 $", CLOSING_Q), KEEP_SECOND, NOT_FOUND)
    out = await map_fields(CTX, [NET, CLOSING], DOCS, FakeRetriever([FIN]), llm)
    assert (out["closing"].status, out["net"].status) == ("populated", "missing")


async def test_a_role_check_error_is_not_retried():
    llm = FakeLLM(_both(FIN, "402.031,45 $", CLOSING_Q), "nope", "nope")
    out = await map_fields(CTX, [NET, CLOSING], DOCS, FakeRetriever([FIN]), llm)
    assert {r.missing_reason for r in out.values()} == {"llm_error"}
    assert len(llm.calls) == 3  # the batch and the role check's two attempts


def test_the_retry_chunks_are_the_cited_ones_and_their_neighbors():
    doc = OUTAGE.document_id
    at = [OUTAGE.model_copy(update={"chunk_index": i, "chunk_id": uuid.uuid4()}) for i in (3, 4, 9)]
    other_doc = at[1].model_copy(update={"document_id": uuid.uuid4(), "chunk_id": uuid.uuid4()})
    hits = [*at, other_doc]
    cited = _ext(_cand("12000", (at[0], OUTAGE_Q)))
    assert _near_cited(hits, cited) == at[:2]  # chunk 4 beside 3; 9 is far, the other doc is out
    assert all(h.document_id == doc for h in _near_cited(hits, cited))
