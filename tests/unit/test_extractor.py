"""Field extraction call: model per type, hit fitting, prompt contents and size bound."""

import json
import uuid

import pytest

from doc_intel.embeddings.schemas import Hit
from doc_intel.llm.gateway import LLMError, prompt_tokens
from doc_intel.llm.prompts import load_prompt
from doc_intel.mapping.extractor import (
    EXTRACTION_MODELS,
    MAX_PRIMARY_HITS,
    PROMPT_OVERHEAD_TOKENS,
    bounded_field,
    check_candidates,
    check_roles,
    extract,
    extract_batch,
    hit_budget,
)
from doc_intel.mapping.schemas import Evidence, LLMCandidateCheck, LLMExtraction
from doc_intel.templates.schemas import Constraints, IRField
from tests.fakes import FakeLLM

EMPTY = '{"candidates": [], "not_found": true, "incomplete": false}'


def _hit(text="Approximately 12,000 customers were affected."):
    return Hit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="ops.pdf",
        page_number=3,
        section_heading="Impact",
        element_type="text",
        text=text,
        score=0.5,
    )


def _field(expected_type="text", **kw):
    base = {
        "field_id": "f",
        "anchor_id": "a",
        "section_path": ["Slide 1", "Impact"],
        "label": "Customers affected",
        "description": "How many customers lost power",
        "expected_type": expected_type,
        "required": True,
        "write_mode": "replace_text",
        "constraints": Constraints(),
    }
    return IRField(**{**base, **kw})


@pytest.mark.parametrize(
    "expected_type, model",
    [
        ("text", LLMExtraction[str]),
        ("number", LLMExtraction[str]),
        ("currency", LLMExtraction[str]),
        ("percent", LLMExtraction[str]),
        ("date", LLMExtraction[str]),
        ("paragraphs", LLMExtraction[list[str]]),
        ("bullet_list", LLMExtraction[list[str]]),
    ],
)
async def test_schema_sent_to_the_llm_follows_expected_type(expected_type, model):
    llm = FakeLLM(EMPTY)
    out = await extract(llm, _field(expected_type), [_hit()])
    assert EXTRACTION_MODELS[expected_type] is model
    assert isinstance(out, model)
    assert llm.calls[0][1] == model.model_json_schema()


def test_prompt_tokens_scale_with_the_window():
    assert prompt_tokens(8192) == 6144
    assert prompt_tokens(32768) == 16384
    assert prompt_tokens(4096) == 6144  # never below the 8k-era budget


@pytest.mark.parametrize(
    "num_ctx, chunk_tokens, budget",
    [(8192, 512, 6), (32768, 512, 26), (20480, 512, 14), (8192, 100000, 1)],
)
def test_hit_budget(num_ctx, chunk_tokens, budget):
    assert hit_budget(num_ctx, chunk_tokens) == budget
    assert PROMPT_OVERHEAD_TOKENS == 3072 and MAX_PRIMARY_HITS == 12


async def test_prompt_has_the_spec_and_every_chunk_header():
    hits = [_hit("first chunk text"), _hit("second chunk text")]
    field = _field(constraints=Constraints(max_chars=40))
    llm = FakeLLM(EMPTY)
    await extract(llm, field, hits)
    system, user = llm.calls[0][0]
    assert system["role"] == "system"
    assert "never guess" in system["content"]
    for h in hits:
        assert f"[{h.chunk_id} | ops.pdf | p.3 | Impact]\n{h.text}" in user["content"]
    spec = json.loads(user["content"].split("Field:\n")[1].split("\n\nChunks:")[0])
    assert spec == {
        "label": "Customers affected",
        "description": "How many customers lost power",
        "section_path": ["Slide 1", "Impact"],
        "expected_type": "text",
        "constraints": {"max_chars": 40},
    }


