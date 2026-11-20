import json
import uuid

import pytest
from pydantic import ValidationError

from doc_intel.chat.edits import EditsIn, apply_edits, parse_edits
from doc_intel.chat.router import ChatMessage
from doc_intel.core.errors import ValidationFailed
from doc_intel.llm.gateway import LLMInvalidOutput
from doc_intel.mapping.schemas import Candidate, ConflictDetails, Evidence, FieldMappingResult
from doc_intel.templates.schemas import TemplateIR
from tests.fakes import FakeLLM

DOC = uuid.uuid4()


def _field(fid: str, etype: str = "text", write_mode: str = "replace_text", token=None, label=None):
    return {
        "field_id": fid,
        "anchor_id": f"a_{fid}",
        "section_path": ["Slide 1"],
        "label": label or fid.title(),
        "description": "d",
        "expected_type": etype,
        "required": True,
        "write_mode": write_mode,
        "constraints": {"token": token} if token else {},
    }


def _ir(*fields: dict) -> TemplateIR:
    return TemplateIR(template_id=uuid.uuid4(), version=1, format="pptx", fields=list(fields))


def _evidence(quote: str = "the quoted passage") -> Evidence:
    return Evidence(
        document_id=DOC,
        chunk_id=uuid.uuid4(),
        filename="a.pdf",
        page_number=1,
        section_heading=None,
        quote=quote,
    )


def _result(fid: str, status: str = "missing", **kw) -> FieldMappingResult:
    base = {
        "field_id": fid,
        "status": status,
        "value": None,
        "evidence": [],
        "confidence": 0.0,
        "missing_reason": "not_found_by_llm",
        "checked_documents": [DOC],
        "conflict_details": None,
        "provenance": "rag",
    }
    return FieldMappingResult(**(base | kw))


def _conflict(fid: str) -> FieldMappingResult:
    cands = [
        Candidate(value="first", evidence=[_evidence()]),
        Candidate(value="second", evidence=[_evidence("another passage")]),
    ]
    return _result(
        fid, "conflict", missing_reason=None, conflict_details=ConflictDetails(candidates=cands)
    )


def _ops(*ops: dict):
    return EditsIn(edits=list(ops)).edits


def _set(fid: str, value) -> dict:
    return {"op": "set_value", "field_id": fid, "value": value}


def test_set_value_effects_and_inputs_not_mutated() -> None:
    ir = _ir(_field("title"))
    results = {"title": _conflict("title")}
    before = results["title"].model_dump()
    out = apply_edits(ir, results, _ops(_set("title", "New title")))
    r = out["title"]
    assert (r.status, r.value, r.evidence, r.provenance) == ("populated", "New title", [], "user")
    assert (r.confidence, r.missing_reason, r.conflict_details) == (1.0, None, None)
    assert r.checked_documents == [DOC]
    assert results["title"].model_dump() == before


def test_choose_candidate_takes_value_and_evidence() -> None:
    ir = _ir(_field("title"), _field("amount", "currency"))
    results = {"title": _conflict("title"), "amount": _conflict("amount")}
    out = apply_edits(
        ir,
        results,
        _ops(
            {"op": "choose_candidate", "field_id": "title", "candidate_index": 1},
            {"op": "choose_candidate", "field_id": "amount", "candidate_index": 0},
        ),
    )
    r = out["title"]
    assert (r.status, r.value, r.provenance, r.confidence) == ("populated", "second", "rag", 0.8)
    assert r.evidence == results["title"].conflict_details.candidates[1].evidence
    assert r.conflict_details is None and r.missing_reason is None
    assert out["amount"].confidence == 0.9


def test_clear_effects() -> None:
    ir = _ir(_field("title"))
    out = apply_edits(
        ir,
        {"title": _result("title", "populated", value="x", evidence=[_evidence()])},
        _ops({"op": "clear", "field_id": "title"}),
    )
    r = out["title"]
    assert (r.status, r.value, r.evidence, r.missing_reason) == (
        "missing",
        None,
        [],
        "cleared_by_user",
    )
    assert (r.confidence, r.provenance, r.conflict_details) == (0.0, "user", None)


@pytest.mark.parametrize(
    ("etype", "value", "expected"),
    [
        ("currency", "1.829.914,84", 1829914.84),
        ("number", 7, 7),
        ("date", "30 junio 2022", "2022-06-30"),
        ("bullet_list", "one", ["one"]),
        ("paragraphs", ["a", "b"], ["a", "b"]),
        ("table", [["a", "b"]], [["a", "b"]]),
    ],
)
def test_set_value_coercion(etype, value, expected) -> None:
    ir = _ir(_field("f", etype))
    out = apply_edits(ir, {"f": _result("f")}, _ops(_set("f", value)))
    assert out["f"].value == expected


