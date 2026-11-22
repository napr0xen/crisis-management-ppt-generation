"""PPTX surgical writer (system-design § 8): ids resolve exactly as in `templates/anchors_pptx`."""

import re
from io import BytesIO

from lxml import etree
from pptx import Presentation
from pptx.chart.chart import Chart
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.shapes.base import BaseShape

from doc_intel.mapping.schemas import MappingWarning
from doc_intel.mapping.verifier import MONTHS, TYPED_TYPES, is_date
from doc_intel.templates.anchors import find_tokens
from doc_intel.templates.anchors_pptx import keyed_shapes
from doc_intel.writers import WriteItem
from doc_intel.writers.runs import (
    A,
    fill_rows,
    paragraphs_of,
    replace_token,
    texts,
    write_paragraphs,
)

_ID = re.compile(r"^pptx:s(\d+):(sp\d+(?:_\d+)?)(?::r(\d+)c(\d+))?$")


_YEAR = re.compile(r"(?:19|20)\d\d")
_QUARTER = re.compile(r"(?:q[1-4](?: ?'?\d{2,4})?|\d{4} ?q[1-4])", re.IGNORECASE)
_ISO_MONTH = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])")
_MONTH_YEAR = re.compile(r"([^\W\d_]+)\.?(?: \d{2,4})?")
_MAX_PIE, _MAX_COLUMNS, _MAX_LABEL = 6, 8, 20


def _time_like(label: str) -> bool:
    """A date, year, quarter, ISO month, or month name (optionally with a year)."""
    label = label.strip()
    m = _MONTH_YEAR.fullmatch(label)
    return bool(
        _YEAR.fullmatch(label)
        or _QUARTER.fullmatch(label)
        or _ISO_MONTH.fullmatch(label)
        or (m and m[1].casefold() in MONTHS)
        or is_date(label)
    )


def chart_type_for(categories: list[str], series: list[list[float]]) -> XL_CHART_TYPE:
    """Shares of a whole -> PIE, a time axis -> LINE, many or long labels -> BAR, else COLUMN."""
    # shortcut: a rule of thumb on the data's shape only, a template-specified type is not read.
    if (
        len(series) == 1
        and 2 <= len(categories) <= _MAX_PIE
        and all(v > 0 for v in series[0])
        and abs(sum(series[0]) - 100) <= 1
    ):
        return XL_CHART_TYPE.PIE
    if len(categories) >= 3 and all(_time_like(c) for c in categories):
        return XL_CHART_TYPE.LINE
    if len(categories) > _MAX_COLUMNS or any(len(c) > _MAX_LABEL for c in categories):
        return XL_CHART_TYPE.BAR_CLUSTERED
    return XL_CHART_TYPE.COLUMN_CLUSTERED


def _data(rows: list[list[str]], names: list[str]) -> CategoryChartData:
    """One series per name; a row without that column plots no point."""
    data = CategoryChartData()
    data.categories = [row[0] for row in rows]
    for i, name in enumerate(names):
        data.add_series(name, [float(r[i + 1]) if i + 1 < len(r) else None for r in rows])
    return data


def _set_title_marker(chart: Chart, marker: str) -> None:
    chart.has_title = True
    chart.chart_title.text_frame.text = marker


def _fill_chart(chart: Chart, rows: list[str] | list[list[str]]) -> None:
    names = [s.name for s in chart.series]
    if not rows or isinstance(rows[0], str):  # a marker: an empty chart, never sample numbers
        data = CategoryChartData()
        data.categories = [""]
        for name in names:
            data.add_series(name, (None,))
        chart.replace_data(data)
        _set_title_marker(chart, "; ".join(rows))
        return
    chart.replace_data(_data(rows, names))
    # shortcut: only bracketed tokens in the title are unwrapped (the template's own words stay),
    # a bare blank like "____" stays, upgrade by naming the title as its own field.
    if chart.has_title and chart.chart_title.has_text_frame:
        frame = chart.chart_title.text_frame
        for token in find_tokens(frame.text):
            if token.startswith(("{{", "[", "<")):
                replace_token(paragraphs_of(frame._txBody, A), token, token.strip("{}[]<> "), A)


def _insert_chart(shape: BaseShape, field_label: str, columns: list[str], rows) -> None:
    if not rows or isinstance(rows[0], str):  # a marker goes where the placeholder text is
        write_paragraphs(paragraphs_of(shape.text_frame._txBody, A), rows, A)
        return
    n = len(rows[0]) - 1
    names = [columns[i] if i < len(columns) else field_label for i in range(n)]
    kind = chart_type_for(
        [r[0] for r in rows], [[float(r[i + 1]) for r in rows if i + 1 < len(r)] for i in range(n)]
    )
    # shortcut: a placeholder inside a group shape gets the chart at its group child coordinates.
    frame = shape.part.slide.shapes.add_chart(
        kind, shape.left, shape.top, shape.width, shape.height, _data(rows, names)
    )
    frame.chart.has_legend = n > 1 or kind == XL_CHART_TYPE.PIE
    shape._element.getparent().remove(shape._element)


def _resolve(prs, anchor_id: str, mode: str = "replace_text") -> tuple[BaseShape, re.Match]:
    m = _ID.match(anchor_id)
    if m is None or not 1 <= int(m[1]) <= len(prs.slides):
        raise KeyError("anchor_unresolved")
    shape = next(
        (
            s
            for key, s in keyed_shapes(prs.slides[int(m[1]) - 1].shapes)
            if key == m[2] and _kind_ok(s, mode, bool(m[3]))
        ),
        None,
    )
    if shape is None or (
        m[3] is not None
        and (int(m[3]) >= len(shape.table.rows) or int(m[4]) >= len(shape.table.columns))
    ):
        raise KeyError("anchor_unresolved")
    return shape, m


def _kind_ok(shape: BaseShape, mode: str, cell: bool) -> bool:
    if cell:
        return shape.has_table
    return shape.has_chart if mode == "fill_chart" else shape.has_text_frame


def write(template: bytes, items: list[WriteItem]) -> tuple[bytes, list[MappingWarning]]:
    prs = Presentation(BytesIO(template))
    warnings: list[MappingWarning] = []
    # Resolve every target before the first write so no write can shift a later lookup.
    targets = [_resolve(prs, item.field.anchor_id, item.field.write_mode) for item in items]
    for item, (shape, m) in zip(items, targets, strict=True):
        field = item.field
        if field.write_mode == "fill_chart":
            _fill_chart(shape.chart, item.content)
            continue
        if field.write_mode == "insert_chart":
            _insert_chart(shape, field.label, field.constraints.columns or [], item.content)
            continue
        if m[3] is None:
            container = shape.text_frame._txBody
        else:
            container = shape.table.cell(int(m[3]), int(m[4]))._tc.txBody
        if field.write_mode == "fill_table_rows":
            table = shape.table

            def cells_of(row: int, table=table) -> list[etree._Element]:
                return [c._tc.txBody for c in table.rows[row].cells if not c.is_spanned]

            if fill_rows(cells_of, len(table.rows), item.content, A):
                warnings.append(MappingWarning(field_id=field.field_id, code="table_overflow"))
        elif field.write_mode == "replace_token":
            token = field.constraints.token or ""
            replace_token(
                paragraphs_of(container, A),
                token,
                "; ".join(texts(item.content)),
                A,
                typed=field.expected_type in TYPED_TYPES,
            )
        else:
            write_paragraphs(paragraphs_of(container, A), texts(item.content), A)
    out = BytesIO()
    prs.save(out)
    return out.getvalue(), warnings
