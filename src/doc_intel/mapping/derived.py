"""Aggregates computed in code over the structured rows of a table (ADR-018). Pure: no I/O, no
LLM. Every failure is a code string the caller maps to `missing`; a figure is never guessed."""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from doc_intel.embeddings.schemas import Hit
from doc_intel.ingestion.schemas import TableRow
from doc_intel.mapping.verifier import _NUMBER, normalize
from doc_intel.templates.schemas import Derivation, ExpectedType

_ROW_ID = re.compile(r"^(.+)-R(\d+)$")
_TAG = re.compile(r"(?<![^\W\d_])(cr|dr)\.?(?![^\W\d_])", re.IGNORECASE)
_TAG_WITH_SPACE = re.compile(r"\s*" + _TAG.pattern + r"\s*", re.IGNORECASE)
_NUMERIC_TYPES = {"number", "currency", "percent"}

Pair = tuple[Hit, TableRow]


@dataclass(frozen=True)
class Table:
    document_id: UUID
    table_id: str  # row-id prefix, e.g. "P1-T2"
    headers: tuple[str, ...]
    rows: tuple[Pair, ...]  # in row-number order
    complete: bool  # False -> fail closed


@dataclass(frozen=True)
class Computed:
    value: str | list[str] | list[list[str]]
    # every row used, with its item index (group index for group_sum, else None)
    cited: tuple[tuple[Hit, TableRow, int | None], ...]
    number: Decimal | None = None  # the exact result of a numeric form; None for the list forms


def tables(hits: list[Hit]) -> list[Table]:
    """The tables rebuilt from the `rows` of `hits`: rows repeated by neighbor chunks once, in row
    order. A table is incomplete when its row numbers have a gap or a plain-text table chunk of the
    document sits in or next to its chunks (a split table piece without rows)."""
    found: dict[tuple[UUID, str], dict[int, Pair]] = defaultdict(dict)
    for hit in hits:
        for row in hit.rows or []:
            if m := _ROW_ID.match(row.id):
                found[(hit.document_id, m[1])].setdefault(int(m[2]), (hit, row))
    plain = [h for h in hits if h.element_type == "table" and h.rows is None]
    out = []
    for (doc, table_id), by_number in found.items():
        rows = tuple(by_number[n] for n in sorted(by_number))
        spans = [h.chunk_index for h, _ in rows if h.chunk_index is not None]
        split = bool(spans) and any(
            p.document_id == doc
            and p.chunk_index is not None
            and min(spans) - 1 <= p.chunk_index <= max(spans) + 1
            for p in plain
        )
        complete = sorted(by_number) == list(range(1, len(rows) + 1)) and not split
        out.append(Table(doc, table_id, tuple(rows[0][1].cells), rows, complete))
    return out


def row_lines(hit: Hit) -> dict[str, str]:
    """Row id -> its verbatim line in the chunk text (header line, separator, then one line per
    row, also in a chunk that continues a table: the chunker repeats the header); empty when the
    chunk has no rows or is not line-aligned."""
    lines = hit.text.split("\n")
    rows = hit.rows or []
    if len(lines) - 2 != len(rows):
        return {}
    return {r.id: ln for r, ln in zip(rows, lines[2:], strict=True)}


def row_line(hit: Hit, row: TableRow) -> str | None:
    return row_lines(hit).get(row.id)


def named(spec: Derivation) -> list[str]:
    """The headers a derivation refers to."""
    return [h for h in (spec.column, spec.minus, spec.group_by, *spec.where) if h is not None]


@dataclass(frozen=True)
class _Cell:
    num: str  # the number as printed, e.g. "1.829.914,84"
    negative: bool  # a minus sign only: DR/CR are tags, never signs
    tag: str | None  # "CR" or "DR"
    affix: tuple[str, str]  # text around the number minus the tag, e.g. ("", " $")


def _style(num: str) -> str:
    dots, commas = num.count("."), num.count(",")
    if dots and commas:
        return "eu" if num.index(".") < num.index(",") else "us"
    if not dots and not commas:
        return "plain"
    sep = "." if dots else ","
    if max(dots, commas) >= 2:
        return "eu" if dots else "us"
    if len(num.rsplit(sep, 1)[1]) == 3 and len(num.split(sep)[0]) <= 3:
        return "ambiguous"  # "12.000": twelve or a thousand
    return "us" if dots else "eu"  # a lone dot or comma before 1-2 digits is the decimal mark


