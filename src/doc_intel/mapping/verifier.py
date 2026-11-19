"""Deterministic citation checks (system-design § 7.3). Nothing here trusts LLM output."""

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal

from doc_intel.embeddings.schemas import Hit

MIN_QUOTE_CHARS = 8
_PUNCT = str.maketrans(
    {
        **dict.fromkeys("\u2018\u2019\u201a\u201b\u2032", "'"),  # curly single, prime
        **dict.fromkeys("\u201c\u201d\u201e\u201f\u2033", '"'),  # curly double
        **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"),  # dashes
        "|": " ",  # a quoted Markdown table row matches its chunk
    }
)
_WS = re.compile(r"\s+")
# shortcut: "1,5" (decimal comma) and "12.000" (dot thousands) read as English numbers, add a
# locale-aware parser when non-English documents need typed values.
# Sign only when not glued to a word or dash ("COVID-19", "10-12" are not negative); a following
# magnitude word scales the number ("2.5 million" == 2500000).
# Thousands separators only in proper groups ("3,4,5" is three numbers); no sign after % or $.
_NUMBER = re.compile(
    r"(?:(?<![\w%$-])(-))?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?:\s*(thousand|million|billion)\b)?"
)
_SCALE = {"thousand": 10**3, "million": 10**6, "billion": 10**9}
TYPED_TYPES = frozenset({"number", "currency", "percent", "date"})
# shortcut: ISO and English month-name dates only (no 03/04/2026: day/month order is ambiguous),
# add formats when the eval set has them.
_DATES = [
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "%Y-%m-%d"),
    (re.compile(r"\b\d{1,2} [A-Za-z]+ \d{4}\b"), "%d %B %Y"),
    (re.compile(r"\b[A-Za-z]+ \d{1,2}, \d{4}\b"), "%B %d, %Y"),
]


def normalize(s: str) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFKC", s).casefold().translate(_PUNCT)).strip()


def verify_quote(hits: list[Hit], chunk_id: str, quote: str) -> Hit | None:
    """The cited hit iff `chunk_id` is one of `hits` and its text contains the quote."""
    q = normalize(quote)
    if len(q) < MIN_QUOTE_CHARS:
        return None
    cited = chunk_id.strip().lower()
    for hit in hits:
        if str(hit.chunk_id) == cited:
            return hit if q in normalize(hit.text) else None
    return None


def _numbers(s: str) -> set[Decimal]:
    found = set()
    for sign, digits, fraction, scale in _NUMBER.findall(normalize(s)):
        n = Decimal(digits.replace(",", "") + fraction) * _SCALE.get(scale, 1)
        found.add(-n if sign else n)
    return found


def _dates(s: str) -> set[date]:
    found = set()
    for rx, fmt in _DATES:
        for m in rx.findall(s):
            try:
                found.add(datetime.strptime(m, fmt).date())
            except ValueError:  # "4 Marchy 2026": a lookalike, not a date
                continue
    return found


def _canonical(value: str | int | float, value_type: str) -> Decimal | date | None:
    """The one number (or date) a typed value states; None when it states none or several."""
    found = _dates(str(value)) if value_type == "date" else _numbers(str(value))
    return next(iter(found)) if len(found) == 1 else None


def canonical_value(value: str | int | float, value_type: str) -> int | float | str | None:
    """int | float for numbers, ISO string for dates, None unless exactly one is stated."""
    c = _canonical(value, value_type)
    if c is None:
        return None
    if isinstance(c, date):
        return c.isoformat()
    # shortcut: floats lose precision above ~15 significant digits, return Decimal strings if the
    # eval ever has such values.
    return int(c) if c == c.to_integral_value() else float(c)


def value_in_quotes(value: str | int | float, value_type: str, quotes: list[str]) -> bool:
    """A typed value (`number`, `currency`, `percent`, `date`) appears in some verified quote."""
    c = _canonical(value, value_type)
    canonical = _dates if value_type == "date" else _numbers
    return c is not None and any(c in canonical(q) for q in quotes)
