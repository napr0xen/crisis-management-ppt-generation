from io import BytesIO

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.formula import ArrayFormula

from doc_intel.templates.anchors import find_tokens
from doc_intel.templates.schemas import Anchor

_SCAN = 100  # shortcut: path context looks at most 100 cells left/up, upgrade if labels sit farther


def _is_formula(cell) -> bool:
    v = cell.value
    return (
        cell.data_type == "f"
        or isinstance(v, ArrayFormula)
        or (isinstance(v, str) and v.startswith("="))
    )


def _text(cell) -> str:
    return "" if cell is None or cell.value is None else str(cell.value)


def _is_string(cell) -> bool:
    return (
        cell is not None
        and not isinstance(cell, MergedCell)
        and isinstance(cell.value, str)
        and cell.value != ""
        and not _is_formula(cell)
    )


def _nearest(cells, positions) -> str | None:
    return next((cells[p].value for p in positions if _is_string(cells.get(p))), None)


def _extract_sheet(ws) -> list[Anchor]:
    cells = ws._cells  # existing cells only, so a huge max_row is never scanned
    chosen: set[tuple[int, int]] = set()
    for (r, c), cell in cells.items():
        if isinstance(cell, MergedCell) or _is_formula(cell):
            continue
        if _is_string(cell) and find_tokens(cell.value):
            chosen.add((r, c))
        elif _is_string(cell):  # a label: its right and below neighbours, when empty
            for pos in ((r, c + 1), (r + 1, c)):
                nb = cells.get(pos)
                if nb is None or (
                    not isinstance(nb, MergedCell) and not _is_formula(nb) and _text(nb) == ""
                ):
                    chosen.add(pos)
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
    return [a for ws in wb.worksheets for a in _extract_sheet(ws)]
