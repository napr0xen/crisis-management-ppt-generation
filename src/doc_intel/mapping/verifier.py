"""Deterministic citation checks (system-design § 7.3). Nothing here trusts LLM output."""

import re
import unicodedata
from datetime import date
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
# shortcut: single-group "12.000" and "1,234" read as US numbers, add a per-document locale hint
# when a source writes single-group European numbers.
# European reads only when unambiguous: dot groups + decimal comma ("1.829.914,84"), two or more
# dot groups ("1.829.914"), or a 1-2 digit decimal comma ("752,76", "0,5", "12,34").
# Sign only when not glued to a word or dash ("COVID-19", "10-12" are not negative); a following
# magnitude word scales the number ("2.5 million" == 2500000).
# Thousands separators only in proper groups ("3,4,5" is three numbers); no sign after % or $.
_NUMBER = re.compile(
    r"(?:(?<![\w%$-])(-))?"
    r"(\d{1,3}(?:\.\d{3})+,\d+|\d{1,3}(?:\.\d{3}){2,}|\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    r"|(?<!\d[.,])\d+,\d{1,2}(?!\d|[.,]\d)|\d+(?:\.\d+)?)"
    r"(?:\s*(thousand|million|billion)\b)?"
)
_SCALE = {"thousand": 10**3, "million": 10**6, "billion": 10**9}
# A quote's number is read every way its source may mean it: one thousands group ("12.000",
# "1,234") both US and European, a DR/CR tag or a trailing minus ("500.00-") either sign. A value
# is still read one way only, so it never states two numbers.
_ONE_GROUP = re.compile(r"\d{1,3}[.,]\d{3}")
_TAG_BEFORE = re.compile(r"(?<![^\W\d_])(?:dr|cr)\.? ?$", re.IGNORECASE)
_TAG_AFTER = re.compile(r" ?(?:dr|cr)(?![^\W\d_])|-(?![\d.,])", re.IGNORECASE)
TYPED_TYPES = frozenset({"number", "currency", "percent", "date"})
_MONTH_NAMES = {
    "en": "january february march april may june july august september october november december",
    "es": "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre"
    " diciembre",
}
MONTHS = {
    w: i
    for names in _MONTH_NAMES.values()
    for i, name in enumerate(names.split(), 1)
    for w in (name, name[:3])
}
MONTHS.update({"sept": 9, "setiembre": 9})
# shortcut: ISO and English/Spanish month-name dates only (no 03/04/2026: day/month order is
# ambiguous), add formats when the eval set has them.
_WORD = r"([^\W\d_]+)\.?"
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DMY = re.compile(rf"\b(\d{{1,2}}) (?:del? )?{_WORD} (?:del? )?(\d{{4}})\b", re.IGNORECASE)
_MDY = re.compile(rf"\b{_WORD} (\d{{1,2}}),? (\d{{4}})\b", re.IGNORECASE)
# shortcut: a two-digit year is 20YY, read a per-document century if older statements matter.
_DMY_SEP = re.compile(
    r"\b(\d{1,2})([/. -])([^\W\d_]{3,})\.?\2(\d{4}|\d{2})\b"
)  # 31/MAR/25, 30 jun 25
_NUMERIC = re.compile(r"\b(\d{1,2})([/.-])(\d{1,2})\2(\d{4})\b")  # 31/03/2025, 03/31/2025


def _decimal(num: str) -> Decimal:
    if "," in num:
        european = ("." in num and num.index(".") < num.index(",")) or len(num.split(",")[-1]) <= 2
    else:
        european = num.count(".") >= 2
    return Decimal(num.replace(".", "").replace(",", ".") if european else num.replace(",", ""))


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


_NUMBER_CI = re.compile(_NUMBER.pattern, re.IGNORECASE)  # source_text keeps the source's case


MAX_ITEM_CHARS, MIN_SUPPORT, _STEM = 400, 0.6, 5
_WORDS = re.compile(r"\w{4,}")