def _parse(text: str) -> _Cell | str | None:
    """None: no digits (an empty cell); a str: a failure code."""
    if not any(c.isdigit() for c in text):
        return None
    found = list(_NUMBER.finditer(text))
    if len(found) != 1 or found[0][3]:  # two numbers, or a scale word ("2 million")
        return "cell_unparseable"
    m = found[0]
    before, after = text[: m.start()], text[m.end() :]
    if "(" in before and ")" in after:  # accounting negative: fail closed, never read as positive
        return "cell_unparseable"
    trailing = not m[1] and after.lstrip().startswith("-")
    if trailing:
        after = after.lstrip()[1:]
    tag = _TAG.search(before + " " + after)
    affix = (_TAG_WITH_SPACE.sub("", before), _TAG_WITH_SPACE.sub("", after))
    return _Cell(m[2], bool(m[1]) or trailing, tag[1].upper() if tag else None, affix)


def _convention(cells: list[_Cell]) -> str | None:
    """The column's number convention; None when it is mixed or only ambiguous cells exist."""
    styles = {_style(c.num) for c in cells}
    sure = styles - {"plain", "ambiguous"}
    if len(sure) > 1 or (not sure and "ambiguous" in styles):
        return None
    return sure.pop() if sure else "plain"


def _value(cell: _Cell, conv: str) -> Decimal:
    num = cell.num.replace(".", "").replace(",", ".") if conv == "eu" else cell.num.replace(",", "")
    return -Decimal(num) if cell.negative else Decimal(num)


def _decimals(cell: _Cell, conv: str) -> int:
    sep = "," if conv == "eu" else "."
    if _style(cell.num) == "ambiguous" or sep not in cell.num:
        return 0
    return len(cell.num.rsplit(sep, 1)[1])


def _render(total: Decimal, cells: list[_Cell], conv: str) -> str:
    """`total` in the source convention: its separators, the most decimals of the cells, and an
    affix (a currency sign) only when every cell has the same one."""
    places = max(_decimals(c, conv) for c in cells)
    grouped = conv == "eu" or (conv == "us" and any("," in c.num for c in cells))
    out = f"{abs(total):,.{places}f}" if grouped else f"{abs(total):.{places}f}"
    if conv == "eu":
        out = out.translate({ord(","): ".", ord("."): ","})
    before, after = cells[0].affix
    if any(c.affix != cells[0].affix for c in cells):
        before = after = ""
    return f"{before}{'-' if total < 0 else ''}{out}{after}"


def render_like(value: Decimal | int | float, cells: list[str]) -> str | None:
    """`value` in the convention, decimals and affix of the numeric `cells` sharing the most
    common affix pair (dates, ids are skipped); None when none is numeric or they disagree."""
    parsed = [c for t in cells if isinstance(c := _parse(t), _Cell)]
    if parsed:  # Counter.most_common keeps first-seen order on ties
        top = Counter(c.affix for c in parsed).most_common(1)[0][0]
        parsed = [c for c in parsed if c.affix == top]
    conv = _convention(parsed) if parsed else None
    return _render(Decimal(str(value)), parsed, conv).strip() if conv else None


def _column(rows: list[Pair], header: str) -> list[tuple[Pair, _Cell]] | str:
    cells = []
    for pair in rows:
        cell = _parse(pair[1].cells[header])
        if isinstance(cell, str):
            return cell
        if cell is not None:
            cells.append((pair, cell))
    return cells


def compute(table: Table, spec: Derivation, expected_type: ExpectedType) -> Computed | str:
    """`spec` over the rows of `table`, or a failure code: op_type_mismatch, rows_incomplete,
    column_missing, no_rows, cell_unparseable, ambiguous_number_format, untagged_cell, mixed_tags,
    group_label_missing, summary_row, exclude_unverified."""
    wanted = {"count": {"number"}, "group_sum": {"table", "bullet_list"}}.get(
        spec.op, _NUMERIC_TYPES
    )
    if expected_type not in wanted:
        return "op_type_mismatch"
    if not table.complete:
        return "rows_incomplete"
    if any(h not in table.headers for h in named(spec)):
        return "column_missing"
    if set(spec.exclude_rows) - set(
        summary_rows(table, spec.model_copy(update={"exclude_rows": []}))
    ):
        return "exclude_unverified"  # only detected summary rows may be dropped
    if summary_rows(table, spec):  # never auto-excluded: a genuine row would give a wrong total
        return "summary_row"
    rows = _kept(table, spec)
    if spec.op == "count":
        # shortcut: counts any non-blank cell (text too); no digits needed, upgrade if a count
        # over a numeric column must skip text cells.
        used = [
            p
            for p in rows
            if p[1].cells[spec.column].strip() not in ("", "-")
            and (not spec.tag or _tagged(p[1].cells[spec.column], spec.tag))
        ]
        cited = tuple((h, r, None) for h, r in used)
        return Computed(str(len(used)), cited, Decimal(len(used))) if used else "no_rows"
    return _aggregate(rows, spec, expected_type)


def _kept(table: Table, spec: Derivation) -> list[Pair]:
    """The rows left after `exclude_rows` and `where`."""
    return [
        pair
        for pair in table.rows
        if pair[1].id not in spec.exclude_rows
        and all(normalize(pair[1].cells[h]) == normalize(v) for h, v in spec.where.items())
    ]


