"""Deterministic quote and typed-value verification (system-design § 7, roadmap Phase 4 exit)."""

import uuid

import pytest

from doc_intel.embeddings.schemas import Hit
from doc_intel.mapping.verifier import normalize, value_in_quotes, verify_quote


def _hit(text: str) -> Hit:
    return Hit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="ops_report.pdf",
        page_number=4,
        section_heading="Impact",
        element_type="text",
        text=text,
        score=0.5,
    )


HIT = _hit("Approximately 12,000 customers were affected by the “planned” outage — see below.")
TABLE = _hit("| Region | Status |\n| --- | --- |\n| North | Contained |")


def test_exact_quote_verifies_and_returns_the_hit():
    assert verify_quote([HIT], str(HIT.chunk_id), "12,000 customers were affected") is HIT


def test_whitespace_case_curly_quotes_and_dashes_are_normalized():
    quote = '12,000   CUSTOMERS were affected by the "planned" outage - see'
    assert verify_quote([HIT], str(HIT.chunk_id), quote) is HIT


def test_markdown_table_row_quoted_without_pipes_verifies():
    assert verify_quote([TABLE], str(TABLE.chunk_id), "North Contained") is TABLE


def test_chunk_id_is_matched_case_insensitively():
    assert verify_quote([HIT], str(HIT.chunk_id).upper(), "12,000 customers") is HIT


def test_quote_not_in_the_cited_chunk_fails():
    assert verify_quote([HIT], str(HIT.chunk_id), "15,400 customers were affected") is None


def test_quote_from_another_hit_does_not_verify_against_the_cited_chunk():
    assert verify_quote([HIT, TABLE], str(TABLE.chunk_id), "12,000 customers were affected") is None


def test_chunk_not_in_hits_fails():
    assert verify_quote([HIT], str(uuid.uuid4()), "12,000 customers were affected") is None


def test_cross_tenant_chunk_fails_even_when_the_text_matches():
    """Another tenant's chunk is never in this tenant's hits, so its id can't verify."""
    other_tenant = _hit(HIT.text)
    assert verify_quote([HIT], str(other_tenant.chunk_id), "12,000 customers") is None


def test_malformed_chunk_id_fails():
    assert verify_quote([HIT], "chunk-1", "12,000 customers were affected") is None


@pytest.mark.parametrize("quote", ["12,000", "  affect  ", "1234567", ""])
def test_quotes_under_8_normalized_chars_fail(quote):
    assert verify_quote([HIT], str(HIT.chunk_id), quote) is None


def test_normalize():
    assert (
        normalize("  \u201cA\u201d \u2018b\u2019 \u2013 c \u2014 d | e\n\tF  ")
        == "\"a\" 'b' - c - d e f"
    )


@pytest.mark.parametrize(
    "value, value_type, quote",
    [
        ("12,000", "number", "approximately 12000 customers"),
        (12000, "number", "approximately 12,000 customers"),
        ("$2.5", "currency", "a budget of 2.5 million dollars"),
        ("87%", "percent", "capacity stands at 87 percent"),
        ("2026-03-04", "date", "on 4 March 2026 the river crested"),
        ("March 4, 2026", "date", "dated 2026-03-04"),
    ],
)
def test_typed_value_present_in_a_quote(value, value_type, quote):
    assert value_in_quotes(value, value_type, ["unrelated", quote])


@pytest.mark.parametrize(
    "value, value_type, quotes",
    [
        ("15,400", "number", ["approximately 12,000 customers"]),
        ("2026-03-05", "date", ["on 4 March 2026"]),
        ("12,000", "number", []),
        ("about a dozen", "number", ["approximately 12 customers"]),  # value has no number
        ("4 Marchy 2026", "date", ["on 4 March 2026"]),  # a lookalike, not a date
    ],
)
def test_typed_value_absent_from_quotes(value, value_type, quotes):
    assert not value_in_quotes(value, value_type, quotes)