def supported(text: str, quotes: list[str], min_support: float = MIN_SUPPORT) -> bool:
    """A synthesized item is backed by its verified quotes: every number appears in them and at
    least `min_support` (60%) of its content words (4+ characters, same first 5 characters) do
    too."""
    # shortcut: scripts written without spaces (CJK, Thai) form one long word per run and need
    # the whole run verbatim, add a tokenizer when such sources matter.
    if not quotes or not text.strip() or len(text) > MAX_ITEM_CHARS:
        return False
    item, source = normalize(text), normalize(" ".join(quotes))
    if not _numbers(item) <= _numbers(source, True):  # canonical values: 1.8 is never 18
        return False
    have = [w for w in _WORDS.findall(source) if not w.isdigit()]
    words = [w for w in _WORDS.findall(item) if not w.isdigit()]

    def found(w: str) -> bool:
        return any(w[: (n := min(_STEM, len(w), len(q)))] == q[:n] for q in have)

    return not words or sum(map(found, words)) / len(words) >= min_support


def _tagged(s: str, m: re.Match[str]) -> bool:
    return bool(_TAG_BEFORE.search(s, 0, m.start()) or _TAG_AFTER.match(s, m.end()))


def _number_spans(
    s: str, rx: re.Pattern[str], quote: bool = False
) -> list[tuple[Decimal, re.Match[str]]]:
    """Every number of `s` with its span; `quote` adds the other readings a source may mean."""
    spans = []
    for m in rx.finditer(s):
        scale = _SCALE.get((m[3] or "").casefold(), 1)
        readings = {_decimal(m[2])}
        if quote and _ONE_GROUP.fullmatch(m[2]):
            other = m[2].replace(".", "") if "." in m[2] else m[2].replace(",", ".")
            readings.add(Decimal(other))
        signs = (1, -1) if quote and _tagged(s, m) else (-1 if m[1] else 1,)
        spans += [(sign * n * scale, m) for n in readings for sign in signs]
    return spans


def _numbers(s: str, quote: bool = False) -> set[Decimal]:
    return {n for n, _ in _number_spans(normalize(s), _NUMBER, quote)}


def _date_spans(s: str, quote: bool = False) -> list[tuple[date, re.Match[str]]]:
    """Every date of `s`; `quote` also reads a numeric date both ways when day and month are
    both 12 or less (03/04/2025), which a value never is."""
    spans = []

    def add(y: int, month: int | None, d: int, m: re.Match[str]) -> None:
        try:
            spans.append((date(y, month, d), m))
        except (ValueError, TypeError):  # "4 Marchy 2026", "31 febrero 2022": not dates
            pass

    for rx, order in ((_ISO, "ymd"), (_DMY, "dmy"), (_MDY, "mdy")):
        for m in rx.finditer(s):
            parts = dict(zip(order, m.groups(), strict=True))
            month = (
                MONTHS.get(parts["m"].casefold()) if not parts["m"].isdigit() else int(parts["m"])
            )
            add(int(parts["y"]), month, int(parts["d"]), m)
    for m in _DMY_SEP.finditer(s):
        y = int(m[4])
        add(y + 2000 if y < 100 else y, MONTHS.get(m[3].casefold()), int(m[1]), m)
    for m in _NUMERIC.finditer(s):
        a, b = int(m[1]), int(m[3])
        if quote or a > 12 or b > 12:  # an impossible month drops that reading
            for d, month in {(a, b), (b, a)}:
                add(int(m[4]), month, d, m)
    return spans


def is_date(s: str) -> bool:
    """The whole text is one date the verifier can parse (ISO, month-name or numeric)."""
    s = s.strip()
    return any(m[0] == s for _, m in _date_spans(s))


def _dates(s: str, quote: bool = False) -> set[date]:
    return {d for d, _ in _date_spans(s, quote)}


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


def states_value(text: str, value_type: str) -> bool:
    """`text` states at least one number (or date, for `date`) the verifier can read."""
    return bool(_dates(text, True) if value_type == "date" else _numbers(text, True))


def value_in_quotes(value: str | int | float, value_type: str, quotes: list[str]) -> bool:
    """A typed value (`number`, `currency`, `percent`, `date`) appears in some verified quote."""
    c = _canonical(value, value_type)
    canonical = _dates if value_type == "date" else _numbers
    return c is not None and any(c in canonical(q, True) for q in quotes)


