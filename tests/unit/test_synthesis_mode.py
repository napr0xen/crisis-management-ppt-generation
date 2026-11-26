"""IRField.mode: extractive slots copy verbatim, synthesis slots are written or computed from
verified quotes (system-design § 7)."""

import json

from doc_intel.llm.prompts import load_prompt
from doc_intel.mapping.engine import map_field, map_fields
from doc_intel.templates.schemas import IRField
from tests.fakes import FakeLLM
from tests.unit.test_mapping_engine import CTX, DOCS, FakeRetriever, _cand, _field, _hit
from tests.unit.test_synthesis import DOMAINS, _synth

MUSEUM = DOMAINS["museum"]
SUMMARY = "62% of visitors rated the dinosaur hall excellent"
TOTALS = _hit("Total deposits 150,000.00 and total withdrawals 120,500.50 in March.")
TOTALS_Q = "Total deposits 150,000.00 and total withdrawals 120,500.50"


def _synthesis(expected_type="text", **constraints):
    return _field(expected_type, **constraints).model_copy(update={"mode": "synthesis"})


def _prompts(llm):
    return [c[0][0]["content"] for c in llm.calls]


def _derived(value):
    return json.dumps(
        {"candidates": [_cand(value, (TOTALS, TOTALS_Q))], "not_found": False, "incomplete": False}
    )


def test_mode_defaults_to_extractive():
    assert _field().mode == "extractive"
    stored = _field().model_dump(exclude={"mode"})  # an IR stored before the mode existed
    assert IRField.model_validate(stored).mode == "extractive"


async def test_a_synthesis_text_slot_is_written_without_a_verbatim_pass():
    hit = _hit(MUSEUM["text"])
    llm = FakeLLM(_synth((SUMMARY, hit, MUSEUM["quotes"][0])))
    r = await map_field(CTX, _synthesis(), DOCS, FakeRetriever([hit]), llm)
    assert (r.status, r.value) == ("populated", SUMMARY)
    assert [e.item for e in r.evidence] == [0]
    assert _prompts(llm) == [load_prompt("field_synthesis")]


async def test_an_unsupported_synthesis_text_slot_stays_missing():
    hit = _hit(MUSEUM["text"])
    llm = FakeLLM(_synth(("Revenue doubled after the new gift shop", hit, MUSEUM["quotes"][0])))
    r = await map_field(CTX, _synthesis(), DOCS, FakeRetriever([hit]), llm)
    assert (r.status, r.missing_reason) == ("missing", "not_found_by_llm")


async def test_a_derived_currency_is_partial_with_its_input_quotes():
    llm = FakeLLM(_derived("29,499.50"))
    r = await map_field(CTX, _synthesis("currency"), DOCS, FakeRetriever([TOTALS]), llm)
    assert (r.status, r.value, r.missing_reason) == ("partial", 29499.5, "insufficient_evidence")
    assert [e.quote for e in r.evidence] == [TOTALS_Q]
    assert '"mode":"synthesis"' in llm.calls[0][0][1]["content"]


async def test_an_extractive_currency_never_accepts_a_derived_value():
    llm = FakeLLM(_derived("29,499.50"))
    r = await map_field(CTX, _field("currency"), DOCS, FakeRetriever([TOTALS]), llm)
    assert (r.status, r.missing_reason) == ("missing", "evidence_failed_verification")


async def test_a_wrong_derived_value_fails_verification():
    llm = FakeLLM(_derived("30,000.00"))
    r = await map_field(CTX, _synthesis("currency"), DOCS, FakeRetriever([TOTALS]), llm)
    assert (r.status, r.missing_reason) == ("missing", "evidence_failed_verification")


async def test_a_stated_value_of_a_synthesis_slot_is_populated():
    llm = FakeLLM(_derived("150,000.00"))
    r = await map_field(CTX, _synthesis("currency"), DOCS, FakeRetriever([TOTALS]), llm)
    assert (r.status, r.value) == ("populated", 150000)


async def test_map_fields_maps_a_synthesis_slot_alone_even_on_the_opening_unit():
    hit = _hit(MUSEUM["text"])
    llm = FakeLLM(_synth((SUMMARY, hit, MUSEUM["quotes"][0])))
    out = await map_fields(CTX, [_synthesis()], DOCS, FakeRetriever([hit]), llm)
    assert out["f"].status == "populated"
    assert _prompts(llm) == [load_prompt("field_synthesis")]


async def _with_siblings(item):
    hit = _hit(MUSEUM["text"])
    f = _synthesis().model_copy(update={"context": "Closing balance"})
    llm = FakeLLM(_synth((item, hit, MUSEUM["quotes"][0])))
    r = await map_field(CTX, f, DOCS, FakeRetriever([hit]), llm, ["Opening balance"])
    return r, llm


async def test_a_synthesis_text_slot_skips_the_heading_check():
    # a written summary never sits under the slot's heading in the source
    r, llm = await _with_siblings(SUMMARY)
    assert (r.status, r.value) == ("populated", SUMMARY)
    assert _prompts(llm) == [load_prompt("field_synthesis")]


async def test_a_synthesis_item_worded_unlike_its_quotes_is_partial():
    # every number is in a verified quote, few of its words are (a summary of table rows)
    r, _ = await _with_siblings("Strong overall engagement: 62% approval")
    assert (r.status, r.missing_reason) == ("partial", "insufficient_evidence")
    assert r.value == "Strong overall engagement: 62% approval"


async def test_a_synthesis_item_with_an_unquoted_number_is_rejected():
    r, _ = await _with_siblings("Strong overall engagement: 75% approval")
    assert (r.status, r.missing_reason) == ("missing", "not_found_by_llm")


async def test_an_extractive_list_still_needs_the_word_share():
    hit = _hit(MUSEUM["text"])
    llm = FakeLLM(
        json.dumps({"candidates": [], "not_found": False, "incomplete": False}),
        _synth(("Strong overall engagement: 62% approval", hit, MUSEUM["quotes"][0])),
    )
    r = await map_field(CTX, _field("bullet_list"), DOCS, FakeRetriever([hit]), llm)
    assert r.status == "missing"


async def test_a_number_free_synthesis_item_needs_half_its_words_in_the_quotes():
    # "visitors" and "dinosaurs" are quoted, "loved" and "overall" are not: 2 of 4 words
    r, _ = await _with_siblings("Visitors loved dinosaurs overall")
    assert (r.status, r.missing_reason) == ("partial", "insufficient_evidence")
