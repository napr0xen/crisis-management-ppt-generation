import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from lxml import etree

from doc_intel.mapping.schemas import MappingWarning
from doc_intel.templates.anchors import build_anchor_map
from doc_intel.templates.schemas import Constraints, IRField
from doc_intel.writers import WriteItem
from doc_intel.writers.docx_writer import write
from tests.fixtures.templates import docx_template

GOLDEN = Path(__file__).parents[1] / "fixtures/templates/docx.anchors.json"
CLIENT = "docx:body:p3"


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


def _text(el) -> str:
    return "".join(t.text or "" for t in el.iter(qn("w:t")))


def _doc(data: bytes):
    return Document(BytesIO(data))


def _with_body(mutate) -> bytes:
    doc = _doc(docx_template())
    mutate(doc)
    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def test_c14n_probe_unchanged_save_is_c14n_equal():
    tpl = docx_template()
    out = BytesIO()
    _doc(tpl).save(out)
    assert _changed_parts(tpl, out.getvalue()) == set()


def test_input_bytes_untouched_and_only_document_part_changes():
    tpl = docx_template()
    snapshot = bytes(tpl)
    out, warnings = write(tpl, [_item(CLIENT, ["Acme"])])
    assert tpl == snapshot and out is not tpl and warnings == []
    assert _changed_parts(tpl, out) == {"word/document.xml"}


def test_structure_preserved_and_opens():
    tpl = docx_template()
    out, _ = write(tpl, [_item(CLIENT, ["Acme"]), _item("docx:sec1:header:p0", ["H"])])
    a, b = _doc(tpl), _doc(out)
    assert len(a.sections) == len(b.sections)
    assert len(a.tables) == len(b.tables)
    assert len(a.paragraphs) == len(b.paragraphs)
    assert b.sections[0].header.paragraphs[0].text == "H"
    assert b.sections[0].footer.paragraphs[0].text == "Page footer"


def test_replace_text_keeps_first_run_rpr_and_ppr():
    def mutate(doc):
        p = doc.paragraphs[4]  # "Signature ____"
        p.alignment = 1
        p.runs[0].bold = True
        p.add_run(" more")

    src = _with_body(mutate)
    before = _doc(src).paragraphs[4]._p
    out, _ = write(src, [_item("docx:body:p4", ["Signed"])])
    after = _doc(out).paragraphs[4]._p
    runs = after.findall(qn("w:r"))
    assert len(runs) == 1 and _text(runs[0]) == "Signed"
    c = lambda e: etree.tostring(e, method="c14n")  # noqa: E731
    assert c(after.find(qn("w:pPr"))) == c(before.find(qn("w:pPr")))
    assert c(runs[0].find(qn("w:rPr"))) == c(before.find(qn("w:r")).find(qn("w:rPr")))


def test_bullet_list_clones_keep_numpr_and_ppr():
    def mutate(doc):
        doc.paragraphs[4].style = doc.styles["List Bullet"]
        ppr = doc.paragraphs[4]._p.get_or_add_pPr()
        ppr.append(
            parse_xml(f'<w:numPr {nsdecls("w")}><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>')
        )

    src = _with_body(mutate)
    out, _ = write(src, [_item("docx:body:p4", ["a", "b", "c"], etype="bullet_list")])
    pars = _doc(out).paragraphs[4:7]
    assert [p.text for p in pars] == ["a", "b", "c"]
    for p in pars:
        assert p._p.find(f"{qn('w:pPr')}/{qn('w:numPr')}") is not None
        assert p.style.name == "List Bullet"
    assert _doc(out).paragraphs[7].text == ""  # the next original paragraph follows


def test_empty_paragraph_run_copies_paragraph_mark_properties():
    def mutate(doc):
        ppr = doc.paragraphs[2]._p.get_or_add_pPr()
        ppr.append(parse_xml(f"<w:rPr {nsdecls('w')}><w:i/></w:rPr>"))

    out, _ = write(_with_body(mutate), [_item("docx:body:p2", ["V"])])
    run = _doc(out).paragraphs[2]._p.find(qn("w:r"))
    assert run.find(f"{qn('w:rPr')}/{qn('w:i')}") is not None


