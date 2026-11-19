"""XLSX surgical writer (ADR-006): patches sheet XML inside the zip, never re-saves via openpyxl."""

import posixpath
import re
import zipfile
from datetime import date, timedelta
from io import BytesIO

from lxml import etree
from openpyxl.utils import column_index_from_string

from doc_intel.mapping.schemas import MappingWarning
from doc_intel.writers import WriteItem

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
_REF = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)$")
# CT_Workbook children that precede calcPr.
_BEFORE_CALC = {
    "fileVersion",
    "fileSharing",
    "workbookPr",
    "workbookProtection",
    "bookViews",
    "sheets",
    "functionGroups",
    "externalReferences",
    "definedNames",
}
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)


def _q(name: str) -> str:
    return f"{{{_NS}}}{name}"


def _parse(data: bytes) -> etree._Element:
    return etree.fromstring(data, _PARSER)


def _sheet_parts(zf: zipfile.ZipFile, workbook: etree._Element) -> dict[str, str]:
    rels = {
        r.get("Id"): r.get("Target")
        for r in _parse(zf.read("xl/_rels/workbook.xml.rels")).iter(_PKG_REL)
    }
    parts = {}
    for sheet in workbook.iter(_q("sheet")):
        target = rels[sheet.get(f"{{{_REL_NS}}}id")]
        parts[sheet.get("name")] = (
            target[1:] if target.startswith("/") else posixpath.normpath(f"xl/{target}")
        )
    return parts


def _split(anchor_id: str, parts: dict[str, str]) -> tuple[str, str, int, int]:
    """(sheet part, cell ref, row, column); anchor ids are `xlsx:{title}!{ref}`."""
    body = anchor_id.removeprefix("xlsx:")
    title, _, ref = body.rpartition("!")
    m = _REF.match(ref)
    if not anchor_id.startswith("xlsx:") or title not in parts or m is None:
        raise KeyError("anchor_unresolved")  # never the title: it is template text
    return parts[title], ref, int(m[2]), column_index_from_string(m[1])


def _col(cell: etree._Element) -> int:
    return column_index_from_string(_REF.match(cell.get("r"))[1])  # type: ignore[index]


def _find_or_create(sheet_data: etree._Element, ref: str, row_n: int, col_n: int):
    # shortcut: rows and cells without an r attribute are not supported, upgrade if a producer
    # emits them.
    row = next((r for r in sheet_data.iter(_q("row")) if int(r.get("r")) >= row_n), None)
    if row is None or int(row.get("r")) != row_n:
        new = etree.Element(_q("row"), r=str(row_n))
        if row is None:
            sheet_data.append(new)
        else:
            row.addprevious(new)
        row = new
    cell = next((c for c in row.iter(_q("c")) if _col(c) >= col_n), None)
    if cell is None or _col(cell) != col_n:
        new = etree.Element(_q("c"), r=ref)
        if cell is None:
            row.append(new)
        else:
            cell.addprevious(new)
        cell = new
    return cell


_DATE_BUILTIN = {*range(14, 23), 45, 46, 47}
_QUOTED = re.compile(r'"[^"]*"|\[[^\]]*\]|\\.')


def _date_styles(zf: zipfile.ZipFile) -> set[int]:
    """cellXfs indexes whose number format is a date; styles.xml is read, never written."""
    if "xl/styles.xml" not in zf.namelist():
        return set()
    styles = _parse(zf.read("xl/styles.xml"))
    # shortcut: a custom format counts as a date when it has a d, m or y outside quotes and
    # brackets, so "Sheet m" style oddities are approximated; upgrade with a real format parser.
    custom = {
        int(f.get("numFmtId")): bool(re.search("[dmy]", _QUOTED.sub("", f.get("formatCode", ""))))
        for f in styles.iter(_q("numFmt"))
    }
    xfs = styles.find(_q("cellXfs"))
    return {
        i
        for i, xf in enumerate(xfs if xfs is not None else [])
        if int(xf.get("numFmtId", "0")) in _DATE_BUILTIN
        or custom.get(int(xf.get("numFmtId", "0")), False)
    }


def _set_value(cell: etree._Element, content: object, *, date_style: bool, is_date: bool) -> None:
    for tag in ("v", "is"):
        for old in cell.findall(_q(tag)):
            cell.remove(old)
    cell.attrib.pop("t", None)
    if is_date and not date_style:  # a bare serial would render as a number
        content = [(date(1899, 12, 30) + timedelta(days=int(content))).isoformat()]  # type: ignore[arg-type]
    if isinstance(content, list):
        cell.set("t", "inlineStr")
        t = etree.SubElement(etree.SubElement(cell, _q("is")), _q("t"))
        t.text = "; ".join(content)
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    else:
        v = etree.SubElement(cell, _q("v"))
        v.text = str(int(content)) if float(content).is_integer() else repr(float(content))  # type: ignore[arg-type]


def _full_calc_on_load(workbook: etree._Element) -> None:
    calc = workbook.find(_q("calcPr"))
    if calc is None:
        calc = etree.Element(_q("calcPr"))
        last = [c for c in workbook if etree.QName(c).localname in _BEFORE_CALC][-1]
        last.addnext(calc)
    calc.set("fullCalcOnLoad", "1")


def write(template: bytes, items: list[WriteItem]) -> tuple[bytes, list[MappingWarning]]:
    with zipfile.ZipFile(BytesIO(template)) as zf:
        return _write(zf, items)


def _write(zf: zipfile.ZipFile, items: list[WriteItem]) -> tuple[bytes, list[MappingWarning]]:
    workbook = _parse(zf.read("xl/workbook.xml"))
    parts = _sheet_parts(zf, workbook)
    date_styles = _date_styles(zf) if any(i.field.expected_type == "date" for i in items) else set()
    sheets: dict[str, etree._Element] = {}
    warnings: list[MappingWarning] = []
    for item in items:
        part, ref, row_n, col_n = _split(item.field.anchor_id, parts)
        if part not in sheets:
            sheets[part] = _parse(zf.read(part))
        sheet_data = sheets[part].find(_q("sheetData"))
        cell = _find_or_create(sheet_data, ref, row_n, col_n)
        if cell.find(_q("f")) is not None:  # never overwrite a formula
            warnings.append(MappingWarning(field_id=item.field.field_id, code="formula_cell"))
            continue
        _set_value(
            cell,
            item.content,
            date_style=int(cell.get("s", "0")) in date_styles,
            is_date=item.field.expected_type == "date" and not isinstance(item.content, list),
        )
    _full_calc_on_load(workbook)
    patched = {"xl/workbook.xml": workbook, **sheets}
    out = BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for info in zf.infolist():
            root = patched.get(info.filename)
            data = (
                etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
                if root is not None
                else zf.read(info.filename)
            )
            dst.writestr(info, data, compress_type=info.compress_type)
    return out.getvalue(), warnings
