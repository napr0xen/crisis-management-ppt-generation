"""Phase 21 item 4: an extractive free-text slot sized for prose that the model reports not found
gets one grounded synthesis attempt; small and typed slots stay strict."""

import json

import pytest

from doc_intel.llm.prompts import load_prompt
from doc_intel.mapping.engine import map_field, map_fields
from doc_intel.templates.schemas import Constraints, IRField
from tests.fakes import FakeLLM
from tests.unit.test_mapping_engine import CTX, DOCS, NOT_FOUND, FakeRetriever, _field, _hit
from tests.unit.test_row_groups import _seq
from tests.unit.test_synthesis import DOMAINS, _synth

BOTH = pytest.mark.parametrize("d", DOMAINS.values(), ids=DOMAINS.keys())
WEAK = {  # every number quoted, few words shared: a summary of the quoted line
    "museum": "Strong overall engagement: 62% approval",
    "warehouse": "Serious incident overall: 340 losses",
}


def _prose(expected_type="text", fit=89, **constraints):
    return _field(expected_type, **constraints).model_copy(update={"fit_chars": fit})


def _systems(llm):
    return [c[0][0]["content"] for c in llm.calls]


@BOTH
async def test_a_prose_slot_not_found_is_synthesized_and_populated_when_well_backed(d):
    hit = _hit(d["text"])
    llm = FakeLLM(NOT_FOUND, _synth((d["items"][0], hit, d["quotes"][0])))
    r = await map_field(CTX, _prose(), DOCS, FakeRetriever([hit]), llm)
    assert (r.status, r.value) == ("populated", d["items"][0])
    assert [e.quote for e in r.evidence] == [d["quotes"][0]]
    assert _systems(llm) == [load_prompt("field_extract"), load_prompt("field_synthesis")]


@pytest.mark.parametrize("name", DOMAINS)
async def test_a_weakly_backed_prose_fallback_is_at_most_partial(name):
    d = DOMAINS[name]
    hit = _hit(d["text"])
    quote = d["quotes"][0] if name == "museum" else d["quotes"][1]
    llm = FakeLLM(NOT_FOUND, _synth((WEAK[name], hit, quote)))
    r = await map_field(CTX, _prose(), DOCS, FakeRetriever([hit]), llm)
    assert (r.status, r.value, r.missing_reason) == ("partial", WEAK[name], "insufficient_evidence")


@BOTH
async def test_an_invented_prose_fallback_stays_missing(d):
    hit = _hit(d["text"])
    llm = FakeLLM(NOT_FOUND, _synth((d["invented"], hit, d["quotes"][0])))
    r = await map_field(CTX, _prose(), DOCS, FakeRetriever([hit]), llm)
    assert (r.status, r.missing_reason) == ("missing", "not_found_by_llm")


@BOTH
async def test_a_prose_list_slot_not_found_is_synthesized(d):
    hit = _hit(d["text"])
    items = [(i, hit, q) for i, q in zip(d["items"], d["quotes"], strict=True)]
    llm = FakeLLM(NOT_FOUND, _synth(*items))
    r = await map_field(
        CTX, _prose("bullet_list", fit=None, max_chars=300), DOCS, FakeRetriever([hit]), llm
    )
    assert (r.status, r.value) == ("populated", d["items"])


@pytest.mark.parametrize(
    "field",
    [
        _prose(fit=12),  # a short label
        _field("text"),  # no size known: strict
        _prose("number", fit=89),
        _prose("currency", fit=89),
        _prose("date", fit=89),
    ],
    ids=["short_text", "unsized_text", "number", "currency", "date"],
)
async def test_small_or_typed_slots_stay_strictly_not_found(field):
    hit = _hit(DOMAINS["museum"]["text"])
    llm = FakeLLM(NOT_FOUND)
    r = await map_field(CTX, field, DOCS, FakeRetriever([hit]), llm)
    assert (r.status, r.missing_reason) == ("missing", "not_found_by_llm")
    assert len(llm.calls) == 1


async def test_a_prose_slot_with_a_verified_value_is_never_synthesized():
    d = DOMAINS["museum"]
    hit = _hit(d["text"])
    found = {
        "value": d["quotes"][1],
        "quotes": [{"chunk_id": str(hit.chunk_id), "quote": d["quotes"][1]}],
    }
    llm = FakeLLM(json.dumps({"candidates": [found], "not_found": False, "incomplete": False}))
    r = await map_field(CTX, _prose(), DOCS, FakeRetriever([hit]), llm)
    assert r.status == "populated" and len(llm.calls) == 1


# ---- in a batch, and a numbered list such as Key Findings 01-04 -------------------------------


def _slot(fid, label, slide=8, context=None, fit=89, expected_type="text", n=None):
    return IRField(
        field_id=fid,
        anchor_id=f"pptx:s{slide}:sp{n or len(fid)}",
        section_path=[f"Slide {slide}"],
        label=label,
        description=label,
        expected_type=expected_type,
        required=True,
        write_mode="replace_token",
        constraints=Constraints(token=f"[{label}]"),
        context=context,
        fit_chars=fit,
    )


async def test_a_batched_prose_slot_not_found_is_synthesized():
    d = DOMAINS["warehouse"]
    hit = _hit(d["text"])
    fields = [_slot("site", "Aisle", fit=12, n=1), _slot("summary", "Overall observation", n=2)]
    batch = json.dumps(
        {
            "slots": [
                {"n": 1, "candidates": [], "not_found": True, "incomplete": False},
                {"n": 2, "candidates": [], "not_found": True, "incomplete": False},
            ]
        }
    )
    llm = FakeLLM(batch, _synth((d["items"][1], hit, d["quotes"][1])))
    out = await map_fields(CTX, fields, DOCS, FakeRetriever([hit]), llm)
    assert out["site"].status == "missing"  # short: strict
    assert (out["summary"].status, out["summary"].value) == ("populated", d["items"][1])
    assert len(llm.calls) == 2


def _findings(fit):
    return [
        _slot(f"key_finding_{i}", f"Key Finding {i}", context=f"0{i}", fit=fit, n=10 + i)
        for i in range(1, 5)
    ]


@BOTH
async def test_a_prose_numbered_list_not_found_is_synthesized(d):
    hit = _hit(d["text"])
    items = [(i, hit, q) for i, q in zip(d["items"], d["quotes"], strict=True)]
    llm = FakeLLM(_seq([], True), _synth(*items))
    out = await map_fields(CTX, _findings(89), DOCS, FakeRetriever([hit]), llm)
    got = [out[f"key_finding_{i}"] for i in range(1, 5)]
    assert [r.value for r in got[:2]] == d["items"]
    assert [r.status for r in got] == ["populated", "populated", "missing", "missing"]
    assert _systems(llm)[1] == load_prompt("field_synthesis")


async def test_a_weak_numbered_list_item_is_partial():
    d = DOMAINS["museum"]
    hit = _hit(d["text"])
    llm = FakeLLM(_seq([], True), _synth((WEAK["museum"], hit, d["quotes"][0])))
    out = await map_fields(CTX, _findings(89), DOCS, FakeRetriever([hit]), llm)
    assert (out["key_finding_1"].status, out["key_finding_1"].value) == ("partial", WEAK["museum"])


async def test_a_short_numbered_list_not_found_stays_missing():
    hit = _hit(DOMAINS["museum"]["text"])
    llm = FakeLLM(_seq([], True))
    out = await map_fields(CTX, _findings(12), DOCS, FakeRetriever([hit]), llm)
    assert {r.missing_reason for r in out.values()} == {"not_found_by_llm"}
    assert len(llm.calls) == 1