def derived_in_quotes(value: str | int | float, value_type: str, quotes: list[str]) -> bool:
    """A computed number (net movement = inflows - outflows) is the sum or difference of two
    different numbers the verified quotes state."""
    # shortcut: two operands only, add longer sums when a template asks for totals of 3+ figures.
    if value_type not in ("number", "currency", "percent"):
        return False
    c = _canonical(value, value_type)
    found: set[Decimal] = set()
    for q in quotes:
        found |= _numbers(q, True)
    return isinstance(c, Decimal) and any(
        b in found and b != a for a in found for b in (c - a, a - c)
    )


# shortcut: any 3 uppercase ASCII letters beside a currency number read as its code ("85 THE"),
# restrict to ISO 4217 codes if a source trips it.
_CODE_BEFORE = re.compile(r"(?<![A-Za-z])[A-Z]{3} ?$")
_CODE_AFTER = re.compile(r" ?[A-Z]{3}(?![A-Za-z])")
# A percent sign, or 1-2 letters glued to the end of the number ("182.4M", "5bn", "3.2K"); a
# letter followed by a digit ("2026Q3") is not a suffix.
_SUFFIX = re.compile(r" ?%|[^\W\d_]{1,2}(?!\w)")


def _is_symbol(ch: str) -> bool:
    return unicodedata.category(ch) == "Sc"


_TAG_EDGE = re.compile(r"^(?i:dr|cr)\.? ?| ?(?i:dr|cr)$")
_CODE_EDGE = re.compile(r"^[A-Z]{3} ?| ?[A-Z]{3}$")


def is_amount(text: str) -> bool:
    """`text` is one monetary amount and nothing else: a number with a decimal part or thousands
    grouping, or any number carrying a currency symbol or DR/CR tag. A bare 3-letter code is
    stripped but never makes an amount alone ("REF 1234" is a reference)."""
    t = text.strip()
    core = "".join(c for c in t if not _is_symbol(c)).strip()
    untagged = _TAG_EDGE.sub("", core).strip()
    bare = _CODE_EDGE.sub("", untagged).strip()
    marked = untagged != t or "," in bare or "." in bare
    return bool(_NUMBER.fullmatch(bare)) and marked


def _with_marker(t: str, start: int, end: int, value_type: str) -> tuple[int, int]:
    """Widen a number span over a glued suffix (%, magnitude letters), then an adjacent currency
    symbol or currency code."""
    if tag := _TAG_AFTER.match(t, end):
        end = tag.end()
    elif tag := _TAG_BEFORE.search(t, 0, start):
        start = tag.start()
    if suffix := _SUFFIX.match(t, end):
        end = suffix.end()
    if start >= 1 and _is_symbol(t[start - 1]):
        return start - 1, end
    if start >= 2 and t[start - 1] == " " and _is_symbol(t[start - 2]):
        return start - 2, end
    if end < len(t) and _is_symbol(t[end]):
        return start, end + 1
    if end + 1 < len(t) and t[end] == " " and _is_symbol(t[end + 1]):
        return start, end + 2
    if value_type == "currency":
        if before := _CODE_BEFORE.search(t, 0, start):
            return before.start(), end
        if after := _CODE_AFTER.match(t, end):
            return start, after.end()
    return start, end


def source_text(value: str | int | float, value_type: str, quotes: list[str]) -> str | None:
    """The span of the first quote that states `value`, as the source wrote it (symbol, code,
    percent sign, DR/CR tag, date format), found with the same patterns `value_in_quotes`
    uses."""
    c = _canonical(value, value_type)
    if c is None:
        return None
    for q in quotes:
        t = unicodedata.normalize("NFKC", q).translate(_PUNCT)
        if value_type == "date":
            spans = _date_spans(t, True)
        else:
            spans = _number_spans(t, _NUMBER_CI, True)
        for found, m in spans:
            if found == c:
                if value_type == "date":
                    return m[0]
                start, end = _with_marker(t, m.start(), m.end(), value_type)
                return t[start:end]
    return None