def _tagged(text: str, tag: str) -> bool:
    found = _TAG.search(text)
    return bool(found and found[1].upper() == tag)


_Cells = list[tuple[Pair, _Cell]]


def _prepare(rows: list[Pair], spec: Derivation) -> tuple[_Cells, _Cells, str] | str:
    """The parsed main and minus cells and the column's convention, or a failure code."""
    main, minus = _column(rows, spec.column), _column(rows, spec.minus) if spec.minus else []
    for part in (main, minus):
        if isinstance(part, str):
            return part
    main = [(p, c) for p, c in main if spec.tag is None or c.tag == spec.tag]
    if not main and not minus:
        return "no_rows"
    if (
        spec.tag is None
        and {c.tag for _, c in main} >= {"CR", "DR"}
        and not (spec.op == "diff" and not spec.minus)
    ):
        return "mixed_tags"
    if (conv := _convention([c for _, c in main + minus])) is None:
        return "ambiguous_number_format"
    return main, minus, conv


def _summaries(seq: list[tuple[str, Decimal]]) -> list[str]:
    """Ids of values equal to the sum of the non-summary values since the previous summary or to
    the sum of all non-summary values before them, each over at least two values."""
    out: list[str] = []
    plain: list[Decimal] = []
    since: list[Decimal] = []
    for row_id, v in seq:
        if (len(since) >= 2 and v == sum(since)) or (len(plain) >= 2 and v == sum(plain)):
            out.append(row_id)
            since = []
        else:
            plain.append(v)
            since.append(v)
    return out


def summary_rows(table: Table, spec: Derivation) -> list[str]:
    """Ids of printed total, subtotal or carried-forward rows among the values `spec` aggregates,
    found by arithmetic alone (language-agnostic). Any parse failure gives [] (`compute` reports
    it). shortcut: a genuine row that equals the running or cumulative sum is flagged too (the slot
    goes missing); a count over a text column is not checked, upgrade if counts need it."""
    rows = _kept(table, spec)
    prepared = _prepare(rows, spec)
    if isinstance(prepared, str):
        return []
    main, minus, conv = prepared
    if spec.op == "count" and any(
        (t := p[1].cells[spec.column].strip()) not in ("", "-") and _parse(t) is None for p in rows
    ):
        return []
    if spec.op == "diff" and spec.minus:
        groups = [main, minus]
    elif spec.op == "diff":
        groups = [[x for x in main if x[1].tag == t] for t in ("CR", "DR")]
    else:
        groups = [main]
    return [rid for g in groups for rid in _summaries([(p[1].id, _value(c, conv)) for p, c in g])]


def _aggregate(rows: list[Pair], spec: Derivation, expected_type: str) -> Computed | str:
    prepared = _prepare(rows, spec)
    if isinstance(prepared, str):
        return prepared
    main, minus, conv = prepared
    cells = [c for _, c in main + minus]
    if spec.op == "group_sum":
        return _groups(main, spec, conv, expected_type)
    values = [_value(c, conv) for _, c in main]
    if spec.op == "diff" and spec.minus:
        total = sum(values) - sum(_value(c, conv) for _, c in minus)
    elif spec.op == "diff":
        if any(c.tag is None for c in cells):
            return "untagged_cell"
        total = sum(-v if c.tag == "DR" else v for v, (_, c) in zip(values, main, strict=True))
    else:
        total = {"sum": sum, "min": min, "max": max}[spec.op](values)
    used = {p[1].id for p, _ in main + minus}
    cited = tuple((h, r, None) for h, r in rows if r.id in used)
    return Computed(_render(total, cells, conv), cited, total)


def _groups(
    main: list[tuple[Pair, _Cell]], spec: Derivation, conv: str, expected_type: str
) -> Computed | str:
    sums: dict[str, Decimal] = {}
    members: dict[str, list[tuple[Pair, _Cell]]] = defaultdict(list)
    for pair, cell in main:
        label = pair[1].cells[spec.group_by or ""].strip()
        if not label:
            return "group_label_missing"
        sums[label] = sums.get(label, Decimal(0)) + _value(cell, conv)
        members[label].append((pair, cell))
    ranked = sorted(sums, key=lambda k: -sums[k])[: spec.top_n]  # stable: ties by first seen
    cells = [c for k in ranked for _, c in members[k]]
    amounts = [(k, _render(sums[k], cells, conv)) for k in ranked]
    value = (
        [[k, a] for k, a in amounts]
        if expected_type == "table"
        else [f"{k}: {a}" for k, a in amounts]
    )
    cited = tuple((h, r, i) for i, k in enumerate(ranked) for (h, r), _ in members[k])
    return Computed(value, cited)
