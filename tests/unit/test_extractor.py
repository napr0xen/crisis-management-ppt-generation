"""Field extraction call: model per type, hit fitting, prompt contents and size bound."""

import json
import uuid

import pytest

from doc_intel.embeddings.schemas import Hit
from doc_intel.llm.gateway import LLMError
from doc_intel.mapping.extractor import (
    EXTRACTION_MODELS,
    MAX_FIELD_HITS,
    bounded_field,
    extract,
    fit_hits,
)
from doc_intel.mapping.schemas import LLMExtraction
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
        ("table", LLMExtraction[list[list[str]]]),
    ],
)
async def test_schema_sent_to_the_llm_follows_expected_type(expected_type, model):
    llm = FakeLLM(EMPTY)
    out = await extract(llm, _field(expected_type), [_hit()])
    assert EXTRACTION_MODELS[expected_type] is model
    assert isinstance(out, model)
    assert llm.calls[0][1] == model.model_json_schema()


def test_fit_hits_keeps_the_first_max_field_hits():
    hits = [_hit() for _ in range(MAX_FIELD_HITS + 3)]
    assert fit_hits(hits) == hits[:MAX_FIELD_HITS]
    assert fit_hits(hits[:2]) == hits[:2]
    assert fit_hits([]) == []


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
    hits = fit_hits([_hit("w" * 2000) for _ in range(20)])
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
    await extract(llm, field, fit_hits([_hit("w" * 2000) for _ in range(20)]))
    assert sum(len(m["content"]) for m in llm.calls[0][0]) <= 22_000


async def test_llm_error_propagates():
    with pytest.raises(LLMError):
        await extract(FakeLLM("bad", "worse"), _field(), [_hit()])