async def test_worst_case_prompt_stays_under_the_char_bound():
    # Assumption: ~4 chars/token, so 22,000 chars is ~5.5k tokens of LLM_NUM_CTX=8192.
    hits = [_hit("w" * 2000) for _ in range(hit_budget(8192, 512))]
    field = _field(
        label="l" * 120,
        description="d" * 500,
        section_path=["s" * 5000] * 6,
        constraints=Constraints(columns=["c" * 100] * 6),
    )
    llm = FakeLLM("not json", "still not json")
    with pytest.raises(LLMError):
        await extract(llm, field, hits)
    for messages, _ in llm.calls:  # first attempt and the retry
        assert sum(len(m["content"]) for m in messages) <= 22_000


async def test_worst_case_prompt_at_32k_stays_under_the_char_bound():
    # 4 chars/token x prompt_tokens(32768)
    hits = [_hit("w" * 2000) for _ in range(hit_budget(32768, 512))]
    field = _field(label="l" * 120, description="d" * 500, section_path=["s" * 5000] * 6)
    field = field.model_copy(update={"context": "c" * 5000})
    llm = FakeLLM("not json", "still not json")
    with pytest.raises(LLMError):
        await extract(llm, field, hits)
    for messages, _ in llm.calls:
        assert sum(len(m["content"]) for m in messages) <= 4 * prompt_tokens(32768)


@pytest.mark.parametrize("context", ["Emergency department", "Wheat yield"])
async def test_spec_carries_context_bounded_to_120_chars(context):
    field = _field().model_copy(update={"context": context})
    llm = FakeLLM(EMPTY)
    await extract(llm, field, [_hit()])
    spec = json.loads(llm.calls[0][0][1]["content"].split("Field:\n")[1].split("\n\nChunks:")[0])
    assert spec["context"] == context
    assert len(bounded_field(field.model_copy(update={"context": "c" * 500})).context) == 120
    assert "context" in load_prompt("field_extract")


async def test_pathological_field_spec_is_bounded():
    field = _field(
        section_path=["p" * 5000] * 1000,
        constraints=Constraints(
            columns=["c" * 5000] * 1000,
            allowed_values=["v" * 5000] * 1000,
            unit="u" * 5000,
            date_format="d" * 5000,
            token="t" * 5000,
        ),
    )
    b = bounded_field(field)
    c = b.constraints
    assert len(b.section_path) == 4 and all(len(p) == 120 for p in b.section_path)
    assert len(c.columns) == 30 and all(len(x) == 80 for x in c.columns)
    assert len(c.allowed_values) == 30 and all(len(x) == 80 for x in c.allowed_values)
    assert (len(c.unit), len(c.date_format), len(c.token)) == (80, 80, 80)
    llm = FakeLLM(EMPTY)
    await extract(llm, field, [_hit("w" * 2000) for _ in range(hit_budget(8192, 512))])
    assert sum(len(m["content"]) for m in llm.calls[0][0]) <= 22_500


async def test_llm_error_propagates():
    with pytest.raises(LLMError):
        await extract(FakeLLM("bad", "worse"), _field(), [_hit()])


# ---- check_candidates: all-or-nothing check that the values belong to the slot -----------------


def _ext(*cands, not_found=False, incomplete=False):
    return LLMExtraction[str].model_validate(
        {
            "candidates": [
                {"value": v, "quotes": [{"chunk_id": str(h.chunk_id), "quote": q} for h, q in qs]}
                for v, *qs in cands
            ],
            "not_found": not_found,
            "incomplete": incomplete,
        }
    )


def _checks(*flags):
    return json.dumps({"checks": [{"about": "x", "match": f} for f in flags]})


def _user(llm, call=0):
    return json.loads(llm.calls[call][0][1]["content"])


DOMAINS = [
    ("Radiology", "Median wait time", "Emergency department: median wait was 47 minutes.", "47"),
    ("Maize", "Average yield", "Wheat plots yielded 6.8 tonnes per hectare.", "6.8"),
    ("Berlin office", "Headcount", "Headcount at quarter end was 120 at the Osaka office.", "120"),
]


@pytest.mark.parametrize("context, label, quote, value", DOMAINS)
async def test_check_candidates_makes_no_call_without_candidates(context, label, quote, value):
    h = _hit(quote)
    field = _field(label=label, context=context)
    for ext in (_ext(not_found=True), _ext(), _ext((value, (h, quote)), not_found=True)):
        llm = FakeLLM()
        assert await check_candidates(llm, field, [h], ext) is ext
        assert llm.calls == []