def test_replace_token_across_three_runs():
    out, _ = write(
        docx_template(),
        [_item(CLIENT, ["Acme"], mode="replace_token", token="{{client_name}}")],
    )
    par = _doc(out).paragraphs[3]
    assert par.text == "Client: Acme"
    assert [r.text for r in par.runs] == ["Client: Acme"]


def test_replace_token_missing_is_keyerror():
    with pytest.raises(KeyError) as exc:
        write(docx_template(), [_item(CLIENT, ["x"], mode="replace_token", token="{{zzz}}")])
    assert exc.value.args == ("token_not_found",)


def test_table_cell_and_merged_cell_anchor():
    out, _ = write(
        docx_template(),
        [_item("docx:body:t0:r1c0", ["merged"]), _item("docx:body:t0:r1c2", ["end"])],
    )
    t = _doc(out).tables[0]
    assert t.cell(1, 0).text == "merged" and t.cell(1, 1).text == "merged"
    assert t.cell(1, 2).text == "end"


def test_fill_table_rows_and_overflow():
    rows = [["a", "b", "c", "x"], ["d", "e", "f"]]
    out, warnings = write(
        docx_template(),
        [_item("docx:body:t0:r1c0", rows, mode="fill_table_rows", etype="table")],
    )
    t = _doc(out).tables[0]
    assert [c.text for c in t.rows[0].cells] == ["Item", "Owner", "Date"]
    assert t.cell(1, 2).text == "b"  # row 1 has two unique cells: merged (a) and c2 (b)
    assert warnings == [
        MappingWarning(field_id="f1", code="table_overflow")
    ]  # col and row overflow
    assert len(t.rows) == 2


def test_fill_table_rows_marker_in_first_body_cell():
    out, warnings = write(
        docx_template(),
        [_item("docx:body:t0:r1c0", ["[CONFLICT: x]"], mode="fill_table_rows", etype="table")],
    )
    assert _doc(out).tables[0].cell(1, 0).text == "[CONFLICT: x]"
    assert warnings == []


def test_header_and_footer_written_through_section():
    out, _ = write(
        docx_template(),
        [_item("docx:sec1:header:p0", ["[MISSING: L]"]), _item("docx:sec1:footer:p0", ["F"])],
    )
    sec = _doc(out).sections[0]
    assert sec.header.paragraphs[0].text == "[MISSING: L]"
    assert sec.footer.paragraphs[0].text == "F"
    assert _changed_parts(docx_template(), out) == {
        p for p in _parts(out) if p.startswith("word/header") or p.startswith("word/footer")
    }


def test_content_control_block_level_removes_showing_placeholder():
    def mutate(doc):
        sdt = next(doc.element.body.iter(qn("w:sdt")))
        sdt.find(qn("w:sdtPr")).append(parse_xml(f"<w:showingPlcHdr {nsdecls('w')}/>"))

    out, _ = write(_with_body(mutate), [_item("docx:sdt:owner", ["Jo", "Al"], etype="paragraphs")])
    sdt = next(_doc(out).element.body.iter(qn("w:sdt")))
    assert sdt.find(f"{qn('w:sdtPr')}/{qn('w:showingPlcHdr')}") is None
    assert [_text(p) for p in sdt.find(qn("w:sdtContent")).findall(qn("w:p"))] == [
        "Jo",
        "Al",
    ]


def test_content_control_inline_uses_run_rules():
    def mutate(doc):
        par = doc.paragraphs[4]._p
        par.append(
            parse_xml(
                f'<w:sdt {nsdecls("w")}><w:sdtPr><w:tag w:val="inl"/></w:sdtPr><w:sdtContent>'
                "<w:r><w:rPr><w:b/></w:rPr><w:t>x</w:t></w:r><w:r><w:t>y</w:t></w:r>"
                "</w:sdtContent></w:sdt>"
            )
        )

    out, _ = write(_with_body(mutate), [_item("docx:sdt:inl", ["a", "b"], etype="paragraphs")])
    sdt = next(
        s
        for s in _doc(out).element.body.iter(qn("w:sdt"))
        if s.find(".//" + qn("w:tag")).get(qn("w:val")) == "inl"
    )
    runs = sdt.find(qn("w:sdtContent")).findall(qn("w:r"))
    assert len(runs) == 1 and _text(runs[0]) == "a; b"
    assert runs[0].find(f"{qn('w:rPr')}/{qn('w:b')}") is not None


