"""Fast-first, think-on-escalation: one bounded thinking retry per slot (system-design § 7)."""

import json

import pytest
from structlog.testing import capture_logs

from doc_intel.core.config import get_settings
from doc_intel.mapping.engine import map_field, map_fields, map_group
from tests.fakes import FakeLLM
from tests.unit.test_batch_mapping import TEXT, _entry, _reply, _slot
from tests.unit.test_charts import RAIN, _chart
from tests.unit.test_mapping_engine import (
    CTX,
    DOCS,
    H1,
    H2,
    NOT_FOUND,
    Q1,
    Q2,
    FakeRetriever,
    _cand,
    _field,
    _hit,
)
from tests.unit.test_row_groups import (
    ROSTER,
    ROSTER_ROWS,
    TRIP_ROWS,
    TRIPS,
    _cell_grid,
    _rows_reply,
    _seq,
    _table_field,
)
from tests.unit.test_synthesis import DOMAINS, _synth, _two_quotes

INVALID = "not json"


@pytest.fixture(autouse=True)
def escalation(monkeypatch):
    monkeypatch.setenv("THINK_ON_ESCALATION", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


def _reply1(*cands, incomplete=False):
    return json.dumps({"candidates": list(cands), "not_found": False, "incomplete": incomplete})


ONE = _reply1(_cand("12000", (H1, Q1)))
TWO = _reply1(_cand("12000", (H1, Q1)), _cand("15400", (H2, Q2)))
UNVERIFIED = _reply1(_cand("9000", (H1, "9,000 customers were affected")))
HIGH = H1.model_copy(update={"score": 0.9})


async def _map(llm, hits=(H1, H2)):
    return await map_field(CTX, _field("number"), DOCS, FakeRetriever(list(hits)), llm)


@pytest.mark.parametrize(
    "reply, status",
    [(ONE, "populated"), (_reply1(_cand("12000", (H1, Q1)), incomplete=True), "partial")],
)
async def test_populated_and_partial_slots_never_think(reply, status):
    llm = FakeLLM(reply)
    assert (await _map(llm, [HIGH, H2])).status == status
    assert llm.thinks == [False] and llm.budgets == [None]


@pytest.mark.parametrize(
    "first, hits, reason, before",
    [
        (TWO, [H1, H2], "conflict", "conflict"),
        (UNVERIFIED, [H1, H2], "verification_failed", "missing"),
        (NOT_FOUND, [HIGH, H2], "high_score_miss", "missing"),
    ],
)
async def test_each_trigger_retries_exactly_once_with_the_thinking_budget(
    first, hits, reason, before
):
    llm = FakeLLM(first, ONE)
    with capture_logs() as logs:
        r = await _map(llm, hits)
    assert (r.status, r.value) == ("populated", 12000)
    assert llm.thinks == [False, True] and llm.budgets == [None, 2048]
    [event] = [e for e in logs if e["event"] == "think_escalation"]
    assert (event["slot_id"], event["reason"]) == ("f", reason)
    assert (event["before_status"], event["after_status"]) == (before, "populated")
    assert isinstance(event["elapsed_ms"], int)


def _synthesis(f):
    return f.model_copy(update={"mode": "synthesis"})


async def test_a_synthesis_slot_high_score_miss_never_thinks():
    llm = FakeLLM(NOT_FOUND)
    f = _synthesis(_field("number"))
    r = await map_field(CTX, f, DOCS, FakeRetriever([HIGH, H2]), llm)
    assert (r.status, r.missing_reason) == ("missing", "not_found_by_llm")
    assert llm.thinks == [False]


async def test_a_synthesis_slot_conflict_still_escalates():
    llm = FakeLLM(TWO, ONE)
    f = _synthesis(_field("number"))
    r = await map_field(CTX, f, DOCS, FakeRetriever([H1, H2]), llm)
    assert (r.status, llm.budgets) == ("populated", [None, 2048])


async def test_a_synthesis_group_high_score_miss_never_thinks():
    hit = _high("\n".join(ROSTER))
    fields = [_synthesis(f) for f in _cell_grid(["text", "text", "number"], 3)]
    llm = FakeLLM(_seq([], True))
    out = await map_fields(CTX, fields, DOCS, FakeRetriever([hit]), llm)
    assert {r.missing_reason for r in out.values()} == {"not_found_by_llm"}
    assert llm.budgets == [None]


async def test_a_low_score_miss_does_not_escalate():
    llm = FakeLLM(NOT_FOUND)
    r = await _map(llm)  # every hit scores 0.5, under 0.65
    assert (r.status, r.missing_reason) == ("missing", "not_found_by_llm")
    assert llm.thinks == [False]


async def test_an_escalated_miss_keeps_the_first_result_and_never_loops():
    llm = FakeLLM(TWO, NOT_FOUND)
    r = await _map(llm)
    assert r.status == "conflict" and len(r.conflict_details.candidates) == 2  # never dropped
    assert llm.thinks == [False, True]
    llm = FakeLLM(NOT_FOUND, NOT_FOUND)
    r = await _map(llm, [HIGH])
    assert (r.status, r.missing_reason) == ("missing", "not_found_by_llm")
    assert llm.thinks == [False, True]


async def test_an_escalated_invalid_reply_keeps_the_first_result():
    llm = FakeLLM(UNVERIFIED, INVALID, INVALID)  # the second INVALID is complete_json's repair
    with capture_logs() as logs:
        r = await _map(llm)
    [event] = [e for e in logs if e["event"] == "think_escalation"]
    assert (event["after_status"], event["error"]) == ("missing", "LLMInvalidOutput")
    assert (r.status, r.missing_reason) == ("missing", "evidence_failed_verification")
    assert llm.thinks == [False, True, True] and not llm.replies


async def test_escalation_is_off_when_disabled(monkeypatch):
    monkeypatch.setenv("THINK_ON_ESCALATION", "false")
    get_settings.cache_clear()
    llm = FakeLLM(TWO)
    assert (await _map(llm)).status == "conflict"
    assert llm.thinks == [False]


async def test_a_batch_escalates_only_its_failed_slot_once():
    h = _hit(TEXT).model_copy(update={"score": 0.9})
    fields = [_slot(1, "Lead"), _slot(2, "Region"), _slot(3, "Status")]
    batch = _reply(
        _entry(1, "Acme Corp", h, "Lead: Acme Corp"),
        _entry(2, "North", h, "Region: North"),
        _entry(3, "Retired", h, "Status: Retired"),  # not in the chunk
    )
    fixed = _reply1(_cand("Active", (h, "Status: Active")))
    llm = FakeLLM(batch, fixed)
    out = await map_fields(CTX, fields, DOCS, FakeRetriever([h]), llm)
    assert llm.thinks == [False, True] and llm.budgets == [None, 2048]
    assert [out[f].status for f in ("f1", "f2", "f3")] == ["populated"] * 3
    assert out["f3"].value == "Active"


# ---- tables, charts and record groups ---------------------------------------------------------


def _event(logs):
    [event] = [e for e in logs if e["event"] == "think_escalation"]
    return event


def _high(text):
    return _hit(text).model_copy(update={"score": 0.9})


def _trips(hit, rows=TRIP_ROWS):
    return _rows_reply([(c, [(hit, s)]) for c, s in zip(rows, TRIPS, strict=True)])


NO_STUDENTS = [[*row[:2], ""] for row in TRIP_ROWS]  # the fast pass drops a whole column


async def _table(llm, hit):
    return (await map_fields(CTX, [_table_field()], DOCS, FakeRetriever([hit]), llm))["t"]


async def test_a_populated_table_never_escalates():
    hit = _high("\n".join(TRIPS))
    llm = FakeLLM(_trips(hit))
    assert (await _table(llm, hit)).status == "populated"
    assert llm.budgets == [None]


@pytest.mark.parametrize(
    "first, reason",
    [
        (lambda h: _rows_reply([], True), "high_score_miss"),  # an empty table
        (lambda h: _trips(h, NO_STUDENTS), "missing_column"),
        (lambda h: _rows_reply([(["x", "y", "z"], [(h, TRIPS[0])])]), "verification_failed"),
    ],
)
async def test_a_table_escalates_once_and_keeps_the_thinking_rows(first, reason):
    hit = _high("\n".join(TRIPS))
    llm = FakeLLM(first(hit), _trips(hit))
    with capture_logs() as logs:
        r = await _table(llm, hit)
    assert (r.status, r.value) == ("populated", TRIP_ROWS)
    assert llm.budgets == [None, 2048] and llm.thinks == [True, True]
    event = _event(logs)
    assert (event["slot_id"], event["reason"], event["after_status"]) == ("t", reason, "populated")


async def test_a_missing_column_under_the_score_threshold_does_not_escalate():
    hit = _hit("\n".join(TRIPS))  # 0.5
    llm = FakeLLM(_trips(hit, NO_STUDENTS))
    r = await _table(llm, hit)
    assert r.status == "partial" and llm.budgets == [None]


@pytest.mark.parametrize("second", [_rows_reply([], True), INVALID])
async def test_a_failed_table_escalation_keeps_the_first_rows_and_never_loops(second):
    hit = _high("\n".join(TRIPS))
    llm = FakeLLM(_trips(hit, NO_STUDENTS), second, second)  # INVALID twice: the repair call
    r = await _table(llm, hit)
    assert (r.status, r.value) == ("partial", NO_STUDENTS)
    assert llm.budgets[:2] == [None, 2048] and set(llm.budgets[2:]) <= {2048}


async def test_an_escalation_that_fills_fewer_cells_keeps_the_first_pass():
    hit = _high("\n".join(TRIPS))
    fewer = _rows_reply([(NO_STUDENTS[0], [(hit, TRIPS[0])])])
    llm = FakeLLM(_trips(hit, NO_STUDENTS), fewer)
    assert (await _table(llm, hit)).value == NO_STUDENTS
    assert llm.budgets == [None, 2048]


def _points(hit, *points):
    return _rows_reply([([c, n], [(hit, f"{c} {n} mm")]) for c, n in points])


async def test_a_chart_with_one_verified_point_escalates_once():
    hit = _high(RAIN)
    one = _points(hit, ("January", "82"))  # one point is no chart: verification failed
    llm = FakeLLM(one, _points(hit, ("January", "82"), ("February", "64")))
    with capture_logs() as logs:
        out = await map_group(CTX, [[_chart()]], DOCS, FakeRetriever([hit]), llm)
    assert (out["c"].status, out["c"].value) == (
        "populated",
        [["January", "82"], ["February", "64"]],
    )
    assert llm.budgets == [None, 2048]
    assert _event(logs)["reason"] == "verification_failed"


def _roster(hit, rows):
    return _seq([(cells, [(hit, s)]) for cells, s in zip(rows, ROSTER, strict=True)])


NO_HOURS = [[*row[:2], ""] for row in ROSTER_ROWS]  # a `ratings`-like column empty in every row


async def test_a_record_grid_missing_a_column_escalates_once():
    hit = _high("\n".join(ROSTER))
    fields = _cell_grid(["text", "text", "number"], 3)
    llm = FakeLLM(_roster(hit, NO_HOURS), _roster(hit, ROSTER_ROWS))
    with capture_logs() as logs:
        out = await map_fields(CTX, fields, DOCS, FakeRetriever([hit]), llm)
    assert [r.status for r in out.values()] == ["populated"] * 9
    assert [out[f"c{r}_2"].value for r in (1, 2, 3)] == [12, 9, 15]
    assert llm.budgets == [None, 2048] and llm.thinks == [True, True]
    event = _event(logs)
    assert (event["slot_id"], event["reason"]) == ("c1_0", "missing_column")
    assert (event["before_status"], event["after_status"]) == ("partial", "populated")


async def test_an_empty_record_group_escalation_that_finds_nothing_keeps_the_miss():
    hit = _high("\n".join(ROSTER))
    fields = _cell_grid(["text", "text", "number"], 3)
    llm = FakeLLM(_seq([], True), _seq([], True))
    out = await map_fields(CTX, fields, DOCS, FakeRetriever([hit]), llm)
    assert {(r.status, r.missing_reason) for r in out.values()} == {("missing", "not_found_by_llm")}
    assert llm.budgets == [None, 2048] and not llm.replies


async def test_a_complete_record_grid_never_escalates():
    hit = _high("\n".join(ROSTER))
    llm = FakeLLM(_roster(hit, ROSTER_ROWS))
    await map_fields(
        CTX, _cell_grid(["text", "text", "number"], 3), DOCS, FakeRetriever([hit]), llm
    )
    assert llm.budgets == [None]


# ---- escalated lists keep the length limits ---------------------------------------------------

WAREHOUSE = DOMAINS["warehouse"]


async def _list(llm, hit):
    f = _field("bullet_list", max_items=1)
    return await map_field(CTX, f, DOCS, FakeRetriever([hit]), llm)


async def test_an_escalated_list_over_max_items_is_shortened_by_synthesis():
    hit = _high(WAREHOUSE["text"])
    short = _synth((WAREHOUSE["items"][1], hit, WAREHOUSE["quotes"][1]))
    llm = FakeLLM(NOT_FOUND, _two_quotes(hit, WAREHOUSE), short)
    r = await _list(llm, hit)
    assert (r.status, r.value) == ("populated", [WAREHOUSE["items"][1]])
    assert llm.budgets == [None, 2048, None]  # the length check's rewrite runs fast


async def test_an_escalated_list_that_still_overflows_keeps_the_first_result():
    hit = _high(WAREHOUSE["text"])
    both = _synth(
        *((i, hit, q) for i, q in zip(WAREHOUSE["items"], WAREHOUSE["quotes"], strict=True))
    )
    llm = FakeLLM(NOT_FOUND, _two_quotes(hit, WAREHOUSE), both, both)  # the rewrite overflows too
    with capture_logs() as logs:
        r = await _list(llm, hit)
    assert (r.status, r.missing_reason) == ("missing", "not_found_by_llm")
    assert not llm.replies and _event(logs)["after_status"] == "missing"
