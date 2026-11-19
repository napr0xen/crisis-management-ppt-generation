from io import BytesIO

from openpyxl import load_workbook
from openpyxl.cell.cell import Cell, MergedCell
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.formula import ArrayFormula

from doc_intel.templates.anchors import find_tokens, too_many_anchors
from doc_intel.templates.schemas import MAX_ANCHORS, Anchor

_MAX_COL, _MAX_ROW = 16384, 1048576  # Excel grid limits (XFD, 1048576)
_SCAN = 100  # shortcut: path context looks at most 100 cells left/up, upgrade if labels sit farther


def _is_formula(cell: Cell | MergedCell) -> bool:
    v = cell.value
    return (
        cell.data_type == "f"
        or isinstance(v, ArrayFormula)
        or (isinstance(v, str) and v.startswith("="))
    )


def _text(cell) -> str:
    return "" if cell is None or cell.value is None else str(cell.value)


def _is_string(cell: Cell | MergedCell | None) -> bool:
    return (
        cell is not None
        and not isinstance(cell, MergedCell)
        and isinstance(cell.value, str)
        and cell.value != ""
        and not _is_formula(cell)
    )


def _nearest(cells, positions) -> str | None:
    return next((cells[p].value for p in positions if _is_string(cells.get(p))), None)


def _candidates(ws) -> set[tuple[int, int]]:
    cells = ws._cells  # existing cells only, so a huge max_row is never scanned
    chosen: set[tuple[int, int]] = set()
    for (r, c), cell in cells.items():
        if isinstance(cell, MergedCell) or _is_formula(cell):
            continue
        if _is_string(cell) and find_tokens(cell.value):
            chosen.add((r, c))
        elif _is_string(cell):  # a label: its right and below neighbours, when empty
            for pos in ((r, c + 1), (r + 1, c)):
                if pos[1] > _MAX_COL or pos[0] > _MAX_ROW:
                    continue
                nb = cells.get(pos)
                if nb is None or (
                    not isinstance(nb, MergedCell) and not _is_formula(nb) and _text(nb) == ""
                ):
                    chosen.add(pos)
    return chosen


def _extract_sheet(ws, chosen: set[tuple[int, int]]) -> list[Anchor]:
    cells = ws._cells
    anchors = []
    for r, c in sorted(chosen):
        text = _text(cells.get((r, c)))
        tokens = find_tokens(text)
        left = _nearest(cells, ((r, k) for k in range(c - 1, max(c - 1 - _SCAN, 0), -1)))
        above = _nearest(cells, ((k, c) for k in range(r - 1, max(r - 1 - _SCAN, 0), -1)))
        anchors.append(
            Anchor(
                anchor_id=f"xlsx:{ws.title}!{get_column_letter(c)}{r}",
                kind="sheet_cell",
                path=[ws.title, *(p for p in (left, above) if p)],
                text=text,
                tokens=tokens,
                is_placeholder=bool(tokens) or not text,
            )
        )
    return anchors


def extract(data: bytes) -> list[Anchor]:
    wb = load_workbook(BytesIO(data), data_only=False)
    chosen = [_candidates(ws) for ws in wb.worksheets]
    total = sum(map(len, chosen))
    if total > MAX_ANCHORS:  # before any path scan or Anchor construction
        raise too_many_anchors(total)
    return [a for ws, c in zip(wb.worksheets, chosen, strict=True) for a in _extract_sheet(ws, c)]
