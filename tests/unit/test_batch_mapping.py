"""Per-unit batched mapping of loose fields (system-design § 7.1b)."""

import json

import pytest

from doc_intel.core.config import get_settings
from doc_intel.llm.prompts import load_prompt
from doc_intel.mapping.engine import _rule_note, map_fields
from doc_intel.mapping.extractor import hit_budget
from doc_intel.mapping.schemas import Evidence
from doc_intel.templates.schemas import Constraints, IRField
from tests.fakes import FakeLLM
from tests.unit.test_mapping_engine import CTX, DOCS, NOT_FOUND, FakeRetriever, _hit

BATCH = load_prompt("slot_values")
SINGLE = load_prompt("field_extract")
CHECK = load_prompt("candidate_check")
YES = '{"checks": [{"about": "x", "match": true}]}'
NO = '{"checks": [{"about": "x", "match": false}]}'
TEXT = "Lead: Acme Corp\nRegion: North\nStatus: Active\nOpen tenders: 12 today. Total value 64.8M."


def _slot(i, label="Lead", context=None, slide=2, expected_type="text", **kw):
    return IRField(
        field_id=f"f{i}",
        anchor_id=f"pptx:s{slide}:sp{i}",
        section_path=[f"Slide {slide}"],
        label=label,
        description=f"{label} of the item",
        expected_type=expected_type,
        required=True,
        write_mode="replace_text",
        constraints=kw.pop("constraints", Constraints()),
        context=context,
        **kw,
    )


def _entry(n, value=None, hit=None, quote=None):
    if value is None:
        return {"n": n, "candidates": [], "not_found": True, "incomplete": False}
    cand = {"value": value, "quotes": [{"chunk_id": str(hit.chunk_id), "quote": quote}]}
    return {"n": n, "candidates": [cand], "not_found": False, "incomplete": False}


def _reply(*entries):
    return json.dumps({"slots": list(entries)})


def _slots_spec(llm, call=0):
    user = llm.calls[call][0][1]["content"]
    return json.loads(user.split("Slots:\n", 1)[1].split("\n\nChunks:", 1)[0])


async def test_loose_fields_of_one_slide_share_one_call_and_verify_one_by_one():
    h = _hit(TEXT)
    fields = [_slot(1, "Lead"), _slot(2, "Region"), _slot(3, "Status")]
    llm = FakeLLM(
        _reply(
            _entry(1, "Acme Corp", h, "Lead: Acme Corp"),
            _entry(2, "North", h, "Region: North"),
            _entry(3, "Retired", h, "Status: Retired"),  # not in the chunk
        )
    )
    out = await map_fields(CTX, fields, DOCS, FakeRetriever([h]), llm)
    assert len(llm.calls) == 1 and llm.thinks == [False]
    assert llm.calls[0][0][0]["content"] == BATCH
    assert (out["f1"].status, out["f1"].value) == ("populated", "Acme Corp")
    assert (out["f2"].status, out["f2"].value) == ("populated", "North")
    assert out["f1"].evidence[0].quote == "Lead: Acme Corp"
    assert (out["f3"].status, out["f3"].missing_reason) == (
        "missing",
        "evidence_failed_verification",
    )


async def test_a_slot_with_no_entry_is_not_found_and_no_hits_means_no_call():
    h = _hit(TEXT)
    fields = [_slot(1, "Lead"), _slot(2, "Region")]
    llm = FakeLLM(_reply(_entry(1, "Acme Corp", h, "Lead: Acme Corp")))
    out = await map_fields(CTX, fields, DOCS, FakeRetriever([h]), llm)
    assert (out["f2"].status, out["f2"].missing_reason) == ("missing", "not_found_by_llm")
    llm = FakeLLM()
    out = await map_fields(CTX, fields, DOCS, FakeRetriever([]), llm)
    assert {r.missing_reason for r in out.values()} == {"no_relevant_chunks"} and not llm.calls