@pytest.mark.parametrize(
    ("etype", "value"),
    [
        ("number", "abc"),
        ("date", "not a date"),
        ("table", "plain string"),
        ("text", "   "),
        ("text", ["a"]),
        ("bullet_list", []),
        ("paragraphs", [["a"]]),
    ],
)
def test_bad_set_values_are_422_and_leave_results_unchanged(etype, value) -> None:
    ir = _ir(_field("f", etype))
    results = {"f": _result("f")}
    before = results["f"].model_dump()
    with pytest.raises(ValidationFailed) as exc:
        apply_edits(ir, results, _ops(_set("f", value)))
    assert "edits[0]" in exc.value.message
    assert str(value) not in exc.value.message or value == "   "
    assert results["f"].model_dump() == before


def test_unknown_duplicate_and_wrong_state_are_422_and_nothing_applies() -> None:
    ir = _ir(_field("a"), _field("b"))
    results = {"a": _result("a"), "b": _result("b")}
    bad = [
        [_set("a", "ok"), _set("zzz", "x")],
        [_set("a", "ok"), _set("a", "again")],
        [{"op": "choose_candidate", "field_id": "a", "candidate_index": 0}],
        [{"op": "clear", "field_id": "zzz"}],
    ]
    for ops in bad:
        with pytest.raises(ValidationFailed) as exc:
            apply_edits(ir, results, _ops(*ops))
        assert "zzz" not in exc.value.message
    assert results["a"].status == "missing"


def test_candidate_index_out_of_range_and_negative() -> None:
    ir = _ir(_field("a"))
    results = {"a": _conflict("a")}
    with pytest.raises(ValidationFailed):
        apply_edits(
            ir, results, _ops({"op": "choose_candidate", "field_id": "a", "candidate_index": 2})
        )
    with pytest.raises(ValidationError):
        _ops({"op": "choose_candidate", "field_id": "a", "candidate_index": -1})


def test_request_model_rejects_unknown_ops_extras_and_empty() -> None:
    for edits in (
        [],
        [{"op": "delete", "field_id": "a"}],
        [{"op": "clear", "field_id": "a", "x": 1}],
    ):
        with pytest.raises(ValidationError):
            EditsIn(edits=edits)
    with pytest.raises(ValidationError):
        EditsIn(edits=[{"op": "clear", "field_id": "a"}] * 51)


def test_repeated_token_siblings_share_the_edit() -> None:
    ir = _ir(
        _field("s1", write_mode="replace_token", token="[X]", label="Name"),
        _field("s2", write_mode="replace_token", token="[X]", label="name"),
        _field("s3", write_mode="replace_token", token="[Y]", label="Name"),
        _field("s4", label="Name"),
    )
    results = {f.field_id: _result(f.field_id) for f in ir.fields}
    out = apply_edits(ir, results, _ops(_set("s1", "Ann")))
    assert out["s2"].value == "Ann" and out["s2"].field_id == "s2"
    assert out["s2"].provenance == "user"
    assert out["s3"].status == out["s4"].status == "missing"


def test_sibling_named_in_the_same_request_is_a_duplicate() -> None:
    ir = _ir(
        _field("s1", write_mode="replace_token", token="[X]", label="Name"),
        _field("s2", write_mode="replace_token", token="[X]", label="Name"),
    )
    results = {f.field_id: _result(f.field_id) for f in ir.fields}
    with pytest.raises(ValidationFailed):
        apply_edits(ir, results, _ops(_set("s1", "Ann"), _set("s2", "Bob")))


# ---------------------------------------------------------------- parse_edits


def _msgs(text: str) -> list[ChatMessage]:
    return [ChatMessage(role="user", content=text)]


def _llm(*edits: dict) -> FakeLLM:
    return FakeLLM(json.dumps({"edits": list(edits)}))


