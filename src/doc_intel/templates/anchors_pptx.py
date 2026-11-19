from io import BytesIO

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from doc_intel.templates.anchors import find_tokens
from doc_intel.templates.schemas import Anchor, TableRef


def _shapes(shapes):
    for shape in shapes:
        yield shape
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _shapes(shape.shapes)


def _layout_text(slide, shape) -> str:
    for ph in slide.slide_layout.placeholders:
        if ph.placeholder_format.idx == shape.placeholder_format.idx and ph.has_text_frame:
            return ph.text_frame.text
    return ""


def extract(data: bytes) -> list[Anchor]:
    anchors: list[Anchor] = []
    for n, slide in enumerate(Presentation(BytesIO(data)).slides, start=1):
        title = slide.shapes.title.text_frame.text.strip() if slide.shapes.title else ""
        for shape in _shapes(slide.shapes):
            path = [f"Slide {n}", *([title] if title else []), shape.name]
            base = f"pptx:s{n}:sp{shape.shape_id}"
            if shape.has_text_frame:
                own = shape.text_frame.text
                # empty placeholder: the layout prompt ("Click to add ...") is its instruction
                text = own or (_layout_text(slide, shape) if shape.is_placeholder else "")
                tokens = find_tokens(text)
                anchors.append(
                    Anchor(
                        anchor_id=base,
                        kind="shape_text",
                        path=path,
                        text=text,
                        tokens=tokens,
                        is_placeholder=bool(tokens) or not own,
                    )
                )
            elif shape.has_table:
                rows = shape.table.rows
                headers = [c.text.strip() or None for c in rows[0].cells]
                for r, row in enumerate(rows):
                    for c, cell in enumerate(row.cells):
                        if cell.is_spanned:
                            continue
                        text = cell.text
                        tokens = find_tokens(text)
                        anchors.append(
                            Anchor(
                                anchor_id=f"{base}:r{r}c{c}",
                                kind="table_cell",
                                path=path,
                                text=text,
                                tokens=tokens,
                                is_placeholder=bool(tokens) or not text,
                                table=TableRef(row=r, col=c, header=headers[c]),
                            )
                        )
    return anchors