async def test_the_first_entry_of_a_repeated_n_wins():
    h = _hit(TEXT)
    llm = FakeLLM(
        _reply(_entry(1, "Acme Corp", h, "Lead: Acme Corp"), _entry(1, "North", h, "Region: North"))
    )
    out = await map_fields(CTX, [_slot(1), _slot(2, "Region")], DOCS, FakeRetriever([h]), llm)
    assert out["f1"].value == "Acme Corp"


async def test_list_types_take_a_string_as_one_item_and_scalars_drop_a_list():
    h = _hit(TEXT)
    fields = [_slot(1, "Leads", expected_type="bullet_list"), _slot(2, "Region")]
    llm = FakeLLM(
        _reply(
            _entry(1, "Acme Corp", h, "Lead: Acme Corp"),
            _entry(2, ["North", "Active"], h, "Region: North"),
        )
    )
    out = await map_fields(CTX, fields, DOCS, FakeRetriever([h]), llm)
    assert out["f1"].value == ["Acme Corp"] and out["f1"].status == "populated"
    assert (out["f2"].status, out["f2"].missing_reason) == ("missing", "not_found_by_llm")


async def test_an_unverified_list_slot_goes_to_synthesis_without_thinking():
    h = _hit(TEXT)
    fields = [_slot(1, "Leads", expected_type="bullet_list"), _slot(2, "Region")]
    llm = FakeLLM(
        _reply(_entry(1, "Gamma", h, "Lead: Gamma"), _entry(2, "North", h, "Region: North")),
        '{"items": [], "not_found": true}',
    )
    out = await map_fields(CTX, fields, DOCS, FakeRetriever([h]), llm)
    assert len(llm.calls) == 2 and llm.thinks == [False, False]
    assert out["f1"].status == "missing" and out["f2"].status == "populated"


async def test_a_rule_breaking_field_is_remapped_alone_with_its_own_retry():
    h = _hit(TEXT)
    count = _slot(1, "Open tenders", expected_type="number")
    count = count.model_copy(
        update={"write_mode": "replace_token", "constraints": Constraints(token="[# Open tenders]")}
    )
    llm = FakeLLM(
        _reply(_entry(1, "64.8", h, "Total value 64.8M"), _entry(2, "North", h, "Region: North")),
        json.dumps(
            {
                "candidates": [
                    {
                        "value": "12",
                        "quotes": [
                            {"chunk_id": str(h.chunk_id), "quote": "Open tenders: 12 today"}
                        ],
                    }
                ],
                "not_found": False,
                "incomplete": False,
            }
        ),
    )
    out = await map_fields(CTX, [count, _slot(2, "Region")], DOCS, FakeRetriever([h]), llm)
    assert len(llm.calls) == 2 and llm.calls[1][0][0]["content"] == SINGLE
    assert (out["f1"].status, out["f1"].value) == ("populated", 12)
    assert out["f2"].value == "North"


async def test_one_value_under_two_headings_is_checked_for_those_fields_only():
    h = _hit(TEXT)
    fields = [
        _slot(1, "Lead", context="Alpha card"),
        _slot(2, "Lead", context="Beta card"),
        _slot(3, "Region", context="Gamma card"),
    ]
    llm = FakeLLM(
        _reply(
            _entry(1, "Acme Corp", h, "Lead: Acme Corp"),
            _entry(2, "Acme Corp", h, "Lead: Acme Corp"),
            _entry(3, "North", h, "Region: North"),
        ),
        YES,
        NO,
    )
    out = await map_fields(CTX, fields, DOCS, FakeRetriever([h]), llm)
    assert len(llm.calls) == 3
    assert [c[0][0]["content"] for c in llm.calls[1:]] == [CHECK, CHECK]
    assert out["f1"].status == "populated"
    assert (out["f2"].status, out["f2"].missing_reason) == ("missing", "not_found_by_llm")
    assert out["f3"].status == "populated"


