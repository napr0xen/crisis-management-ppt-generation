"""DOCX surgical writer (system-design § 8): ids resolve exactly as in `templates/anchors_docx`."""

import re
from io import BytesIO

from docx import Document
from docx.document import Document as DocumentObject
from docx.oxml.ns import qn
from lxml import etree

from doc_intel.mapping.schemas import MappingWarning
from doc_intel.mapping.verifier import TYPED_TYPES
from doc_intel.templates.schemas import IRField
from doc_intel.writers import WriteItem
from doc_intel.writers.runs import (
    W,
    fill_rows,
    paragraphs_of,
    replace_token,
    set_paragraph,
    texts,
    write_paragraphs,
)

_BODY_P = re.compile(r"^docx:body:p(\d+)$")
_BODY_CELL = re.compile(r"^docx:body:t(\d+):r(\d+)c(\d+)$")
_HF = re.compile(r"^docx:sec(\d+):(header|footer):p(\d+)$")
_SDT = "docx:sdt:"


_UNRESOLVED = "anchor_unresolved"  # never the anchor id: it can carry template text


def _first_sdt(doc: DocumentObject, tag: str) -> etree._Element:
    # shortcut: a repeated tag writes the first control only (mirrors the extractor), upgrade
    # when templates need every duplicate filled.
    for sdt in doc.element.body.iter(qn("w:sdt")):
        t = sdt.find(f"{qn('w:sdtPr')}/{qn('w:tag')}")
        if t is not None and t.get(qn("w:val")) == tag:
            if sdt.find(qn("w:sdtContent")) is None:
                break
            return sdt
    raise KeyError(_UNRESOLVED)


def _paragraph_container(doc: DocumentObject, anchor_id: str) -> etree._Element:
    """The element to write into: a `w:p`, `w:tc` or `w:sdt`."""
    try:
        if m := _BODY_P.match(anchor_id):
            return doc.paragraphs[int(m[1])]._p
        if m := _BODY_CELL.match(anchor_id):
            return doc.tables[int(m[1])].rows[int(m[2])].cells[int(m[3])]._tc
        if m := _HF.match(anchor_id):
            section = doc.sections[int(m[1]) - 1]
            part = section.header if m[2] == "header" else section.footer
            return part.paragraphs[int(m[3])]._p
    except IndexError as exc:
        raise KeyError(_UNRESOLVED) from exc
    if anchor_id.startswith(_SDT):
        return _first_sdt(doc, anchor_id.removeprefix(_SDT))
    raise KeyError(_UNRESOLVED)


def _row_cells(doc: DocumentObject, field: IRField) -> list[list[etree._Element]]:
    """Each table row's unique `w:tc` elements: a merged cell is one tc at several grid slots."""
    m = _BODY_CELL.match(field.anchor_id)
    if m is None:
        raise KeyError(_UNRESOLVED)
    rows = []
    for row in doc.tables[int(m[1])].rows:
        tcs = [c._tc for c in row.cells]
        rows.append([tc for i, tc in enumerate(tcs) if tc not in tcs[:i]])
    return rows


def _resolve(doc: DocumentObject, field: IRField) -> object:
    anchor = _paragraph_container(doc, field.anchor_id)  # validates the anchor either way
    return _row_cells(doc, field) if field.write_mode == "fill_table_rows" else anchor


def _write_item(
    item: WriteItem, target: object, protected: frozenset[etree._Element]
) -> list[MappingWarning]:
    field = item.field
    if field.write_mode == "fill_table_rows":
        rows = target  # type: ignore[assignment]
        if fill_rows(lambda r: rows[r], len(rows), item.content, W):  # type: ignore[arg-type]
            return [MappingWarning(field_id=field.field_id, code="table_overflow")]
        return []
    items, token = texts(item.content), field.constraints.token or ""
    typed = field.expected_type in TYPED_TYPES
    if target.tag == qn("w:sdt"):  # type: ignore[attr-defined]
        _write_sdt(target, field.write_mode, token, items, typed)  # type: ignore[arg-type]
    else:  # w:p or w:tc
        _write_paragraphs(
            paragraphs_of(target, W) if target.tag == qn("w:tc") else [target],  # type: ignore[attr-defined,arg-type]
            field.write_mode,
            token,
            items,
            typed,
            protected,
        )
    return []


def _write_paragraphs(
    paragraphs: list[etree._Element],
    mode: str,
    token: str,
    items: list[str],
    typed: bool,
    protected: frozenset[etree._Element] = frozenset(),
) -> None:
    if mode == "replace_token":
        replace_token(paragraphs, token, "; ".join(items), W, typed)
    else:
        write_paragraphs(paragraphs, items, W, protected)


def _write_sdt(sdt: etree._Element, mode: str, token: str, items: list[str], typed: bool) -> None:
    pr = sdt.find(qn("w:sdtPr"))
    placeholder = pr.find(qn("w:showingPlcHdr")) if pr is not None else None
    if placeholder is not None:
        pr.remove(placeholder)
    content = sdt.find(qn("w:sdtContent"))
    if content.find(qn("w:p")) is not None:  # block level: paragraph rules
        _write_paragraphs(paragraphs_of(content, W), mode, token, items, typed)
    elif mode == "replace_token":  # inline: the runs sit directly in sdtContent
        replace_token([content], token, "; ".join(items), W, typed)
    else:
        set_paragraph(content, "; ".join(items), W)
    if placeholder is not None:  # the value must not render in the grey placeholder style
        for style in list(content.iter(qn("w:rStyle"))):
            if style.get(qn("w:val")) == "PlaceholderText":
                style.getparent().remove(style)


def write(template: bytes, items: list[WriteItem]) -> tuple[bytes, list[MappingWarning]]:
    doc = Document(BytesIO(template))
    # Resolve every target to its element before the first write: replace_text inserts cloned
    # paragraphs, which would shift any later index-based lookup.
    targets = [_resolve(doc, i.field) for i in items]
    protected = frozenset(
        t for t in targets if isinstance(t, etree._Element) and t.tag == qn("w:sdt")
    )
    warnings: list[MappingWarning] = []
    for item, target in zip(items, targets, strict=True):
        warnings += _write_item(item, target, protected)
    out = BytesIO()
    doc.save(out)
    return out.getvalue(), warnings