@pytest.mark.parametrize("context, label, quote, value", DOMAINS)
async def test_check_candidates_no_match_is_not_found_with_or_without_context(
    context, label, quote, value
):
    h = _hit(quote)
    field = _field(label=label, context=context)
    ext = _ext((value, (h, quote)), incomplete=True)
    for reply in (_checks(False), _checks(), '{"checks": []}'):
        out = await check_candidates(FakeLLM(reply), field, [h], ext)
        assert (out.candidates, out.not_found, out.incomplete) == ([], True, True)


@pytest.mark.parametrize("context, label, quote, value", DOMAINS)
async def test_check_candidates_any_match_keeps_every_candidate(context, label, quote, value):
    h1, h2 = _hit(quote), _hit("Another heading: 5 units.")
    ext = _ext((value, (h1, quote)), ("5", (h2, "Another heading: 5 units.")))
    field = _field(label=label, context=context)
    for flags in ((True, False), (False, True), (True, True)):
        llm = FakeLLM(_checks(*flags))
        out = await check_candidates(llm, field, [h1, h2], ext)
        assert out is ext  # never drops one candidate, so never picks between conflicting values
    assert llm.calls[0][1] == LLMCandidateCheck.model_json_schema()
    user = _user(llm)
    assert user.get("context") == context
    assert user["label"] == label
    assert [q["n"] for q in user["quotes"]] == [1, 2] and user["quotes"][0]["value"] == value
    assert user["quotes"][0]["section"] == "Impact" and "before" not in user["quotes"][0]


@pytest.mark.parametrize(
    "label, quote, value",
    [
        ("Berlin headcount", "Headcount at quarter end was 120 at the Osaka office.", "120"),
        ("Plot area", "The orchard covers 14 hectares of the northern estate.", "14"),
    ],
)
async def test_check_candidates_makes_no_call_for_a_field_without_context(label, quote, value):
    h = _hit(quote)
    ext = _ext((value, (h, quote)))
    llm = FakeLLM()
    assert await check_candidates(llm, _field(label=label), [h], ext) is ext
    assert llm.calls == []


@pytest.mark.parametrize(
    "context, others",
    [("Radiology", ["Emergency department", "Day surgery"]), ("Maize", ["Wheat", "Barley"])],
)
async def test_check_candidates_sends_other_headings_only_when_given(context, others):
    h = _hit("Wheat yielded 6.8 tonnes.")
    ext = _ext(("6.8", (h, "Wheat yielded 6.8")))
    llm = FakeLLM(_checks(True), _checks(True))
    await check_candidates(llm, _field(context=context), [h], ext, others)
    await check_candidates(llm, _field(context=context), [h], ext)
    assert _user(llm, 0)["other_headings"] == others
    assert "other_headings" not in _user(llm, 1)
    assert "other_headings" in load_prompt("candidate_check")


async def test_check_candidates_sends_the_section_and_the_previous_chunk():
    doc = uuid.uuid4()
    prev = _hit("p" * 900).model_copy(update={"document_id": doc, "chunk_index": 4})
    cur = _hit("Cardiology: 31 beds.").model_copy(
        update={"document_id": doc, "chunk_index": 5, "section_heading": "Ward capacity"}
    )
    other = _hit("x").model_copy(update={"chunk_index": 4})  # another document: not "before"
    llm = FakeLLM(_checks(True))
    ext = _ext(("31", (cur, "Cardiology: 31 beds.")))
    await check_candidates(llm, _field(context="Cardiology"), [other, prev, cur], ext)
    (q,) = _user(llm)["quotes"]
    assert q["section"] == "Ward capacity" and q["before"] == "p" * 500