async def test_distinct_values_under_distinct_headings_need_no_check():
    h = _hit(TEXT)
    fields = [_slot(1, "Lead", context="Alpha card"), _slot(2, "Region", context="Beta card")]
    llm = FakeLLM(
        _reply(_entry(1, "Acme Corp", h, "Lead: Acme Corp"), _entry(2, "North", h, "Region: North"))
    )
    out = await map_fields(CTX, fields, DOCS, FakeRetriever([h]), llm)
    assert len(llm.calls) == 1 and out["f1"].status == out["f2"].status == "populated"


async def test_an_invalid_batch_reply_falls_back_to_one_call_per_field():
    h = _hit(TEXT)

    def one(value, quote):
        cand = {"value": value, "quotes": [{"chunk_id": str(h.chunk_id), "quote": quote}]}
        return json.dumps({"candidates": [cand], "not_found": False, "incomplete": False})

    llm = FakeLLM(
        "nope", "still nope", one("Acme Corp", "Lead: Acme Corp"), one("North", "Region: North")
    )
    out = await map_fields(CTX, [_slot(1), _slot(2, "Region")], DOCS, FakeRetriever([h]), llm)
    assert len(llm.calls) == 4
    assert [out["f1"].value, out["f2"].value] == ["Acme Corp", "North"]


async def test_batches_hold_twelve_slots_and_a_lone_slot_keeps_the_single_path():
    first = [_slot(1, slide=1)]
    rest = [_slot(i, f"Label {i}") for i in range(2, 15)]  # 13 slots on slide 2
    llm = FakeLLM(_reply(), _reply(), NOT_FOUND)
    await map_fields(CTX, [*first, *rest], DOCS, FakeRetriever([_hit(TEXT)]), llm)
    assert len(llm.calls) == 3
    assert len(_slots_spec(llm, 1)["slots"]) == 12 and llm.calls[1][0][0]["content"] == BATCH
    assert llm.calls[2][0][0]["content"] == SINGLE


async def test_only_the_first_unit_gets_each_documents_opening_chunks():
    cover = _hit("Cover page of the briefing")
    results = _hit(TEXT)
    retriever = FakeRetriever([results])
    retriever.opening_hits = [cover]
    fields = [
        _slot(1, slide=1),
        _slot(2, "Region", slide=2),
        _slot(3, "Owner", slide=3),
        _slot(4, "Status", slide=3),
    ]
    llm = FakeLLM(_reply(), NOT_FOUND, _reply())
    await map_fields(CTX, fields, DOCS, retriever, llm)
    assert llm.calls[0][0][0]["content"] == BATCH  # a lone slot on the opening unit is batched
    first = llm.calls[0][0][1]["content"]
    assert first.index(str(cover.chunk_id)) < first.index(str(results.chunk_id))
    assert (
        llm.calls[1][0][0]["content"] == SINGLE and cover.text not in llm.calls[1][0][1]["content"]
    )
    assert cover.text not in llm.calls[2][0][1]["content"]
    assert len(retriever.opening_calls) == 1 and retriever.opening_calls[0] == DOCS


async def test_a_slides_table_cells_get_their_own_batch_apart_from_its_text_slots():
    cells = [
        _slot(i, f"Cell {i}").model_copy(update={"anchor_id": f"pptx:s2:sp9:r{i}c1"})
        for i in (1, 2)
    ]
    fields = [_slot(7, "Lead"), *cells, _slot(8, "Region")]
    llm = FakeLLM(_reply(), _reply())
    await map_fields(CTX, fields, DOCS, FakeRetriever([_hit(TEXT)]), llm)
    specs = [[s["label"] for s in _slots_spec(llm, i)["slots"]] for i in range(2)]
    assert specs == [["Lead", "Region"], ["Cell 1", "Cell 2"]]