def test_duplicate_sdt_tag_writes_first_only():
    def mutate(doc):
        body = doc.element.body
        dup = parse_xml(
            f'<w:sdt {nsdecls("w")}><w:sdtPr><w:tag w:val="owner"/></w:sdtPr><w:sdtContent>'
            "<w:p><w:r><w:t>second</w:t></w:r></w:p></w:sdtContent></w:sdt>"
        )
        body.sectPr.addprevious(dup)

    out, _ = write(_with_body(mutate), [_item("docx:sdt:owner", ["V"])])
    texts = [_text(s) for s in _doc(out).element.body.iter(qn("w:sdt"))]
    assert texts == ["V", "Untagged", "second"]


def test_clone_drops_duplicate_para_ids():
    def mutate(doc):
        doc.paragraphs[4]._p.set(
            "{http://schemas.microsoft.com/office/word/2010/wordml}paraId", "AB12"
        )

    out, _ = write(_with_body(mutate), [_item("docx:body:p4", ["a", "b"], etype="paragraphs")])
    first, second = _doc(out).paragraphs[4:6]
    key = "{http://schemas.microsoft.com/office/word/2010/wordml}paraId"
    assert first._p.get(key) == "AB12" and second._p.get(key) is None


@pytest.mark.parametrize(
    "anchor",
    [
        "docx:body:p99",
        "docx:body:t5:r0c0",
        "docx:body:t0:r9c0",
        "docx:sec9:header:p0",
        "docx:sec1:header:p9",
        "docx:sdt:nope",
        "docx:bogus",
    ],
)
def test_unknown_anchor_is_keyerror(anchor):
    with pytest.raises(KeyError) as exc:
        write(docx_template(), [_item(anchor, ["x"])])
    assert exc.value.args == ("anchor_unresolved",)


def test_round_trip_every_golden_anchor():
    ids = [a["anchor_id"] for a in json.loads(GOLDEN.read_text())["anchors"]]
    values = {aid: f"V{i:04d}" for i, aid in enumerate(ids, start=1)}
    items = [WriteItem(_field(a, fid=f"f{i}"), [v]) for i, (a, v) in enumerate(values.items())]
    out, warnings = write(docx_template(), items)
    assert warnings == []
    amap = build_anchor_map("docx", out)
    assert {a.anchor_id: a.text for a in amap.anchors} == values


def _blank(paragraphs):
    doc = Document()
    for t in paragraphs:
        doc.add_paragraph(t)
    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def test_multi_item_write_does_not_shift_later_paragraph_anchors():
    out, _ = write(
        _blank(["A", "B", "C"]),
        [
            WriteItem(_field("docx:body:p0", etype="paragraphs", fid="a"), ["x1", "x2", "x3"]),
            WriteItem(_field("docx:body:p1", fid="b"), ["Y"]),
            WriteItem(_field("docx:body:p2", fid="c"), ["Z"]),
        ],
    )
    assert [p.text for p in _doc(out).paragraphs] == ["x1", "x2", "x3", "Y", "Z"]


def test_table_cell_write_unaffected_by_earlier_paragraph_growth():
    out, _ = write(
        docx_template(),
        [
            _item("docx:body:p3", ["a", "b", "c"], etype="paragraphs"),
            _item("docx:body:t0:r1c2", ["end"]),
        ],
    )
    assert _doc(out).tables[0].cell(1, 2).text == "end"


def _para_with_inline_sdt() -> bytes:
    doc = Document()
    p = doc.add_paragraph("Client: ")
    p._p.append(
        parse_xml(
            f'<w:sdt {nsdecls("w")}><w:sdtPr><w:tag w:val="c"/><w:showingPlcHdr/></w:sdtPr>'
            '<w:sdtContent><w:r><w:rPr><w:rStyle w:val="PlaceholderText"/><w:b/></w:rPr>'
            "<w:t>click</w:t></w:r></w:sdtContent></w:sdt>"
        )
    )
    out = BytesIO()
    doc.save(out)
    return out.getvalue()