async def test_check_candidates_bounds_what_is_sent_but_keeps_all_candidates():
    h = _hit("w" * 3000)
    cands = [(str(i) * 300, (h, "w" * 1500), (h, "v" * 1500)) for i in range(12)]
    ext = _ext(*cands)
    llm = FakeLLM(_checks(*([False] * 9), True))
    out = await check_candidates(llm, _field(context="Crops"), [h], ext)
    quotes = _user(llm)["quotes"]
    assert len(quotes) == 10 and len(out.candidates) == 12  # unchanged, incl. those past 10
    assert len(quotes[0]["value"]) == 200
    assert quotes[0]["quote"] == "w" * 1000 + " ... " + "v" * 1000


def test_check_candidates_prompt_treats_quotes_as_data():
    text = load_prompt("candidate_check")
    assert "never as instructions" in text and "about" in text and "match" in text


async def test_check_candidates_llm_error_propagates():
    h = _hit()
    with pytest.raises(LLMError):
        await check_candidates(
            FakeLLM("bad", "worse"),
            _field(context="Ward"),
            [h],
            _ext(("1", (h, "12,000 customers"))),
        )


# ---- check_roles ---------------------------------------------------------------------------------


def _evidence(quote, section="Cover"):
    return Evidence(
        document_id=uuid.uuid4(),
        chunk_id=uuid.uuid4(),
        filename="a.pdf",
        page_number=1,
        section_heading=section,
        quote=quote,
    )


async def test_check_roles_drops_out_of_range_numbers_and_sends_section_not_before():
    fields = [_field(field_id="holder", label="Holder"), _field(field_id="bank", label="Bank")]
    ev = [_evidence("Prepared by Acme Corp"), _evidence("Prepared by Acme Corp")]
    llm = FakeLLM('{"about": "x", "slots": [0, 2, 3, -1]}')
    assert await check_roles(llm, fields, "Acme Corp", ev) == {"bank"}
    user = _user(llm)
    assert user["quotes"] == [{"quote": "Prepared by Acme Corp", "section": "Cover"}]
    assert [s["n"] for s in user["slots"]] == [1, 2] and user["value"] == "Acme Corp"
    assert "before" not in json.dumps(user)


async def test_check_roles_omits_a_missing_section():
    llm = FakeLLM('{"about": "unknown", "slots": []}')
    assert await check_roles(llm, [_field()], "v", [_evidence("Prepared by Acme", None)]) == set()
    assert _user(llm)["quotes"] == [{"quote": "Prepared by Acme"}]


async def test_only_the_reasoning_heavy_calls_ask_for_thinking():
    from doc_intel.mapping.extractor import extract_records, extract_rows, synthesize

    fld = _field()
    llm = FakeLLM('{"rows": [], "not_found": true}')
    await extract_rows(llm, fld, [_hit()])
    llm_r = FakeLLM('{"records": [], "not_found": true}')
    await extract_records(llm_r, [[fld]], [_hit()])
    llm_m = FakeLLM('{"records": [], "not_found": true}')
    await extract_records(llm_m, [[fld], [fld]], [_hit()])
    llm_b = FakeLLM('{"slots": []}')
    await extract_batch(llm_b, [fld], [_hit()])
    llm_s = FakeLLM(
        '{"items": [{"value": "a", "quotes": []}, {"value": "b", "quotes": []}],'
        ' "not_found": false}',
        '{"items": [], "not_found": true}',
    )
    await synthesize(llm_s, _field("bullet_list", constraints=Constraints(max_items=1)), [_hit()])
    llm_c = FakeLLM('{"about": "x", "slots": []}')
    await check_roles(llm_c, [fld], "v", [_evidence("Prepared by Acme")])
    llm_e = FakeLLM(EMPTY)
    await extract(llm_e, fld, [_hit()])
    assert llm.thinks == [True] and llm_r.thinks == [False] and llm_m.thinks == [True]
    assert llm_s.thinks == [False, False] and llm_c.thinks == [False]
    assert llm_e.thinks == [False] and llm_b.thinks == [False]


# ---- every value is bound to its slot / column label ---------------------------------------------


def _cells_reply(key, cells):
    value = [{"slot": s, "value": v} for s, v in cells]
    return json.dumps({key: [{"n": 1, "value": value, "quotes": []}], "not_found": False})