async def test_unscored_opening_chunks_survive_a_tight_token_budget(monkeypatch):
    from doc_intel.mapping import retriever as retriever_mod

    monkeypatch.setattr(retriever_mod, "count_tokens", lambda text, model: 3072)  # one fits
    cover = _hit("Account holder: Acme Corp").model_copy(update={"score": 0.0})  # retrieve()
    retriever = FakeRetriever([_hit(TEXT)])
    retriever.opening_hits = [cover]
    llm = FakeLLM(_reply())
    await map_fields(CTX, [_slot(1, slide=1)], DOCS, retriever, llm)
    assert str(cover.chunk_id) in llm.calls[0][0][1]["content"]


async def test_the_opening_chunks_alone_still_feed_the_cover_when_no_search_hits():
    cover = _hit("Prepared by: Acme Corp")
    retriever = FakeRetriever([])
    retriever.opening_hits = [cover]
    llm = FakeLLM(_reply(_entry(1, "Acme Corp", cover, "Prepared by: Acme Corp")))
    out = await map_fields(CTX, [_slot(1, slide=1)], DOCS, retriever, llm)
    assert (out["f1"].status, out["f1"].value) == ("populated", "Acme Corp")


async def test_batch_retrieval_searches_each_field_and_heading_then_widens_the_neighbors():
    h = _hit(TEXT)
    fields = [_slot(1, "Lead", context="Alpha card"), _slot(2, "Region", context="Beta card")]
    retriever = FakeRetriever([h])
    await map_fields(CTX, fields, DOCS, retriever, FakeLLM(_reply()))
    s = get_settings()
    chunks = min(
        hit_budget(s.llm_num_ctx, s.chunk_max_tokens),
        max(1, s.retrieval_context_tokens // s.chunk_max_tokens),
    )
    assert retriever.top_ns == [2, 2, 1, 1]
    assert [c[1] for c in retriever.calls][2:] == ["Alpha card", "Beta card"]
    assert retriever.budgets == [2 * chunks]


async def test_fit_chars_is_a_soft_hint_beside_the_constraints_never_in_them():
    h = _hit(TEXT)
    field = _slot(1, constraints=Constraints(max_chars=100), fit_chars=40)
    llm = FakeLLM(_reply(), '{"items": [], "not_found": true}')  # prose-sized: a synthesis try
    await map_fields(CTX, [field, _slot(2, "Region")], DOCS, FakeRetriever([h]), llm)
    slot = _slots_spec(llm)["slots"][0]
    assert slot["fit_chars"] == 40 and slot["constraints"] == {"max_chars": 100}
    assert "fit_chars" not in _slots_spec(llm)["slots"][1]


# ---- count rule: the number and its label are stated in one chunk ----------------------------


def _ev(chunk, quote):
    h = _hit(quote)
    return Evidence(
        document_id=h.document_id,
        chunk_id=chunk.chunk_id,
        filename="a.pdf",
        page_number=None,
        section_heading=None,
        quote=quote,
    )


def _count(context="Pipeline / No. of events"):
    return _slot(1, "No. of events", context=context, expected_type="number").model_copy(
        update={"write_mode": "replace_token", "constraints": Constraints(token="[# 1]")}
    )


def test_a_count_and_its_label_stated_in_two_chunks_break_the_rule():
    a, b = _hit("a"), _hit("b")
    evidence = [
        _ev(a, "There are 25 active sourcing events with a combined value of 64.8M."),
        _ev(b, "| Stage | Requiremen t | RFx | Evaluation |"),
    ]
    assert _rule_note(_count("Pipeline / Requirement"), "25", evidence)


def test_a_count_and_its_label_stated_in_one_chunk_pass():
    b = _hit("b")
    evidence = [_ev(b, "| Stage | Requiremen t | RFx |"), _ev(b, "| No. of events | 7 | 6 |")]
    assert _rule_note(_count("Pipeline / Requirement"), "7", evidence) is None


@pytest.mark.parametrize("value", ["64.8M", "12.5"])
def test_a_count_with_a_magnitude_or_decimal_is_not_a_whole_count(value):
    b = _hit("b")
    assert _rule_note(_count(), value, [_ev(b, f"No. of events {value} so far")])
