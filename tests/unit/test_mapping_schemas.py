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
