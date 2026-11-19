import zipfile
from io import BytesIO

import pytest
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches

from doc_intel.core.errors import ValidationFailed
from doc_intel.ingestion.pipeline import ParseError
from doc_intel.templates.anchors import build_anchor_map

XXE = b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
LAUGHS = (
    b'<!DOCTYPE x [<!ENTITY a "' + b"lol" * 10 + b'">'
    b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
    b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>'
)


def _save(obj) -> bytes:
    out = BytesIO()
    obj.save(out)
    return out.getvalue()


def _docx() -> bytes:
    doc = Document()
    doc.add_paragraph("ENTITYHERE")
    return _save(doc)


def _pptx() -> bytes:
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_textbox(
        Inches(1), Inches(1), Inches(1), Inches(1)
    ).text_frame.text = "ENTITYHERE"
    return _save(prs)


def _xlsx() -> bytes:
    wb = Workbook()
    wb.active["A1"] = "ENTITYHERE"
    return _save(wb)


CASES = [
    ("docx", _docx, "word/document.xml"),
    ("pptx", _pptx, "ppt/slides/slide1.xml"),
    ("xlsx", _xlsx, "xl/sharedStrings.xml"),
]


def _rewrite(data: bytes, part: str, doctype: bytes, ref: bytes) -> bytes:
    out = BytesIO()
    with zipfile.ZipFile(BytesIO(data)) as src, zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            body = src.read(item.filename)
            if item.filename == part:
                body = body.replace(b"?>", b"?>" + doctype, 1).replace(b"ENTITYHERE", ref)
                assert doctype in body
            dst.writestr(item.filename, body)
    return out.getvalue()


@pytest.mark.parametrize(("fmt", "build", "part"), CASES)
def test_xxe_does_not_leak_file_content(fmt, build, part):
    data = _rewrite(build(), part, XXE, b"&e;")
    try:
        amap = build_anchor_map(fmt, data)
    except (ParseError, ValidationFailed):
        return
    assert "root:" not in amap.model_dump_json()


def test_billion_laughs_not_expanded():
    data = _rewrite(_docx(), "word/document.xml", LAUGHS, b"&c;")
    try:
        amap = build_anchor_map("docx", data)
    except (ParseError, ValidationFailed):  # entities dropped leaves no anchors: also fine
        return
    assert len(amap.model_dump_json()) < 5_000  # 300 x "lol" x 100 would be 90 kB


def test_understated_zip_size_is_rejected():
    out = BytesIO()
    with zipfile.ZipFile(BytesIO(_docx())) as src, zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            dst.writestr(item.filename, src.read(item.filename))
            if item.filename == "word/document.xml":
                # zipfile reads at most the declared size; the 512 MB declared-total cap in
                # ingestion/upload.py then bounds decompression.
                dst.filelist[-1].file_size = item.file_size // 2
    with pytest.raises(ParseError):
        build_anchor_map("docx", out.getvalue())


def _never_called(data):
    raise AssertionError("the library must not see an oversized template")


def test_too_many_zip_parts_rejected(monkeypatch):
    monkeypatch.setattr("doc_intel.templates.anchors_docx.extract", _never_called)
    out = BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for i in range(5001):
            z.writestr(f"p{i}", b"")
    with pytest.raises(ValidationFailed, match="too many parts"):
        build_anchor_map("docx", out.getvalue())


def test_oversized_xml_part_rejected_before_parsing(monkeypatch):
    monkeypatch.setattr("doc_intel.templates.anchors_docx.extract", _never_called)
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", b"<a><!--" + b"x" * (33 * 1024 * 1024) + b"--></a>")
    with pytest.raises(ValidationFailed, match="too large"):
        build_anchor_map("docx", out.getvalue())


@pytest.mark.parametrize("name", ["ppt/slides/slide1.png", "word/DOCUMENT.XML"])
def test_part_cap_ignores_part_name(monkeypatch, name):
    # [Content_Types].xml, not the extension, decides what python-pptx/docx parse as XML.
    monkeypatch.setattr("doc_intel.templates.anchors_docx.extract", _never_called)
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, b"x" * (33 * 1024 * 1024))
    with pytest.raises(ValidationFailed, match="too large"):
        build_anchor_map("docx", out.getvalue())


def test_template_total_size_capped(monkeypatch):
    monkeypatch.setattr("doc_intel.templates.anchors_docx.extract", _never_called)
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for i in range(5):  # 5 x 30 MB: every part under the part cap, total over 128 MB
            z.writestr(f"word/p{i}.xml", b"x" * (30 * 1024 * 1024))
    with pytest.raises(ValidationFailed, match="too large"):
        build_anchor_map("docx", out.getvalue())
