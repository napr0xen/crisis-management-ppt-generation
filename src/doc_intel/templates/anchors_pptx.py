from collections import Counter
from collections.abc import Iterable, Iterator
from io import BytesIO

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER
from pptx.shapes.base import BaseShape
from pptx.slide import Slide

from doc_intel.templates.anchors import cell_context, find_tokens, static_text
from doc_intel.templates.schemas import Anchor, TableRef


def _shapes(shapes: Iterable[BaseShape]) -> Iterator[BaseShape]:
    for shape in shapes:
        yield shape
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _shapes(shape.shapes)


def keyed_shapes(shapes: Iterable[BaseShape]) -> Iterator[tuple[str, BaseShape]]:
    # Real decks reuse shape ids, so the k-th text/table shape per id gets an "_k" suffix.
    seen: Counter[tuple[int, str]] = Counter()
    for shape in _shapes(shapes):
        kind = "text" if shape.has_text_frame else "table" if shape.has_table else None
        if kind is None:
            continue
        seen[shape.shape_id, kind] += 1
        k = seen[shape.shape_id, kind]
        yield f"sp{shape.shape_id}" if k == 1 else f"sp{shape.shape_id}_{k}", shape


def _layout_text(slide: Slide, shape: BaseShape) -> str:
    for ph in slide.slide_layout.placeholders:
        if ph.placeholder_format.idx == shape.placeholder_format.idx and ph.has_text_frame:
            return ph.text_frame.text
    return ""


# slide furniture: never a field unless the author left a token in it
_FURNITURE = {
    PP_PLACEHOLDER.SLIDE_NUMBER,
    PP_PLACEHOLDER.FOOTER,
    PP_PLACEHOLDER.DATE,
    PP_PLACEHOLDER.HEADER,
}


def _pos(shape: BaseShape, width: int, height: int) -> tuple[int, int] | None:
    # shortcut: group children use the group's child coordinates (equal to slide coordinates
    # unless the group was scaled), transform via chOff/chExt if scaled groups mislead the IR.
    if None in (shape.left, shape.top, shape.width, shape.height) or not width or not height:
        return None
    x = (shape.left + shape.width / 2) * 100 / width
    y = (shape.top + shape.height / 2) * 100 / height
    return min(100, max(0, round(x))), min(100, max(0, round(y)))


def _box(shape: BaseShape) -> tuple[int, int, int, int] | None:
    if None in (shape.left, shape.top, shape.width, shape.height):
        return None
    return shape.left, shape.top, shape.left + shape.width, shape.top + shape.height


def _context(
    target: BaseShape, headings: list[tuple[str, BaseShape]], width: int, height: int
) -> str | None:
    """The nearest static text above (or left of) the target, else None. Ties go to above."""
    # shortcut: group children use the group's child coordinates, like _pos.
    t = _box(target)
    if t is None:
        return None
    tl, tt, tr, tb = t
    best: tuple[int, int, str] | None = None  # (gap, 0 above / 1 left, text)
    for text, shape in headings:
        h = _box(shape)
        if h is None or shape == target:
            continue
        hl, ht, hr, hb = h
        if ht < tt and hb <= tt + (tb - tt) / 2 and min(hr, tr) > max(hl, tl):
            gap, side, limit = max(0, tt - hb), 0, height
        elif hl < tl and hr <= tl + (tr - tl) / 2 and min(hb, tb) > max(ht, tt):
            gap, side, limit = max(0, tl - hr), 1, width
        else:
            continue
        if gap <= limit / 5 and (best is None or (gap, side) < best[:2]):
            best = (gap, side, text)
    return best[2] if best else None


def extract(data: bytes) -> list[Anchor]:
    anchors: list[Anchor] = []
    prs = Presentation(BytesIO(data))
    for n, slide in enumerate(prs.slides, start=1):
        title = slide.shapes.title.text_frame.text.strip() if slide.shapes.title else ""
        keyed = list(keyed_shapes(slide.shapes))
        slide_title = slide.shapes.title
        headings = [
            (text, shape)
            for _, shape in keyed
            if shape.has_text_frame
            and shape != slide_title
            and (text := static_text(shape.text_frame.text))
        ]
        for key, shape in keyed:
            path = [f"Slide {n}", *([title] if title else []), shape.name]
            base = f"pptx:s{n}:{key}"
            pos = _pos(shape, prs.slide_width, prs.slide_height)
            if shape.has_text_frame:
                own = shape.text_frame.text
                if not own.strip() and not shape.is_placeholder:
                    continue  # decorative box, icon or badge
                if (
                    shape.is_placeholder
                    and shape.placeholder_format.type in _FURNITURE
                    and not find_tokens(own)
                ):
                    continue
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
                        pos=pos,
                        context=_context(shape, headings, prs.slide_width, prs.slide_height),
                    )
                )
            elif shape.has_table:
                rows = shape.table.rows
                headers = [c.text.strip() or None for c in rows[0].cells]
                for r, row in enumerate(rows):
                    texts = [cell.text for cell in row.cells]
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
                                pos=pos,
                                context=cell_context(texts, c, headers[c]) if r else None,
                            )
                        )
    return anchors
