import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from lxml import etree
from pptx import Presentation

from doc_intel.mapping.schemas import MappingWarning
from doc_intel.templates.anchors import build_anchor_map
from doc_intel.templates.schemas import Constraints, IRField
from doc_intel.writers import WriteItem
from doc_intel.writers.pptx_writer import write
from tests.fixtures.templates import pptx_template

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
GOLDEN = Path(__file__).parents[1] / "fixtures/templates/pptx.anchors.json"
TITLE = "pptx:s2:sp2"
BOX = "pptx:s2:sp3"
CELL = "pptx:s3:sp3:r{}c{}"


def _field(anchor_id, mode="replace_text", etype="text", fid="f1", **constraints) -> IRField:
    return IRField(
        field_id=fid,
        anchor_id=anchor_id,
        section_path=[],
        label="L",
        description="",
        expected_type=etype,
        required=True,
        write_mode=mode,
        constraints=Constraints(**constraints),
    )


def _item(anchor_id, content, mode="replace_text", etype="text", **constraints) -> WriteItem:
    return WriteItem(_field(anchor_id, mode, etype, **constraints), content)


def _parts(data: bytes) -> dict[str, bytes]:
    z = zipfile.ZipFile(BytesIO(data))
    return {i.filename: z.read(i.filename) for i in z.infolist()}


def _c14n(name: str, data: bytes) -> bytes:
    if name.endswith((".xml", ".rels")):
        return etree.tostring(etree.fromstring(data), method="c14n")
    return data


def _changed_parts(before: bytes, after: bytes) -> set[str]:
    a, b = _parts(before), _parts(after)
    assert a.keys() == b.keys()
    return {n for n in a if _c14n(n, a[n]) != _c14n(n, b[n])}


def _shape(data: bytes, slide: int, shape_id: int):
    prs = Presentation(BytesIO(data))
    return next(s for s in prs.slides[slide - 1].shapes if s.shape_id == shape_id)


def _paras(shape):
    return shape.text_frame._txBody.findall(f"{A}p")


def test_c14n_probe_unchanged_save_is_c14n_equal():
    tpl = pptx_template()
    out = BytesIO()
    Presentation(BytesIO(tpl)).save(out)
    assert _changed_parts(tpl, out.getvalue()) == set()


def test_input_bytes_are_not_mutated_and_untouched_parts_equal():
    tpl = pptx_template()
    snapshot = bytes(tpl)
    out, warnings = write(tpl, [_item(TITLE, ["New"])])
    assert tpl == snapshot and out is not tpl
    assert warnings == []
    assert _changed_parts(tpl, out) == {"ppt/slides/slide2.xml"}


def test_structure_preserved_and_opens():
    tpl = pptx_template()
    out, _ = write(tpl, [_item(TITLE, ["New"]), _item(CELL.format(1, 0), ["x"])])
    a, b = Presentation(BytesIO(tpl)), Presentation(BytesIO(out))
    assert len(a.slides) == len(b.slides)
    assert [len(s.shapes) for s in a.slides] == [len(s.shapes) for s in b.slides]
    assert len(a.slide_layouts) == len(b.slide_layouts)


def test_replace_text_keeps_first_run_and_ppr():
    tpl = pptx_template()
    # give the paragraph a distinctive pPr and rPr, three runs
    prs = Presentation(BytesIO(tpl))
    shape = next(s for s in prs.slides[1].shapes if s.shape_id == 3)
    p = shape.text_frame.paragraphs[0]
    p.alignment = 2
    p.runs[0].font.bold = True
    p.add_run().text = " extra"
    buf = BytesIO()
    prs.save(buf)
    src = buf.getvalue()
    before = _paras(_shape(src, 2, 3))[0]
    out, _ = write(src, [_item(BOX, ["Hello"])])
    after = _paras(_shape(out, 2, 3))
    assert len(after) == 1
    runs = after[0].findall(f"{A}r")
    assert len(runs) == 1 and runs[0].find(f"{A}t").text == "Hello"
    c = lambda e: etree.tostring(e, method="c14n")  # noqa: E731
    assert c(after[0].find(f"{A}pPr")) == c(before.find(f"{A}pPr"))
    assert c(runs[0].find(f"{A}rPr")) == c(before.find(f"{A}r").find(f"{A}rPr"))


def test_multiple_items_clone_paragraphs_and_leftovers_removed():
    out, _ = write(pptx_template(), [_item("pptx:s1:sp3", ["a", "b", "c"], etype="paragraphs")])
    assert [p.text for p in _shape(out, 1, 3).text_frame.paragraphs] == ["a", "b", "c"]
    out2, _ = write(out, [_item("pptx:s1:sp3", ["only"], etype="paragraphs")])
    assert [p.text for p in _shape(out2, 1, 3).text_frame.paragraphs] == ["only"]


