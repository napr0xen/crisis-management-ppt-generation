"""Deterministic quote and typed-value verification (system-design § 7, roadmap Phase 4 exit)."""

import uuid
from decimal import Decimal

import pytest

from doc_intel.embeddings.schemas import Hit
from doc_intel.mapping.verifier import (
    _numbers,
    canonical_value,
    normalize,
    value_in_quotes,
    verify_quote,
)


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
        ("$2.5 million", "currency", "a budget of 2.5 million dollars"),
        ("2,500,000", "currency", "a budget of $2.5 million"),
        ("-5", "number", "fell by \u22125 points"),  # Unicode minus
        ("-5", "number", "fell by -5 points"),
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
        ("$2.5", "currency", ["a budget of 2.5 million dollars"]),  # under-scaled
        ("-5", "number", ["5 degrees"]),  # sign dropped
        ("5", "number", ["fell by -5 degrees"]),  # sign invented
        ("5", "number", ["COVID-19 case 19"]),  # hyphen is not a sign
        ("5 and 6", "number", ["5 and 6 people"]),  # value states two numbers
    ],
)
def test_typed_value_absent_from_quotes(value, value_type, quotes):
    assert not value_in_quotes(value, value_type, quotes)


@pytest.mark.parametrize(
    "value, value_type, expected",
    [
        ("12,000", "number", 12000),
        (12000.0, "number", 12000),
        ("$2.5 million", "currency", 2500000),
        ("2.5", "number", 2.5),
        ("87%", "percent", 87),
        ("\u22125", "number", -5),
        ("(-5)", "number", -5),
        ("10-12", "number", None),
        ("3,4,5", "number", None),
        ("ids 12,34", "number", 12.34),  # a 2-digit group after a comma is a decimal comma
        ("1,234,567.5", "number", 1234567.5),
        ("$-5", "currency", 5),
        ("5%-10%", "percent", None),
        ("none", "number", None),
        ("4 March 2026", "date", "2026-03-04"),
        ("2026-03-04", "date", "2026-03-04"),
        ("4 March 2026 or 5 March 2026", "date", None),
        ("soon", "date", None),
    ],
)
def test_canonical_value(value, value_type, expected):
    assert canonical_value(value, value_type) == expected


@pytest.mark.parametrize(
    "text, numbers",
    [
        ("3,4,5", {3, 4, 5}),
        ("ids 12,34", {Decimal("12.34")}),
        ("12,000", {12000}),
        ("1,234,567.5", {1234567.5}),
        ("5%-10%", {5, 10}),
        ("$-5", {5}),
    ],
)
def test_number_grouping_and_sign_rules(text, numbers):
    assert _numbers(text) == {Decimal(str(n)) for n in numbers}


@pytest.mark.parametrize(
    "value, expected",
    [
        ("444.132,10 $", 444132.1),
        ("1.829.914,84 $", 1829914.84),
        ("58.100,00 DR", 58100),
        ("1.829.914", 1829914),
        ("0,5", 0.5),
        ("752,76 DR", 752.76),
        ("10,50", 10.5),
        ("1,829,914.84", 1829914.84),
        ("534,613.48", 534613.48),
        ("1,234", 1234),
        ("12.000", 12),
        ("3,4,5", None),
    ],
)
def test_european_and_ambiguous_numbers(value, expected):
    assert canonical_value(value, "currency") == expected


@pytest.mark.parametrize(
    "value, quote, expected",
    [
        ("534,613.48", "Saldo 534.613,48 $", True),
        (444132.1, "444.132,10 $", True),
        (444132.11, "444.132,10 $", False),
    ],
)
def test_us_and_european_amounts_match_across_forms(value, quote, expected):
    assert value_in_quotes(value, "currency", [quote]) is expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("30 junio 2022", "2022-06-30"),
        ("1 de junio de 2022", "2022-06-01"),
        ("1 del junio del 2022", "2022-06-01"),
        ("31 mayo 2022", "2022-05-31"),
        ("30 jun. 2022", "2022-06-30"),
        ("30 JUN 2022", "2022-06-30"),
        ("June 30 2022", "2022-06-30"),
        ("Sept. 5, 2022", "2022-09-05"),
        ("4 March 2026", "2026-03-04"),
        ("2022-06-30", "2022-06-30"),
        ("31 febrero 2022", None),
        ("4 Marchy 2026", None),
        ("03/04/2026", None),
    ],
)
def test_spanish_and_english_dates(text, expected):
    assert canonical_value(text, "date") == expected


def test_iso_date_matches_spanish_quote():
    assert value_in_quotes("2022-06-30", "date", ["Saldo al 30 junio 2022"])