def _slots():
    return [_field(field_id=f"f{i}", label=label) for i, label in enumerate(("Name", "Count"))]


async def test_record_cells_are_realigned_to_spec_order_by_label():
    from doc_intel.mapping.extractor import extract_records

    reply = _cells_reply("records", [(" COUNT ", "14"), ("name", "North depot")])
    out = await extract_records(FakeLLM(reply), [_slots()], [_hit()])
    assert out.records[0].cells() == ["North depot", "14"]


@pytest.mark.parametrize(
    "slots",
    [[("Count", "14"), ("Other", "x")], [("Name", "14"), ("Name", "x")]],
    ids=["unknown", "dup"],
)
async def test_record_cells_with_an_unknown_or_repeated_label_keep_reply_order(slots):
    from doc_intel.mapping.extractor import extract_records

    out = await extract_records(FakeLLM(_cells_reply("records", slots)), [_slots()], [_hit()])
    assert out.records[0].cells() == [v for _, v in slots]


async def test_row_cells_are_realigned_to_column_order_by_label():
    from doc_intel.mapping.extractor import extract_rows

    table = _field(
        "table", write_mode="fill_table_rows", constraints=Constraints(columns=["A", "B"])
    )
    reply = _cells_reply("rows", [("B", "2"), ("A", "1")]).replace('"n": 1, ', "")
    out = await extract_rows(FakeLLM(reply), table, [_hit()])
    assert out.rows[0].cells() == ["1", "2"]


def test_the_reply_schemas_bind_each_value_to_a_slot_key():
    from doc_intel.mapping.schemas import LLMRecords, LLMRows

    for model in (LLMRecords, LLMRows):
        assert '"slot"' in json.dumps(model.model_json_schema())


# ---- batched slot values and the soft fit_chars hint -----------------------------------


def _batch_spec(llm):
    user = llm.calls[0][0][1]["content"]
    return json.loads(user.split("Slots:\n", 1)[1].split("\n\nChunks:", 1)[0])


async def test_extract_batch_numbers_the_slots_and_keeps_fit_chars_out_of_constraints():
    token = _field(
        label="Owner",
        context="Alpha card",
        write_mode="replace_token",
        constraints=Constraints(token="[Owner]", max_chars=90),
        fit_chars=40,
    )
    plain = _field(label="Region")
    llm = FakeLLM('{"slots": []}')
    out = await extract_batch(llm, [token, plain], [_hit()])
    spec = _batch_spec(llm)
    assert out.slots == [] and spec["section_path"] == ["Slide 1", "Impact"]
    first, second = spec["slots"]
    assert first["n"] == 1 and first["context"] == "Alpha card" and first["placeholder"] == "Owner"
    assert first["fit_chars"] == 40 and first["constraints"] == {
        "max_chars": 90,
        "token": "[Owner]",
    }
    assert second["n"] == 2 and "fit_chars" not in second and "constraints" not in second
    assert llm.calls[0][0][0]["content"] == load_prompt("slot_values")


async def test_extract_batch_bounds_template_strings():
    long = _field(label="x" * 120, context="y" * 500)
    llm = FakeLLM('{"slots": []}')
    await extract_batch(llm, [long], [_hit()])
    slot = _batch_spec(llm)["slots"][0]
    assert len(slot["context"]) <= 120


async def test_the_single_field_and_records_specs_carry_fit_chars():
    fld = _field(fit_chars=55)
    llm = FakeLLM(EMPTY)
    await extract(llm, fld, [_hit()])
    assert (
        json.loads(llm.calls[0][0][1]["content"].split("\n\n", 1)[0].split("Field:\n")[1])[
            "fit_chars"
        ]
        == 55
    )
    from doc_intel.mapping.extractor import _slot_spec

    assert _slot_spec(fld)["fit_chars"] == 55 and "fit_chars" not in _slot_spec(_field())


@pytest.mark.parametrize("name", ["slot_values", "field_extract", "slot_records"])
def test_the_prompts_describe_the_fit_chars_hint(name):
    text = load_prompt(name)
    assert "fit_chars" in text and "never cut, join or paraphrase a quote" in text
