"""PPTX surgical writer (system-design § 8): ids resolve exactly as in `templates/anchors_pptx`."""

import re
from io import BytesIO

from lxml import etree
from pptx import Presentation
from pptx.shapes.base import BaseShape

from doc_intel.mapping.schemas import MappingWarning
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


def _resolve(prs, anchor_id: str) -> tuple[BaseShape, re.Match]:
    m = _ID.match(anchor_id)
    if m is None or not 1 <= int(m[1]) <= len(prs.slides):
        raise KeyError("anchor_unresolved")
    shape = next(
        (
            s
            for key, s in keyed_shapes(prs.slides[int(m[1]) - 1].shapes)
            if key == m[2] and (s.has_table if m[3] else s.has_text_frame)
        ),
        None,
    )
    if shape is None or (
        m[3] is not None
        and (int(m[3]) >= len(shape.table.rows) or int(m[4]) >= len(shape.table.columns))
    ):
        raise KeyError("anchor_unresolved")
    return shape, m


def write(template: bytes, items: list[WriteItem]) -> tuple[bytes, list[MappingWarning]]:
    prs = Presentation(BytesIO(template))
    warnings: list[MappingWarning] = []
    # Resolve every target before the first write so no write can shift a later lookup.
    targets = [_resolve(prs, item.field.anchor_id) for item in items]
    for item, (shape, m) in zip(items, targets, strict=True):
        field = item.field
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
            replace_token(paragraphs_of(container, A), token, "; ".join(texts(item.content)), A)
        else:
            write_paragraphs(paragraphs_of(container, A), texts(item.content), A)
    out = BytesIO()
    prs.save(out)
    return out.getvalue(), warnings
