"""FieldMappingResult status invariants (system-design § 6)."""

import uuid

import pytest
from pydantic import ValidationError

from doc_intel.mapping.schemas import Candidate, ConflictDetails, Evidence, FieldMappingResult

EV = Evidence(
    document_id=uuid.uuid4(),
    chunk_id=uuid.uuid4(),
    filename="a.pdf",
    page_number=None,
    section_heading=None,
    quote="twelve thousand customers",
)


def _make(**kw):
    base = {
        "field_id": "f",
        "status": "populated",
        "value": "x",
        "evidence": [EV],
        "confidence": 0.8,
        "missing_reason": None,
        "checked_documents": [],
        "conflict_details": None,
        "provenance": "rag",
    }
    return FieldMappingResult(**{**base, **kw})


CONFLICT = ConflictDetails(candidates=[Candidate(value="a", evidence=[EV])] * 2)


@pytest.mark.parametrize(
    "kw",
    [
        {"evidence": []},  # populated (rag) without evidence
        {"value": None},  # populated without value
        {"status": "partial", "evidence": []},
        {"status": "partial", "value": None},
        {"status": "missing", "value": "x", "evidence": [], "missing_reason": "llm_error"},
        {"status": "missing", "value": None, "evidence": [], "missing_reason": None},
        {"status": "missing", "value": None, "evidence": [EV], "missing_reason": "llm_error"},
        {"status": "conflict", "value": None, "evidence": [], "conflict_details": None},
        {"status": "conflict", "value": "x", "evidence": [], "conflict_details": CONFLICT},
    ],
)
def test_invalid_shapes_are_rejected(kw):
    with pytest.raises(ValidationError):
        _make(**kw)


def test_conflict_needs_two_candidates():
    with pytest.raises(ValidationError):
        ConflictDetails(candidates=[Candidate(value="a", evidence=[EV])])


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"status": "partial", "missing_reason": "insufficient_evidence"},
        {"status": "missing", "value": None, "evidence": [], "missing_reason": "llm_error"},
        {"status": "conflict", "value": None, "evidence": [], "conflict_details": CONFLICT},
        {"provenance": "user", "evidence": []},  # user-set values need no evidence
    ],
)
def test_valid_shapes_are_accepted(kw):
    _make(**kw)


def test_evidence_item_is_optional_and_round_trips():
    old = EV.model_dump(mode="json", exclude={"item"})
    assert Evidence.model_validate(old).item is None
    item = Evidence.model_validate({**old, "item": 2})
    assert Evidence.model_validate_json(item.model_dump_json()).item == 2
    with pytest.raises(ValidationError):
        Evidence.model_validate({**old, "item": -1})


def test_quote_strips_provenance_ids_and_keeps_other_brackets():
    from doc_intel.mapping.schemas import LLMQuote

    q = lambda s: LLMQuote(chunk_id="c", quote=s).quote  # noqa: E731
    assert q("[P1-T2-R04] | Closing | 10 |") == "| Closing | 10 |"
    assert q("[T2-R04] x") == "x"
    assert q("[P12-T3-R04] text") == "text"
    assert q("see [B12] and [P1-B03] here") == "see [B12] and [P1-B03] here"
    assert q("a [P1-T2-R04] b") == "a b"
    assert q("[1] and [Note] stay") == "[1] and [Note] stay"


def test_evidence_row_id_is_optional_provenance():
    assert EV.row_id is None
    assert EV.model_copy(update={"row_id": "P1-T2-R04"}).model_dump()["row_id"] == "P1-T2-R04"
    assert Evidence(**{**EV.model_dump(), "row_id": "P1-T2-R04"}).row_id == "P1-T2-R04"
