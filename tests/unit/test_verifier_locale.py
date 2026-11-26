"""Locale-aware verification: EU/MXN numbers, DR/CR tags, Spanish and numeric dates (§ 7.3)."""

import pytest

from doc_intel.mapping.verifier import (
    canonical_value,
    derived_in_quotes,
    source_text,
    value_in_quotes,
)


@pytest.mark.parametrize(
    "value, quote",
    [
        ("444132.10", "Saldo final 444.132,10 $"),  # dot groups, decimal comma, trailing symbol
        ("444.132,10 $", "Saldo final 444.132,10 $"),
        ("12000", "Abono 12.000 $"),  # one dot group: a quote's number is read both ways
        ("12.000", "Abono 12.000 $"),
        ("1.234", "Fee 1,234"),
        ("-1,234.56", "Saldo 1,234.56 DR"),  # a DR/CR tag states the amount with either sign
        ("1,234.56", "Saldo 1,234.56 DR"),
        ("-1234.56", "Saldo DR 1,234.56"),
        ("-250", "Abono CR 250.00"),
        ("-1.234,56", "saldo 1.234,56cr"),
        ("-500.00", "Cargo 500.00-"),  # a trailing minus
        ("500.00", "Cargo 500.00-"),
    ],
)
def test_a_value_matches_its_locale_formatted_quote(value, quote):
    assert value_in_quotes(value, "currency", [quote])


@pytest.mark.parametrize(
    "value, quote",
    [
        ("12001", "Abono 12.000 $"),
        ("-1,234.56", "Saldo 1,234.56"),  # an untagged number keeps its sign
        ("-10", "pages 10-12"),  # a range is no trailing minus
        ("-5", "5 drones were bought"),  # "dr" inside a word is no tag
        ("1234.56", "Saldo 1,234.57 CR"),
    ],
)
def test_a_value_the_quote_does_not_state_fails(value, quote):
    assert not value_in_quotes(value, "currency", [quote])


def test_a_value_reads_one_way_only():
    assert canonical_value("12.000", "currency") == 12  # the value side stays unambiguous
    assert canonical_value("1,234.56 DR", "currency") == 1234.56


def test_source_text_keeps_the_tag_and_the_source_reading():
    assert source_text(-1234.56, "currency", ["Saldo 1,234.56 DR"]) == "1,234.56 DR"
    assert source_text(12000, "currency", ["Abono 12.000 $"]) == "12.000 $"
    assert source_text(-500, "currency", ["Cargo 500.00-"]) == "500.00-"


@pytest.mark.parametrize(
    "value, quote",
    [
        ("2025-03-31", "Periodo del 01 al 31 de marzo de 2025"),
        ("2025-03-31", "PERIODO DEL 01 AL 31 DE MARZO DE 2025"),  # upper case
        ("March 31, 2025", "31 DE MARZO DE 2025"),
        ("2025-03-31", "Fecha de corte 31/MAR/2025"),
        ("2025-03-31", "Fecha 31-mar-25"),  # two-digit year
        ("2025-03-31", "Corte 31/03/2025"),  # numeric, day > 12
        ("2025-03-31", "Cut-off 03/31/2025"),  # numeric, month first
        ("2025-03-31", "Corte 31.03.2025"),
        ("31 de marzo de 2025", "Corte 31/03/2025"),
        ("2025-04-03", "Corte 03/04/2025"),  # ambiguous: a quote reads both ways
        ("2025-03-04", "Corte 03/04/2025"),
        ("2025-06-30", "Periodo 01 jun 25 al 30 jun 25"),  # space, two-digit year
    ],
)
def test_a_date_matches_its_quote_in_any_supported_format(value, quote):
    assert value_in_quotes(value, "date", [quote])


@pytest.mark.parametrize(
    "value, quote",
    [("2025-03-30", "Corte 31/03/2025"), ("2025-03-31", "Fecha 31/FOO/2025")],
)
def test_a_date_the_quote_does_not_state_fails(value, quote):
    assert not value_in_quotes(value, "date", [quote])


def test_numeric_date_values():
    assert canonical_value("31/03/2025", "date") == "2025-03-31"
    assert canonical_value("31-MAR-25", "date") == "2025-03-31"
    assert canonical_value("30 jun 25", "date") == "2025-06-30"
    assert canonical_value("03/04/2025", "date") is None  # day and month order unknown


DEPOSITS = ["Total depósitos 150.000,00 $", "Total retiros 120.500,50 $"]


@pytest.mark.parametrize("value", ["29,499.50", "-29499.50", "270500.50"])
def test_derived_is_a_sum_or_difference_of_two_quoted_numbers(value):
    assert derived_in_quotes(value, "currency", DEPOSITS)


@pytest.mark.parametrize(
    "value, value_type, quotes",
    [
        ("29500", "currency", DEPOSITS),
        ("150000", "currency", []),
        ("2025-03-31", "date", DEPOSITS),
        ("several", "currency", DEPOSITS),
    ],
)
def test_derived_rejects_anything_else(value, value_type, quotes):
    assert not derived_in_quotes(value, value_type, quotes)