def test_empty_paragraph_gets_run_with_endparardpr_properties():
    tpl = pptx_template()
    prs = Presentation(BytesIO(tpl))
    cell = prs.slides[2].shapes[-2].table.cell(1, 0)  # group is last, table before it
    p = cell._tc.txBody.find(f"{A}p")
    assert p.find(f"{A}r") is None
    end = etree.SubElement(p, f"{A}endParaRPr")
    end.set("lang", "en-US")
    buf = BytesIO()
    prs.save(buf)
    out, _ = write(buf.getvalue(), [_item(CELL.format(1, 0), ["V"])])
    tbl = next(s for s in Presentation(BytesIO(out)).slides[2].shapes if s.has_table)
    p2 = tbl.table.cell(1, 0)._tc.txBody.find(f"{A}p")
    kids = [k.tag for k in p2]
    assert kids == [f"{A}r", f"{A}endParaRPr"]
    assert p2.find(f"{A}r/{A}rPr").get("lang") == "en-US"
    assert p2.find(f"{A}r/{A}t").text == "V"


def test_replace_token_across_runs_keeps_other_runs():
    prs = Presentation(BytesIO(pptx_template()))
    shape = next(s for s in prs.slides[1].shapes if s.shape_id == 3)
    p = shape.text_frame.paragraphs[0]
    for r in list(p.runs):
        r._r.getparent().remove(r._r)
    for text, bold in (
        ("Date {{inc", True),
        ("ident", False),
        ("_date}} status", None),
        (" TBD", None),
    ):
        run = p.add_run()
        run.text = text
        run.font.bold = bold
    buf = BytesIO()
    prs.save(buf)
    out, _ = write(
        buf.getvalue(),
        [_item(BOX, ["2024-01-02"], mode="replace_token", token="{{incident_date}}")],
    )
    runs = _paras(_shape(out, 2, 3))[0].findall(f"{A}r")
    assert [r.find(f"{A}t").text for r in runs] == ["Date 2024-01-02 status", " TBD"]
    assert runs[0].find(f"{A}rPr").get("b") == "1"  # first spanning run's rPr kept


def test_replace_token_missing_token_is_keyerror():
    with pytest.raises(KeyError) as exc:
        write(pptx_template(), [_item(BOX, ["x"], mode="replace_token", token="{{nope}}")])
    assert exc.value.args == ("token_not_found",)


def test_replace_token_joins_list_content():
    out, _ = write(
        pptx_template(), [_item(BOX, ["a", "b"], mode="replace_token", token="{{incident_date}}")]
    )
    assert _shape(out, 2, 3).text_frame.text == "Date a; b status TBD"


def test_fill_table_rows_writes_body_rows_and_keeps_header():
    rows = [["a", "b", "c"], ["d", "e", "f"]]
    out, warnings = write(
        pptx_template(),
        [_item(CELL.format(1, 0), rows, mode="fill_table_rows", etype="table")],
    )
    tbl = next(s for s in Presentation(BytesIO(out)).slides[2].shapes if s.has_table).table
    assert [[c.text for c in r.cells] for r in tbl.rows] == [
        ["Name", "Role", "Owner"],
        ["a", "b", "c"],
        ["d", "e", "f"],
    ]
    assert warnings == []


def test_fill_table_rows_overflow_warns_once():
    rows = [["a", "b", "c", "extra"], ["d", "e", "f"], ["g"], ["h"]]
    out, warnings = write(
        pptx_template(),
        [_item(CELL.format(1, 0), rows, mode="fill_table_rows", etype="table")],
    )
    tbl = next(s for s in Presentation(BytesIO(out)).slides[2].shapes if s.has_table).table
    assert [c.text for c in tbl.rows[1].cells] == ["a", "b", "c"]
    assert [c.text for c in tbl.rows[2].cells] == ["d", "e", "f"]
    assert warnings == [MappingWarning(field_id="f1", code="table_overflow")]


def test_fill_table_rows_marker_goes_to_first_body_cell():
    out, _ = write(
        pptx_template(),
        [_item(CELL.format(1, 0), ["[MISSING: L]"], mode="fill_table_rows", etype="table")],
    )
    tbl = next(s for s in Presentation(BytesIO(out)).slides[2].shapes if s.has_table).table
    assert tbl.cell(1, 0).text == "[MISSING: L]"
    assert tbl.cell(1, 1).text == ""


def test_marker_text_at_anchor_and_group_shape_resolution():
    out, _ = write(pptx_template(), [_item("pptx:s3:sp5", ["[CONFLICT: See Chat Summary]"])])
    prs = Presentation(BytesIO(out))
    group = next(s for s in prs.slides[2].shapes if s.shape_type == 6)
    assert group.shapes[0].text_frame.text == "[CONFLICT: See Chat Summary]"


@pytest.mark.parametrize(
    "anchor",
    ["pptx:s9:sp2", "pptx:s1:sp999", "pptx:s3:sp3:r9c9", "docx:body:p0", "pptx:s2:sp2:r0c0"],
)
def test_unknown_anchor_is_keyerror(anchor):
    with pytest.raises(KeyError) as exc:
        write(pptx_template(), [_item(anchor, ["x"])])
    assert exc.value.args == ("anchor_unresolved",)