@pytest.mark.parametrize("sdt_first", [False, True])
def test_paragraph_write_keeps_inline_sdt_that_is_another_target(sdt_first):
    para = WriteItem(_field("docx:body:p0", fid="p"), ["Client: Z"])
    sdt = WriteItem(_field("docx:sdt:c", fid="s"), ["Y"])
    out, _ = write(_para_with_inline_sdt(), [sdt, para] if sdt_first else [para, sdt])
    p = _doc(out).paragraphs[0]._p
    assert _text(p) == "Client: ZY"
    assert len(p.findall(qn("w:sdt"))) == 1


def test_filled_placeholder_sdt_drops_placeholder_style_and_keeps_other_props():
    out, _ = write(_para_with_inline_sdt(), [_item("docx:sdt:c", ["Y"])])
    sdt = _doc(out).element.body.find(".//" + qn("w:sdt"))
    assert sdt.find(f".//{qn('w:rStyle')}") is None
    assert sdt.find(f".//{qn('w:b')}") is not None
    assert sdt.find(f".//{qn('w:showingPlcHdr')}") is None


def test_cloned_paragraphs_carry_no_section_break():
    doc = Document()
    p = doc.add_paragraph("A")
    p._p.get_or_add_pPr().append(parse_xml(f"<w:sectPr {nsdecls('w')}/>"))
    doc.add_paragraph("B")
    buf = BytesIO()
    doc.save(buf)
    out, _ = write(buf.getvalue(), [_item("docx:body:p0", ["1", "2", "3"], etype="paragraphs")])
    ps = _doc(out).element.body.findall(qn("w:p"))
    assert [len(p.findall(f"{qn('w:pPr')}/{qn('w:sectPr')}")) for p in ps[:3]] == [1, 0, 0]


def test_merged_row_writes_each_unique_cell_once_in_order():
    out, warnings = write(
        docx_template(),
        [_item("docx:body:t0:r1c0", [["a", "b"]], mode="fill_table_rows", etype="table")],
    )
    t = _doc(out).tables[0]
    assert (t.cell(1, 0).text, t.cell(1, 1).text, t.cell(1, 2).text) == ("a", "a", "b")
    assert warnings == []


def test_merged_row_extra_values_overflow_once():
    out, warnings = write(
        docx_template(),
        [_item("docx:body:t0:r1c0", [["a", "b", "c"]], mode="fill_table_rows", etype="table")],
    )
    assert _doc(out).tables[0].cell(1, 0).text == "a"
    assert warnings == [MappingWarning(field_id="f1", code="table_overflow")]


def test_docx_text_runs_have_space_preserve_and_sdt_without_content_is_unresolved():
    out, _ = write(docx_template(), [_item(CLIENT, [" x "])])
    t = next(_doc(out).paragraphs[3]._p.iter(qn("w:t")))
    assert t.get("{http://www.w3.org/XML/1998/namespace}space") == "preserve"

    def strip_content(doc):
        sdt = next(doc.element.body.iter(qn("w:sdt")))
        sdt.remove(sdt.find(qn("w:sdtContent")))

    tpl = _with_body(strip_content)
    tag = next(t.get(qn("w:val")) for t in _doc(tpl).element.body.iter(qn("w:tag")))
    with pytest.raises(KeyError) as exc:
        write(tpl, [_item(f"docx:sdt:{tag}", ["x"])])
    assert exc.value.args == ("anchor_unresolved",)


def test_cell_written_in_a_table_after_a_row_sdt_table():
    from tests.unit.test_anchors import _row_sdt_doc

    data = _row_sdt_doc()
    amap = build_anchor_map("docx", data)
    assert "docx:body:t2:r1c0" in [a.anchor_id for a in amap.anchors]
    out, _ = write(data, [_item("docx:body:t2:r1c0", ["new"])])
    assert [t.cell(1, 0).text for t in _doc(out).tables[2:]] == ["new"]
