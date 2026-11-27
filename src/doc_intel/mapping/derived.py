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
from doc_intel.templates.ir_builder import _direction, _words
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
    heads = (spec.column, spec.minus, spec.group_by, spec.pick, *spec.where)
    return [h for h in heads if h is not None]


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
    group_label_missing, summary_row, exclude_unverified, rank_out_of_range, rank_tied."""
    wanted = {
        "count": {"number"},
        "group_sum": {"text", "number", "currency"} if spec.rank else {"table", "bullet_list"},
        "top": _NUMERIC_TYPES | {"text", "date"},
    }.get(spec.op, _NUMERIC_TYPES)
    if expected_type not in wanted:
        return "op_type_mismatch"
    if not table.complete:
        return "rows_incomplete"
    if any(h not in table.headers for h in named(spec)):
        return "column_missing"
    tagged = {p[1].id for p in table.rows if p[1].row_role != "data"}
    if (
        set(spec.exclude_rows)
        - tagged
        - set(summary_rows(table, spec.model_copy(update={"exclude_rows": []})))
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


def _real(table: Table, name: str) -> str | None:
    """The table's own header for `name`: the exact one, else the one equal after `normalize`."""
    if name in table.headers:
        return name
    found = [h for h in table.headers if normalize(h) == normalize(name)]
    return found[0] if len(found) == 1 else None


def _data(table: Table, header: str) -> list[str]:
    return [r.cells.get(header, "") for _, r in table.rows if r.row_role == "data"]


def _numeric(table: Table, header: str) -> bool:
    """Every non-empty data cell is a bare number (a tag or currency sign aside) and one exists.
    shortcut: a currency word in the cell ("USD 5") makes the column text, upgrade with a list."""
    cells = [_parse(t) for t in _data(table, header)]
    cells = [c for c in cells if c is not None]
    return bool(cells) and all(
        isinstance(c, _Cell) and not any(ch.isalpha() for ch in "".join(c.affix)) for c in cells
    )


def both_tags(table: Table, header: str) -> bool:
    """The column holds CR and DR cells (a one-sided suffix, e.g. a balance's "Cr", is no tag
    column)."""
    cells = [_parse(t) for t in _data(table, header)]
    return {c.tag for c in cells if isinstance(c, _Cell)} >= {"CR", "DR"}


# shortcut: English/Spanish words only, add words per language when a source needs them.
_BALANCE_WORDS = frozenset("balance balances saldo saldos".split())


def _amount(table: Table, spec: Derivation, tag: str | None) -> str:
    """The amount column: the numeric column whose header names the wanted direction, else the one
    holding both CR and DR tags (also for an untagged group_sum), else the only numeric one (unless
    the LLM named another real header), else the LLM's when numeric; code `column_ambiguous`
    otherwise. A sum, group_sum, top or count never takes a balance column (the LLM naming one
    counts as naming none)."""
    nums = [h for h in table.headers if _numeric(table, h)]
    llm = _real(table, spec.column)
    if spec.op in ("sum", "group_sum", "top", "count"):  # a running balance: never aggregated
        balances = [h for h in nums if _BALANCE_WORDS & _words((h,))[0]]
        nums = [h for h in nums if h not in balances]
        llm = None if llm in balances else llm
    if tag or spec.op == "group_sum" or (spec.op == "diff" and not spec.minus):
        named = [h for h in nums if tag and _direction((h,)) == tag]
        tagged = [h for h in nums if both_tags(table, h)]
        for found in (named, tagged):
            if len(found) == 1:
                return found[0]
    if len(nums) == 1:
        return nums[0] if llm in (None, nums[0]) else "column_ambiguous"
    return llm if llm is not None and llm in nums else "column_ambiguous"


def _sep(table: Table, header: str) -> str | None:
    """The delimiter every cell of a column of all-distinct values holds (a `category · detail`
    label column), else None."""
    cells = [t for t in _data(table, header) if t.strip()]
    if len(set(cells)) < len(cells):
        return None
    found = [d for d in ("·", ":") if all(d in t for t in cells)]
    return (
        found[0]
        if len(found) == 1 and not any(o in t for t in cells for o in "·:" if o != found[0])
        else None
    )


# a header that names the group: a printed summary has one row per category, all distinct
# shortcut: English/Spanish words only, add words per language when a source needs them.
_GROUP_HEADER = frozenset("category categories categoria categorias type tipo".split())


def _repeats(table: Table, header: str) -> bool:
    return len(set(_data(table, header))) < len(_data(table, header))


def _group(table: Table, spec: Derivation) -> str:
    """The group column: the LLM's when it names a header whose values repeat or share a
    delimiter (`_sep`), else the one non-numeric column whose values repeat; code
    `group_ambiguous` otherwise (an all-distinct column, unless its header names the group,
    makes every row its own group)."""
    if spec.group_by and (real := _real(table, spec.group_by)):
        grouped = _repeats(table, real) or _sep(table, real) or _GROUP_HEADER & _words((real,))[0]
        return real if grouped else "group_ambiguous"
    rep = [h for h in table.headers if not _numeric(table, h) and _repeats(table, h)]
    return rep[0] if len(rep) == 1 else "group_ambiguous"