async def test_parse_edits_maps_reply_and_prompts_with_field_list() -> None:
    ir = _ir(_field("title"), _field("who"), _field("when"))
    results = {
        "title": _conflict("title"),
        "who": _result("who", "populated", value="Bob", provenance="user"),
        "when": _result("when"),
    }
    llm = _llm(
        {"op": "set_value", "field_id": "who", "value": "Ann"},
        {"op": "choose_candidate", "field_id": "title", "candidate_index": 1},
        {"op": "clear", "field_id": "when"},
    )
    ops = await parse_edits(llm, _msgs("set who to Ann, use the first title"), ir, results)
    assert [o.op for o in ops] == ["set_value", "choose_candidate", "clear"]
    assert ops[0].value == "Ann" and ops[1].candidate_index == 0
    user = llm.calls[0][0][-1]["content"]
    assert (
        "who | Who | populated | Bob" in user
        and "1: first (a.pdf)" in user
        and "2: second (a.pdf)" in user
    )
    assert "set who to Ann" in user
    assert len(llm.calls) == 1


async def test_parse_edits_rejects_a_value_not_written_by_the_user() -> None:
    ir = _ir(_field("who"))
    llm = _llm({"op": "set_value", "field_id": "who", "value": "Mallory"})
    with pytest.raises(ValidationFailed) as exc:
        await parse_edits(llm, _msgs("change the name"), ir, {"who": _result("who")})
    assert "Mallory" not in exc.value.message


@pytest.mark.parametrize(
    "edit",
    [
        {"op": "set_value", "field_id": "who"},
        {"op": "choose_candidate", "field_id": "who"},
    ],
)
async def test_parse_edits_op_missing_its_argument_is_422(edit) -> None:
    with pytest.raises(ValidationFailed):
        await parse_edits(_llm(edit), _msgs("x"), _ir(_field("who")), {"who": _result("who")})


async def test_parse_edits_empty_reply_and_invalid_output() -> None:
    ir, results = _ir(_field("who")), {"who": _result("who")}
    assert await parse_edits(_llm(), _msgs("hmm"), ir, results) == []
    with pytest.raises(LLMInvalidOutput):
        await parse_edits(FakeLLM("nope", "nope"), _msgs("x"), ir, results)


async def test_parse_edits_lists_a_token_group_once_and_drops_repeated_ops() -> None:
    ir = _ir(
        _field("s1", write_mode="replace_token", token="[X]", label="Name"),
        _field("s2", write_mode="replace_token", token="[X]", label="Name"),
        _field("n", "number"),
    )
    results = {
        "s1": _result("s1"),
        "s2": _result("s2"),
        "n": _result("n", "populated", value=0, evidence=[_evidence()], missing_reason=None),
    }
    llm = _llm(
        {"op": "set_value", "field_id": "s1", "value": "Ann"},
        {"op": "set_value", "field_id": "s2", "value": "Ann"},
        {"op": "clear", "field_id": "n"},
        {"op": "clear", "field_id": "n"},
    )
    ops = await parse_edits(llm, _msgs("set the name to Ann and clear n"), ir, results)
    assert [(o.op, o.field_id) for o in ops] == [("set_value", "s1"), ("clear", "n")]
    user = llm.calls[0][0][-1]["content"]
    assert "s1 | Name" in user and "s2 |" not in user
    assert "n | N | populated | 0" in user
    out = apply_edits(ir, results, ops)
    assert out["s2"].value == "Ann"


@pytest.mark.parametrize("value", ["1200", "12,50"])
async def test_parse_edits_rejects_a_cut_down_value(value) -> None:
    ir = _ir(_field("n", "number"))
    llm = _llm({"op": "set_value", "field_id": "n", "value": value})
    with pytest.raises(ValidationFailed):
        await parse_edits(llm, _msgs("set n to 12000 or 12,500"), ir, {"n": _result("n")})


async def test_parse_edits_candidate_numbers_start_at_one() -> None:
    ir, results = _ir(_field("title")), {"title": _conflict("title")}
    llm = _llm({"op": "choose_candidate", "field_id": "title", "candidate_index": 2})
    ops = await parse_edits(llm, _msgs("use the second value"), ir, results)
    assert ops[0].candidate_index == 1
    assert apply_edits(ir, results, ops)["title"].value == "second"
    llm = _llm({"op": "choose_candidate", "field_id": "title", "candidate_index": 0})
    with pytest.raises(ValidationFailed):
        await parse_edits(llm, _msgs("use value 0"), ir, results)


async def test_oversized_field_ids_and_values_are_422() -> None:
    with pytest.raises(ValidationError):
        EditsIn(edits=[{"op": "clear", "field_id": "x" * 65}])
    ir, results = _ir(_field("who")), {"who": _result("who")}
    with pytest.raises(ValidationFailed):
        apply_edits(ir, results, _ops(_set("who", "a" * 10_001)))
    llm = _llm({"op": "clear", "field_id": "x" * 65})
    with pytest.raises(ValidationFailed):
        await parse_edits(llm, _msgs("clear it"), ir, results)