def test_round_trip_every_golden_anchor():
    tpl = pptx_template()
    ids = [a["anchor_id"] for a in json.loads(GOLDEN.read_text())["anchors"]]
    values = {aid: f"V{i:04d}" for i, aid in enumerate(ids, start=1)}
    items = [WriteItem(_field(a, fid=f"f{i}"), [v]) for i, (a, v) in enumerate(values.items())]
    out, warnings = write(tpl, items)
    assert warnings == []
    amap = build_anchor_map("pptx", out)
    assert {a.anchor_id: a.text for a in amap.anchors} == values


def test_a_t_has_no_space_attribute():
    out, _ = write(pptx_template(), [_item(BOX, [" x "])])
    root = etree.fromstring(_parts(out)["ppt/slides/slide2.xml"])
    assert all(t.attrib == {} for t in root.iter(f"{A}t"))


def test_fill_table_rows_skips_spanned_cells():
    prs = Presentation(BytesIO(pptx_template()))
    tbl = next(s for s in prs.slides[2].shapes if s.has_table).table
    tbl.cell(1, 0).merge(tbl.cell(1, 1))
    buf = BytesIO()
    prs.save(buf)
    out, warnings = write(
        buf.getvalue(),
        [_item(CELL.format(1, 0), [["a", "b"]], mode="fill_table_rows", etype="table")],
    )
    tbl = next(s for s in Presentation(BytesIO(out)).slides[2].shapes if s.has_table).table
    assert (tbl.cell(1, 0).text, tbl.cell(1, 1).text, tbl.cell(1, 2).text) == ("a", "", "b")
    assert warnings == []


MULTI_TEXT = ("[Presenter Name]", "[Role]", "[DD Month YYYY]")


def _multi_token_deck(runs):
    prs = Presentation(BytesIO(pptx_template()))
    shape = next(s for s in prs.slides[1].shapes if s.shape_id == 3)
    p = shape.text_frame.paragraphs[0]
    for r in list(p.runs):
        r._r.getparent().remove(r._r)
    for text, bold in runs:
        run = p.add_run()
        run.text = text
        run.font.bold = bold
    buf = BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _three_items(values=("Ada", "CFO", "01 March 2026")):
    return [
        WriteItem(_field(BOX, "replace_token", fid=f"f{i}", token=tok), [val])
        for i, (tok, val) in enumerate(zip(MULTI_TEXT, values, strict=True))
    ]


def test_several_replace_token_items_on_one_shape_compose():
    deck = _multi_token_deck(
        [
            ("[Presenter", True),
            (" Name], ", None),
            ("[Role] \u00b7 [DD Month", False),
            (" YYYY]", None),
        ]
    )
    out, _ = write(deck, _three_items())
    p = _paras(_shape(out, 2, 3))[0]
    assert "".join(t.text for t in p.iter(f"{A}t")) == "Ada, CFO \u00b7 01 March 2026"
    runs = p.findall(f"{A}r")
    assert runs[0].find(f"{A}rPr").get("b") == "1"  # the run the first token started in


def test_substring_tokens_are_replaced_in_either_ir_order():
    from doc_intel.writers import render

    deck = _multi_token_deck([("a ____ and ______ z", None)])
    for order in (("____", "______"), ("______", "____")):
        fields = [_field(BOX, "replace_token", fid=f"f{i}", token=t) for i, t in enumerate(order)]
        values = {
            "f0" if order[0] == "____" else "f1": "S",
            "f1" if order[0] == "____" else "f0": "L",
        }
        results = {k: _user_result(k, v) for k, v in values.items()}
        out, _ = render(
            "pptx", deck, fields, results, missing_marker="[{label}?]", conflict_marker="[!]"
        )
        assert _shape(out, 2, 3).text_frame.text == "a S and L z"


def _user_result(field_id, value):
    from doc_intel.mapping.schemas import FieldMappingResult

    return FieldMappingResult(
        field_id=field_id,
        status="populated",
        value=value,
        evidence=[],
        confidence=1.0,
        missing_reason=None,
        checked_documents=[],
        conflict_details=None,
        provenance="user",
    )


def test_missing_field_writes_its_marker_in_its_token_place():
    from doc_intel.mapping.schemas import FieldMappingResult
    from doc_intel.writers import render

    deck = _multi_token_deck([("[Presenter Name], [Role] \u00b7 [DD Month YYYY]", None)])
    fields = [_field(BOX, "replace_token", fid=f"f{i}", token=t) for i, t in enumerate(MULTI_TEXT)]
    missing = FieldMappingResult(
        field_id="f1",
        status="missing",
        value=None,
        evidence=[],
        confidence=0.0,
        missing_reason="no_relevant_chunks",
        checked_documents=[],
        conflict_details=None,
        provenance="rag",
    )
    results = {"f0": _user_result("f0", "Ada"), "f1": missing, "f2": _user_result("f2", "now")}
    out, _ = render(
        "pptx", deck, fields, results, missing_marker="<<{label}>>", conflict_marker="!"
    )
    assert _shape(out, 2, 3).text_frame.text == "Ada, <<L>> \u00b7 now"