def resolve_columns(table: Table, spec: Derivation, tag: str | None) -> Derivation | str:
    """`spec` with every named header replaced by the table's real one (equal after `normalize`),
    the amount column (also of a count with a tag) and group column found from the table's
    structure (`_amount`, `_group`), and `group_sep` set from the group column's values; a
    failure code `column_missing`,
    `column_ambiguous` or `group_ambiguous`. `spec.op` is the op the slot means."""
    fixed: dict[str, object] = {}
    counted = spec.op == "count" and not tag  # a directional count's column is found below
    heads = {"column": spec.column if counted else None}
    heads |= {"minus": spec.minus, "pick": spec.pick} if spec.op in ("diff", "top") else {}
    for name, header in heads.items():
        if header is None:
            continue
        if (real := _real(table, header)) is None:
            return "column_missing"
        fixed[name] = real
    if not counted:
        if (column := _amount(table, spec, tag)) == "column_ambiguous":
            return column
        fixed["column"] = column
    if spec.op == "group_sum":
        if (group := _group(table, spec)) == "group_ambiguous":
            return group
        fixed |= {"group_by": group, "group_sep": _sep(table, group)}
    where = {_real(table, k): v for k, v in spec.where.items()}
    if None in where:
        return "column_missing"
    return spec.model_copy(update=fixed | {"where": where})


def _kept(table: Table, spec: Derivation) -> list[Pair]:
    """The rows left after `exclude_rows` and `where`."""
    return [
        pair
        for pair in table.rows
        if pair[1].row_role == "data"  # carry-forward and total rows are never aggregated
        and pair[1].id not in spec.exclude_rows
        and all(normalize(pair[1].cells[h]) == normalize(v) for h, v in spec.where.items())
    ]


def tagged_column(table: Table, header: str) -> bool:
    """Whether any cell of the column carries a CR/DR tag (separate in/out columns do not)."""
    return any(_TAG.search(row.cells.get(header, "")) for _, row in table.rows)


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
    if spec.op == "top":
        return _top(main, spec, conv, expected_type)
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
        label = pair[1].cells[spec.group_by or ""]
        if spec.group_sep:
            label = label.split(spec.group_sep, 1)[0]
        label = label.strip()
        if not label:
            return "group_label_missing"
        sums[label] = sums.get(label, Decimal(0)) + _value(cell, conv)
        members[label].append((pair, cell))
    ranked = sorted(sums, key=lambda k: -sums[k])  # stable: ties by first seen
    if spec.rank:  # the k-th group alone: its label, or its sum
        if spec.rank > len(ranked):
            return "rank_out_of_range"
        k = ranked[spec.rank - 1]
        if any(sums[n] == sums[k] for n in ranked[max(spec.rank - 2, 0) : spec.rank + 1] if n != k):
            return "rank_tied"  # the rank order among equal sums would be arbitrary
        cited = tuple((h, r, None) for (h, r), _ in members[k])
        if expected_type == "text":
            return Computed(k, cited)
        return Computed(_render(sums[k], [c for _, c in members[k]], conv), cited, sums[k])
    ranked = ranked[: spec.top_n]
    cells = [c for k in ranked for _, c in members[k]]
    amounts = [(k, _render(sums[k], cells, conv)) for k in ranked]
    value = (
        [[k, a] for k, a in amounts]
        if expected_type == "table"
        else [f"{k}: {a}" for k, a in amounts]
    )
    cited = tuple((h, r, i) for i, k in enumerate(ranked) for (h, r), _ in members[k])
    return Computed(value, cited)


def _top(
    main: list[tuple[Pair, _Cell]], spec: Derivation, conv: str, expected_type: str
) -> Computed | str:
    """The `rank`-th row (default 1) by the magnitude of `column`, ties in source order; the slot
    takes that row's `pick` cell."""
    if spec.tag is None and {c.negative for _, c in main} == {True, False}:
        return "mixed_tags"  # a signed column: magnitude alone would rank a receipt as a payment
    ordered = sorted(main, key=lambda x: -abs(_value(x[1], conv)))
    if (spec.rank or 1) > len(ordered):
        return "rank_out_of_range"
    pair, _ = ordered[(spec.rank or 1) - 1]
    cited = ((pair[0], pair[1], None),)
    text = pair[1].cells[spec.pick or spec.column].strip()
    if expected_type not in _NUMERIC_TYPES:
        return Computed(text, cited)
    cell = _parse(text)
    if not isinstance(cell, _Cell):
        return cell or "cell_unparseable"
    if (own := conv if spec.pick in (None, spec.column) else _convention([cell])) is None:
        return "ambiguous_number_format"
    n = _value(cell, own)
    return Computed(_render(n, [cell], own), cited, n)
