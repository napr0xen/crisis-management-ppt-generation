"""Pure scoring for the regression bench (testing-strategy § 5): no services, no document logs."""

import re
from collections import Counter

from doc_intel.mapping.verifier import normalize, value_in_quotes

COUNTS = ("fields", "fields_correct", "populated", "populated_wrong", "rows", "rows_aligned")
_SEPARATOR = re.compile(r"^:?-+:?$")


def row_matches(actual: list, expected: list) -> bool:
    return len(actual) == len(expected) and all(
        normalize(str(e)) in normalize(str(a)) for a, e in zip(actual, expected, strict=True)
    )


def matches(actual, expected) -> bool:
    if isinstance(expected, list) and expected and isinstance(expected[0], list):
        # a table: same row count, each expected row matches its own actual row (row order
        # across documents is not defined)
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        left = [r for r in actual if isinstance(r, list)]
        for row in expected:
            hit = next((a for a in left if row_matches(a, row)), None)
            if hit is None:
                return False
            left.remove(hit)
        return True
    if isinstance(expected, list):
        joined = normalize(" ".join(actual)) if isinstance(actual, list) else ""
        return all(normalize(item) in joined for item in expected)
    if isinstance(expected, str):
        return isinstance(actual, str) and normalize(expected) in normalize(actual)
    return isinstance(actual, int | float) and float(actual) == float(expected)


def source_rows(case: dict) -> list[str]:
    """Each Markdown pipe row of the case's sources as one string (separator rows skipped)."""
    rows = []
    for source in case["sources"]:
        for line in source["text"].splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.strip().startswith("|") and not all(_SEPARATOR.match(c) for c in cells if c):
                rows.append(" | ".join(cells))
    return rows


def _aligned(cells: list, rows: list[str]) -> bool:
    """Every non-empty cell occurs in one single source row."""
    cells = [c for c in cells if c is not None and str(c).strip()]

    def in_row(cell, row: str) -> bool:
        if isinstance(cell, int | float):
            return value_in_quotes(cell, "number", [row])
        return normalize(str(cell)) in normalize(row)

    return any(all(in_row(c, row) for c in cells) for row in rows)


def _is_table(value) -> bool:
    return isinstance(value, list) and bool(value) and isinstance(value[0], list)


def _wrong(result, gold: dict | None) -> bool:
    if gold is None or gold["status"] in ("missing", "conflict"):
        return True
    return "value" in gold and not matches(result.value, gold["value"])


def score_case(case: dict, gold: dict, results: dict) -> Counter:
    """Counts for one case; `results` maps field_id to a FieldMappingResult."""
    fields, count = gold["fields"], Counter()
    for field_id, expected in fields.items():
        result = results.get(field_id)
        count["fields"] += 1
        count["fields_correct"] += bool(
            result
            and result.status == expected["status"]
            and ("value" not in expected or matches(result.value, expected["value"]))
        )
    for field_id, result in results.items():
        if result.status == "populated":
            count["populated"] += 1
            count["populated_wrong"] += _wrong(result, fields.get(field_id))
    rows = source_rows(case)
    # shortcut: a numeric cell is compared as a number against one row's text, not per column;
    # tighten if a row ever aligns only by coincidence.
    for result in results.values():
        if result.status == "populated" and _is_table(result.value):
            for row in result.value:
                count["rows"] += 1
                count["rows_aligned"] += _aligned(row, rows)
    for group in gold["row_groups"]:
        got = [results.get(f) for f in group]
        if all(r and r.status in ("populated", "partial") for r in got):
            count["rows"] += 1
            count["rows_aligned"] += _aligned([r.value for r in got], rows)
    return count


def rates(count: Counter) -> dict:
    def rate(num: str, den: str) -> float:
        return count[num] / count[den] if count[den] else 0.0

    return {
        "field_accuracy": rate("fields_correct", "fields"),
        "populated_but_wrong_rate": rate("populated_wrong", "populated"),
        # no populated rows: nothing misaligned, so 1.0 (the count `rows` says n=0)
        "row_alignment_accuracy": rate("rows_aligned", "rows") if count["rows"] else 1.0,
        **{k: count[k] for k in COUNTS},
    }
