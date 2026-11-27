"""Universal slot types: inference at IR build time, constraint injection, verifier gate."""

import json

import pytest

from doc_intel.core.config import get_settings
from doc_intel.mapping.engine import _rejection, map_field
from doc_intel.mapping.extractor import _batch_spec, extract
from doc_intel.mapping.schemas import Evidence
from doc_intel.templates.ir_builder import SLOT_FORMAT, build_ir, slot_type
from doc_intel.templates.schemas import AnchorMap
from tests.fakes import FakeLLM
from tests.unit.test_ir_builder import TID, _anchor, _field, _reply
from tests.unit.test_mapping_engine import CTX, DOCS, FakeRetriever, _cand, _hit
from tests.unit.test_mapping_engine import _field as _slot

HIT = _hit("Highest outflow was in March 2024, when $1,250.00 left the account.")


def _typed(expected_type="text", kind=None):
    return _slot(expected_type).model_copy(update={"slot_type": kind})


@pytest.mark.parametrize(
    "expected, llm_choice, want",
    [
        ("text", "date_period", "date_period"),
        ("paragraphs", "entity_name", "entity_name"),
        ("bullet_list", "boolean", "boolean"),
        ("text", "currency", "free_text"),  # a typed kind is not a text slot's to choose
        ("text", None, "free_text"),
        ("currency", "entity_name", "currency"),
        ("percent", None, "percentage"),
        ("number", "number_count", "number_count"),
        # any other number (a rating, a ratio) is not told it is a whole count
        ("number", "free_text", None),
        ("number", None, None),
        ("date", None, "date_period"),
        ("table", "entity_name", None),
        ("chart", None, None),
    ],
)
def test_slot_type_inference(expected, llm_choice, want):
    assert slot_type(_typed(expected, llm_choice)) == want


async def test_build_ir_sets_slot_type():
    amap = AnchorMap(format="pptx", anchors=[_anchor("a1", tokens=["{{t}}"])])
    draft = _field("a", "a1", "replace_token", token="{{t}}").model_copy(
        update={"slot_type": "date_period"}
    )
    ir = await build_ir(FakeLLM(_reply(draft)), amap, TID)
    assert ir.fields[0].slot_type == "date_period"
    plain = _field("a", "a1", "replace_token", token="{{t}}")
    ir = await build_ir(FakeLLM(_reply(plain)), amap, TID)
    assert ir.fields[0].slot_type == "free_text"


async def test_extract_prompt_carries_the_constraint_line():
    reply = json.dumps({"candidates": [], "not_found": True, "incomplete": False})
    llm = FakeLLM(reply, reply)
    await extract(llm, _typed("text", "date_period"), [HIT])
    await extract(llm, _typed("currency"), [HIT])  # an old IR: derived from expected_type
    first, second = (c[0][1]["content"] for c in llm.calls)
    assert SLOT_FORMAT["date_period"] in first and '"slot_type":"date_period"' in first
    assert SLOT_FORMAT["currency"] in second


def test_batch_spec_carries_the_constraint_line():
    (slot,) = _batch_spec([_typed("text", "entity_name")])["slots"]
    assert slot["slot_type"] == "entity_name" and slot["format"] == SLOT_FORMAT["entity_name"]
    (free,) = _batch_spec([_typed("text")])["slots"]
    assert "format" not in free


def _reject(kind, value):
    evidence = [
        Evidence(
            document_id=HIT.document_id,
            chunk_id=HIT.chunk_id,
            filename=HIT.filename,
            page_number=HIT.page_number,
            section_heading=HIT.section_heading,
            quote=HIT.text[:30],
        )
    ]
    return _rejection(_typed("text", kind), [HIT], value, evidence)


@pytest.mark.parametrize(
    "kind, value",
    [
        ("date_period", "$1,250.00"),
        ("date_period", "1,250.00"),
        ("date_period", "125"),
        ("entity_name", "$1,250.00"),
        ("entity_name", "1,250.00"),
    ],
)
def test_amount_in_a_text_slot_is_rejected(kind, value):
    code, note = _reject(kind, value)
    assert code == "slot_type_mismatch"
    assert value in note and SLOT_FORMAT[kind] in note


@pytest.mark.parametrize(
    "kind, value",
    [
        ("date_period", "March 2024"),
        ("date_period", "2024-03-31"),
        ("date_period", "2024"),
        ("entity_name", "Acme Ltd"),
        ("entity_name", "Route 66 Diner"),
        ("entity_name", "4410 2231"),  # an id or account number is a name, not an amount
        ("free_text", "$1,250.00"),
    ],
)
def test_matching_values_pass_the_slot_type_gate(kind, value):
    result = _reject(kind, value)
    assert result is None or result[0] != "slot_type_mismatch"


@pytest.fixture
def escalation(monkeypatch):
    monkeypatch.setenv("THINK_ON_ESCALATION", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


async def test_amount_for_a_date_period_slot_is_corrected_then_populated(escalation):
    field = _typed("text", "date_period")
    wrong = json.dumps(
        {
            "candidates": [_cand("$1,250.00", (HIT, "$1,250.00 left the account"))],
            "not_found": False,
            "incomplete": False,
        }
    )
    right = json.dumps(
        {
            "candidates": [_cand("March 2024", (HIT, "Highest outflow was in March 2024"))],
            "not_found": False,
            "incomplete": False,
        }
    )
    llm = FakeLLM(wrong, right)
    result = await map_field(CTX, field, DOCS, FakeRetriever([HIT]), llm)
    assert result.status == "populated" and result.value == "March 2024"
    assert SLOT_FORMAT["date_period"] in llm.calls[1][0][-1]["content"]
