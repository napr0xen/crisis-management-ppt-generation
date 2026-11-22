import re
from collections import Counter
from collections.abc import Iterable, Iterator
from io import BytesIO

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER
from pptx.shapes.base import BaseShape
from pptx.slide import Slide

from doc_intel.templates.anchors import cell_context, find_tokens, has_letter, static_text
from doc_intel.templates.schemas import Anchor, ChartRef, TableRef


def _shapes(shapes: Iterable[BaseShape]) -> Iterator[BaseShape]:
    for shape in shapes:
        yield shape
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _shapes(shape.shapes)


def keyed_shapes(shapes: Iterable[BaseShape]) -> Iterator[tuple[str, BaseShape]]:
    # Real decks reuse shape ids, so the k-th text/table/chart shape per id gets an "_k" suffix.
    seen: Counter[tuple[int, str]] = Counter()
    for shape in _shapes(shapes):
        kind = (
            "text"
            if shape.has_text_frame
            else "table"
            if shape.has_table
            else "chart"
            if getattr(shape, "has_chart", False)
            else None
        )
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


_SAME_SIZE = 0.02


def _area(box: tuple[int, int, int, int]) -> int:
    return (box[2] - box[0]) * (box[3] - box[1])


def _size(box: tuple[int, int, int, int]) -> tuple[int, int]:
    return box[2] - box[0], box[3] - box[1]


def _contains(outer: tuple[int, int, int, int], inner: tuple[int, int, int, int]) -> bool:
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
        and _area(outer) > _area(inner)
    )


def _reading_order(members: list[int], boxes: list[tuple[int, int, int, int]]) -> list[int]:
    """Rows by top (a new row when the top is half a member height below the row's first),
    left to right inside a row."""
    ordered = sorted(members, key=lambda i: boxes[i][1])
    bands: list[list[int]] = []
    for i in ordered:
        first = bands[-1][0] if bands else None
        if first is not None and boxes[i][1] < boxes[first][1] + (boxes[i][3] - boxes[i][1]) / 2:
            bands[-1].append(i)
        else:
            bands.append([i])
    return [i for band in bands for i in sorted(band, key=lambda j: boxes[j][0])]


def _card_ids(keyed: list[tuple[str, BaseShape]], shapes: list[BaseShape]) -> dict[str, str]:
    """anchor key -> "set.member" for shapes inside a card: one of >= 2 containers of equal size
    (within 2%) that each hold >= 2 anchor shapes."""
    # shortcut: tiles drawn without a container shape are not detected, group them by position
    # if real decks need it.
    boxes = [b if (b := _box(s)) else (0, 0, 0, 0) for s in shapes]
    anchor_keys = {
        key: next(i for i, s in enumerate(shapes) if s == shape)
        for key, shape in keyed
        if shape.has_table or (shape.has_text_frame and shape.text_frame.text.strip())
    }
    container: dict[str, int] = {}
    for key, i in anchor_keys.items():
        if _box(shapes[i]) is None:
            continue
        around = [j for j in range(len(shapes)) if j != i and _contains(boxes[j], boxes[i])]
        if around:
            container[key] = min(around, key=lambda j: _area(boxes[j]))
    held = Counter(container.values())
    sets: list[list[int]] = []  # containers of one size
    for j in sorted(c for c, n in held.items() if n >= 2):
        w, h = _size(boxes[j])
        for members in sets:
            mw, mh = _size(boxes[members[0]])
            if abs(w - mw) <= _SAME_SIZE * max(w, mw) and abs(h - mh) <= _SAME_SIZE * max(h, mh):
                members.append(j)
                break
        else:
            sets.append([j])
    cards: dict[int, str] = {}
    ordered = [_reading_order(m, boxes) for m in sets if len(m) >= 2]
    for n, members in enumerate(
        sorted(ordered, key=lambda m: (boxes[m[0]][1], boxes[m[0]][0])), start=1
    ):
        cards |= {j: f"{n}.{k}" for k, j in enumerate(members, start=1)}
    return {key: cards[j] for key, j in container.items() if j in cards}


_LABEL_EDGE = " |:\u00b7-\u2013\u2014,;/"


def _own_label(text: str, tokens: list[str]) -> str | None:
    """The shape's own label beside its tokens: the one run of text around them that has a letter
    (a token line, then the line "Requirement"); a sentence with a token in the middle
    (`Date [x] status`) has two runs and no label."""
    runs = [r for r in re.split("|".join(map(re.escape, tokens)), text) if has_letter(r)]
    label = (static_text(runs[0]) or "").strip(_LABEL_EDGE) if len(runs) == 1 else ""
    return label if has_letter(label) else None


def _chart_anchor(
    shape: BaseShape, base: str, path: list[str], pos: tuple[int, int] | None, context: str | None
) -> Anchor | None:
    chart = shape.chart
    try:
        kind = chart.chart_type.name
        categories = [str(c) for c in chart.plots[0].categories]
    except (IndexError, NotImplementedError, ValueError):  # no plot, or a type python-pptx lacks
        return None
    # shortcut: XY and bubble charts have no category axis and are not fields, add them when a
    # template needs numeric x values filled.
    series = [s.name for s in chart.series]
    if kind.startswith(("XY_SCATTER", "BUBBLE")) or not series:  # no series: nothing to fill
        return None
    title = chart.chart_title.text_frame.text if chart.has_title else ""
    parts = [f"Chart {kind}", *([f"title: {title}"] if title else [])]
    parts += [f"series: {', '.join(series)}", f"categories: {', '.join(categories)}"]
    text = "; ".join(parts)
    tokens = find_tokens("\n".join([title, *categories, *series]))
    return Anchor(
        anchor_id=base,
        kind="chart",
        path=path,
        text=text[:200],
        tokens=tokens,
        is_placeholder=bool(tokens),
        pos=pos,
        context=context,
        chart=ChartRef(chart_type=kind, series=series, categories=len(categories)),
    )


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
        cards = _card_ids(keyed, list(_shapes(slide.shapes)))
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
                ctx = _context(shape, headings, prs.slide_width, prs.slide_height)
                # the shape's own label is the most specific; a section or card heading stays
                if tokens and (own_label := _own_label(text, tokens)):
                    ctx = f"{ctx} / {own_label}" if has_letter(ctx) else own_label
                anchors.append(
                    Anchor(
                        anchor_id=base,
                        kind="shape_text",
                        path=path,
                        text=text,
                        tokens=tokens,
                        is_placeholder=bool(tokens) or not own,
                        pos=pos,
                        context=ctx,
                        card=cards.get(key),
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
                                card=cards.get(key),
                            )
                        )
            elif anchor := (
                _chart_anchor(
                    shape,
                    base,
                    path,
                    pos,
                    _context(shape, headings, prs.slide_width, prs.slide_height),
                )
                if shape.has_chart
                else None
            ):
                # shortcut: a chart has no card, so repeating chart tiles are separate fields.
                anchors.append(anchor)
    return anchors
