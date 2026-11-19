import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from lxml import etree
from openpyxl import Workbook, load_workbook

from doc_intel.mapping.schemas import MappingWarning
from doc_intel.templates.schemas import Constraints, IRField
from doc_intel.writers import WriteItem
from doc_intel.writers.xlsx_writer import write
from tests.fixtures.templates import xlsx_template

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
GOLDEN = Path(__file__).parents[1] / "fixtures/templates/xlsx.anchors.json"
SHEET1 = "xl/worksheets/sheet1.xml"


def _item(anchor_id, content, fid="f1", etype="text") -> WriteItem:
    field = IRField(
        field_id=fid,
        anchor_id=anchor_id,
        section_path=[],
        label="L",
        description="",
        expected_type=etype,
        required=True,
        write_mode="fill_cell",
        constraints=Constraints(),
    )
    return WriteItem(field, content)


def _zip(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(BytesIO(data))


def _cell(data: bytes, ref: str, part: str = SHEET1):
    root = etree.fromstring(_zip(data).read(part))
    return root.find(f".//{NS}c[@r='{ref}']")


def test_only_touched_sheet_and_workbook_change_and_zip_layout_kept():
    tpl = xlsx_template()
    snapshot = bytes(tpl)
    out, warnings = write(tpl, [_item("xlsx:Summary!B2", ["x"])])
    assert tpl == snapshot and out is not tpl and warnings == []
    a, b = _zip(tpl), _zip(out)
    assert [i.filename for i in a.infolist()] == [i.filename for i in b.infolist()]
    assert [i.compress_type for i in a.infolist()] == [i.compress_type for i in b.infolist()]
    changed = {n for n in a.namelist() if a.read(n) != b.read(n)}
    assert changed == {SHEET1, "xl/workbook.xml"}


def test_full_calc_on_load_set_in_schema_position():
    out, _ = write(xlsx_template(), [_item("xlsx:Summary!B2", ["x"])])
    wb = etree.fromstring(_zip(out).read("xl/workbook.xml"))
    calc = wb.findall(f"{NS}calcPr")
    assert len(calc) == 1 and calc[0].get("fullCalcOnLoad") == "1"
    names = [etree.QName(c).localname for c in wb]
    assert names.index("calcPr") > names.index("sheets")


def test_existing_calcpr_is_updated_not_duplicated():
    out1, _ = write(xlsx_template(), [_item("xlsx:Summary!B2", ["x"])])
    out2, _ = write(out1, [_item("xlsx:Summary!B3", ["y"])])
    wb = etree.fromstring(_zip(out2).read("xl/workbook.xml"))
    assert len(wb.findall(f"{NS}calcPr")) == 1


def test_calcpr_goes_before_olesize_and_after_defined_names():
    tpl = xlsx_template()
    z = _zip(tpl)
    wb = etree.fromstring(z.read("xl/workbook.xml"))
    calc = wb.find(f"{NS}calcPr")
    if calc is not None:
        wb.remove(calc)
    names = etree.SubElement(wb, f"{NS}definedNames")
    etree.SubElement(names, f"{NS}definedName", name="X").text = "Summary!$A$1"
    etree.SubElement(wb, f"{NS}oleSize", ref="A1")
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for i in z.infolist():
            data = z.read(i.filename)
            out.writestr(i, etree.tostring(wb) if i.filename == "xl/workbook.xml" else data)
    result, _ = write(buf.getvalue(), [_item("xlsx:Summary!B2", ["x"])])
    order = [
        etree.QName(c).localname for c in etree.fromstring(_zip(result).read("xl/workbook.xml"))
    ]
    assert order[-3:] == ["definedNames", "calcPr", "oleSize"]


def _styled_shared_string_template() -> bytes:
    """A4 becomes a styled shared-string cell, as Excel writes them."""
    src = _zip(xlsx_template())
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == SHEET1:
                old = b'<c r="A4" t="inlineStr"><is><t>Total</t></is></c>'
                assert old in data
                data = data.replace(old, b'<c r="A4" s="3" t="s"><v>0</v></c>')
            out.writestr(info, data)
        sst = b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Total</t></si></sst>'
        out.writestr("xl/sharedStrings.xml", sst)
    return buf.getvalue()


def test_string_written_inline_style_kept_shared_strings_untouched():
    tpl = _styled_shared_string_template()
    assert _cell(tpl, "A4").get("s") == "3"
    out, _ = write(tpl, [_item("xlsx:Summary!A4", ["Totals"])])
    cell = _cell(out, "A4")
    assert cell.get("t") == "inlineStr" and cell.get("s") == "3"
    assert cell.find(f"{NS}is/{NS}t").text == "Totals"
    assert cell.find(f"{NS}v") is None
    assert _zip(tpl).read("xl/sharedStrings.xml") == _zip(out).read("xl/sharedStrings.xml")


def test_style_of_number_cell_kept():
    tpl = _styled_shared_string_template()
    out, _ = write(tpl, [_item("xlsx:Summary!A4", 7, etype="number")])
    cell = _cell(out, "A4")
    assert cell.get("s") == "3" and cell.get("t") is None and cell.find(f"{NS}v").text == "7"


def test_existing_shared_string_cell_is_overwritten_as_inline():
    out, _ = write(xlsx_template(), [_item("xlsx:Summary!A2", ["New"])])
    cell = _cell(out, "A2")
    assert cell.get("t") == "inlineStr" and cell.find(f"{NS}v") is None


@pytest.mark.parametrize(
    ("value", "text"), [(5, "5"), (5.0, "5"), (0.25, "0.25"), (45356, "45356"), (-3, "-3")]
)
def test_numbers_written_as_v_without_type(value, text):
    out, _ = write(xlsx_template(), [_item("xlsx:Summary!B2", value, etype="number")])
    cell = _cell(out, "B2")
    assert cell.get("t") is None and cell.find(f"{NS}v").text == text
    assert load_workbook(BytesIO(out))["Summary"]["B2"].value == float(text) or int(text)


def test_formula_cell_untouched_with_warning():
    tpl = xlsx_template()
    out, warnings = write(tpl, [_item("xlsx:Summary!B4", ["x"], fid="calc")])
    assert warnings == [MappingWarning(field_id="calc", code="formula_cell")]
    assert etree.tostring(_cell(out, "B4")) == etree.tostring(_cell(tpl, "B4"))
    assert load_workbook(BytesIO(out))["Summary"]["B4"].value == "=SUM(B2:B3)"


def test_value_starting_with_equals_is_a_string_not_a_formula():
    out, _ = write(xlsx_template(), [_item("xlsx:Summary!B2", ["=1+1"])])
    cell = _cell(out, "B2")
    assert cell.find(f"{NS}f") is None and cell.find(f"{NS}is/{NS}t").text == "=1+1"


def test_missing_row_and_cell_created_in_order():
    tpl = xlsx_template()
    out, _ = write(
        tpl,
        [
            _item("xlsx:Summary!F20", ["far"]),
            _item("xlsx:Summary!F10", ["mid"]),
            _item("xlsx:Summary!C6", ["c6"]),
            _item("xlsx:Summary!AA6", ["aa6"]),
        ],
    )
    root = etree.fromstring(_zip(out).read(SHEET1))
    rows = [int(r.get("r")) for r in root.iter(f"{NS}row")]
    assert rows == sorted(rows) and {10, 20} <= set(rows)
    row6 = root.find(f".//{NS}row[@r='6']")
    assert [c.get("r") for c in row6] == ["A6", "C6", "AA6"]
    ws = load_workbook(BytesIO(out))["Summary"]
    assert (ws["F20"].value, ws["F10"].value, ws["AA6"].value) == ("far", "mid", "aa6")


def test_sheet_title_with_bang_and_untouched_other_sheet():
    tpl = xlsx_template()
    out, _ = write(tpl, [_item("xlsx:Q1!Data!B1", ["v"])])
    a, b = _zip(tpl), _zip(out)
    assert a.read(SHEET1) == b.read(SHEET1)
    assert load_workbook(BytesIO(out))["Q1!Data"]["B1"].value == "v"


def test_structure_preserved():
    tpl = xlsx_template()
    out, _ = write(tpl, [_item("xlsx:Summary!B2", ["x"])])
    a, b = load_workbook(BytesIO(tpl)), load_workbook(BytesIO(out))
    assert a.sheetnames == b.sheetnames
    assert list(a.defined_names) == list(b.defined_names)
    assert b["Summary"].merged_cells.ranges == a["Summary"].merged_cells.ranges


def test_list_content_joined_and_markers():
    out, _ = write(xlsx_template(), [_item("xlsx:Summary!B2", ["[MISSING: L]"])])
    assert load_workbook(BytesIO(out))["Summary"]["B2"].value == "[MISSING: L]"
    out, _ = write(xlsx_template(), [_item("xlsx:Summary!B2", ["a", "b"])])
    assert load_workbook(BytesIO(out))["Summary"]["B2"].value == "a; b"


@pytest.mark.parametrize(
    "anchor",
    ["xlsx:Nope!A1", "xlsx:Summary", "xlsx:Summary!1A", "pptx:s1:sp1", "xlsx:Summary!B0"],
)
def test_unknown_anchor_is_keyerror(anchor):
    with pytest.raises(KeyError) as exc:
        write(xlsx_template(), [_item(anchor, ["x"])])
    assert exc.value.args == ("anchor_unresolved",)


def test_round_trip_every_golden_anchor():
    ids = [a["anchor_id"] for a in json.loads(GOLDEN.read_text())["anchors"]]
    values = {aid: f"V{i:04d}" for i, aid in enumerate(ids, start=1)}
    out, warnings = write(
        xlsx_template(), [_item(a, [v], fid=f"f{i}") for i, (a, v) in enumerate(values.items())]
    )
    assert warnings == []
    wb = load_workbook(BytesIO(out))
    for anchor, value in values.items():
        sheet, ref = anchor.removeprefix("xlsx:").rsplit("!", 1)
        assert wb[sheet][ref].value == value


def _dated_template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    for ref, fmt in {
        "A1": "mm-dd-yy",  # built-in 14
        "A2": "dd/mm/yyyy hh:mm",  # custom date
        "A3": "0.00",  # number
        "A4": '"day" 0',  # custom number with a quoted d
    }.items():
        ws[ref] = 1
        ws[ref].number_format = fmt
    ws["A5"] = 1  # General
    out = BytesIO()
    wb.save(out)
    return out.getvalue()


@pytest.mark.parametrize(
    ("ref", "typed"),
    [("A1", True), ("A2", True), ("A3", False), ("A4", False), ("A5", False), ("A9", False)],
)
def test_date_is_a_serial_only_in_a_date_formatted_cell(ref, typed):
    tpl = _dated_template()
    styles = _zip(tpl).read("xl/styles.xml")
    out, _ = write(tpl, [_item(f"xlsx:Summary!{ref}", 45356, etype="date")])
    cell = _cell(out, ref)
    if typed:
        assert cell.get("t") is None and cell.find(f"{NS}v").text == "45356"
    else:
        assert cell.get("t") == "inlineStr"
        assert "".join(cell.find(f"{NS}is").itertext()) == "2024-03-05"
    assert _zip(out).read("xl/styles.xml") == styles
