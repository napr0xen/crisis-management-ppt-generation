"""EN/ES carry-forward and total phrases, shared by the parser (page joins) and the chunker
(`TableRow.row_role`)."""

import re

_CARRY = ("balance brought forward", "brought forward", "balance carried forward")
_CARRY += ("carried forward", "saldo anterior", "suma y sigue")
_CARRY += ("continued on next page", "continued overleaf", "continúa en la página siguiente")
_TOTAL_ONE = ("total", "totals", "subtotal", "suma", "totales")
_TOTAL_MANY = ("sub-total", "sub total", "grand total", "total charged", "total amount")
_TOTAL_MANY += ("total due", "suma total", "total general")

# a carry-forward line may carry an amount (and CR/DR) after the phrase
_CARRY_RE = re.compile(rf"^(?:{'|'.join(map(re.escape, _CARRY))})[:\s]*[\d\s.,$€£()+-]*(?:cr|dr)?$")
_ONE_RE = re.compile(rf"^(?:{'|'.join(_TOTAL_ONE)})$")
_MANY_RE = re.compile(rf"^(?:{'|'.join(map(re.escape, _TOTAL_MANY))})\b")


def _norm(text: str) -> str:
    return " ".join(text.split()).lower().rstrip(":").rstrip()


def is_carry_forward(text: str) -> bool:
    return bool(_CARRY_RE.match(_norm(text)))


def is_total(text: str) -> bool:
    t = _norm(text)
    return bool(_ONE_RE.match(t) or _MANY_RE.match(t))


_AMOUNT = re.compile(
    r"^[+-]?\(?(?:[A-Z]{3}\s?|[$€£]\s?)?[+-]?\d[\d.,']*\)?\s?(?:[$€£]|[A-Z]{3})?(?:\s?(?:CR|DR))?$"
)
_PAGE = re.compile(r"^(?:(?:page|pág\.?|página)\s*)?\d+(?:\s*(?:of|de|/)\s*\d+)?$", re.IGNORECASE)


def is_amount(text: str) -> bool:
    """Digits with separators, optional sign / parentheses / currency / CR / DR."""
    return bool(_AMOUNT.match(text.strip()))


def is_strong_amount(text: str) -> bool:
    """An amount that is not a bare integer (a year or a count is not one)."""
    return is_amount(text) and not text.strip().isdigit()


def is_page_number(text: str) -> bool:
    """A whole-line page number: "Page 2", "Page 2 of 4", "2 / 4", "Página 2 de 4", "2"."""
    return bool(_PAGE.match(" ".join(text.split())))
