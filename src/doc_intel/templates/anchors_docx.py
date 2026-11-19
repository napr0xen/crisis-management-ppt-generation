from io import BytesIO

from docx import Document
from docx.document import Document as DocumentObject
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from doc_intel.templates.anchors import find_tokens
from doc_intel.templates.schemas import Anchor, AnchorKind, TableRef


def _heading_level(par: Paragraph) -> int | None:
    name = par.style.name if par.style is not None else ""
    if name == "Title":
        return 0
    if name.startswith("Heading"):
        digits = name.removeprefix("Heading").strip()
        return int(digits) if digits.isdigit() else 1
    return None


def _anchor(
    anchor_id: str, kind: AnchorKind, path: list[str], text: str, table: TableRef | None = None
) -> Anchor:
    tokens = find_tokens(text)
    return Anchor(
        anchor_id=anchor_id,
        kind=kind,
        path=path,
        text=text,
        tokens=tokens,
        # a content control exists to be filled; anything else only when unfilled
        is_placeholder=bool(tokens) or not text or kind == "content_control",
        table=table,
    )


def _body(doc: DocumentObject) -> list[Anchor]:
    anchors: list[Anchor] = []
    trail: dict[int, str] = {}
    p_idx = t_idx = 0
    prev_heading = False
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            par = Paragraph(child, doc)
            level = _heading_level(par)
            if level is not None:
                trail = {k: v for k, v in trail.items() if k < level}
                trail[level] = par.text
                prev_heading = True
            else:
                if par.text or prev_heading:
                    path = [trail[k] for k in sorted(trail)]
                    anchors.append(_anchor(f"docx:body:p{p_idx}", "paragraph", path, par.text))
                prev_heading = False
            p_idx += 1
        elif child.tag == qn("w:tbl"):
            prev_heading = False
            # shortcut: nested tables are not anchored, upgrade when templates need them.
            table = Table(child, doc)
            path = [*(trail[k] for k in sorted(trail)), f"Table {t_idx}"]
            headers = [c.text.strip() or None for c in table.rows[0].cells]
            seen = set()  # table-wide: a vertical merge repeats its cell in every spanned row
            for r, row in enumerate(table.rows):
                for c, cell in enumerate(row.cells):
                    if cell._tc in seen:  # merged cells repeat; keep the first grid position
                        continue
                    seen.add(cell._tc)  # the element, not id(): proxies' ids get reused
                    ref = TableRef(row=r, col=c, header=headers[c] if c < len(headers) else None)
                    anchors.append(
                        _anchor(f"docx:body:t{t_idx}:r{r}c{c}", "table_cell", path, cell.text, ref)
                    )
            t_idx += 1
    return anchors


def _headers_footers(doc: DocumentObject) -> list[Anchor]:
    anchors: list[Anchor] = []
    for s, section in enumerate(doc.sections, start=1):
        # shortcut: first-page and even-page headers/footers are skipped, upgrade on demand.
        for name, part in (("header", section.header), ("footer", section.footer)):
            if part.is_linked_to_previous:
                continue
            for i, par in enumerate(part.paragraphs):
                if par.text:
                    anchors.append(
                        _anchor(
                            f"docx:sec{s}:{name}:p{i}",
                            f"{name}_paragraph",
                            [f"Section {s}", name.capitalize()],
                            par.text,
                        )
                    )
    return anchors


def _content_controls(doc: DocumentObject) -> list[Anchor]:
    anchors: list[Anchor] = []
    # An inline sdt overlaps its paragraph's anchor; Phase 7 writers must handle that.
    # shortcut: content controls in headers/footers are not scanned, and paragraphs inside a
    # block-level w:sdt appear only as that content_control anchor, upgrade when templates
    # need them.
    for sdt in doc.element.body.iter(qn("w:sdt")):
        tag = sdt.find(f"{qn('w:sdtPr')}/{qn('w:tag')}")
        if tag is None or not tag.get(qn("w:val")):
            continue
        content = sdt.find(qn("w:sdtContent"))
        text = "".join(t.text or "" for t in content.iter(qn("w:t"))) if content is not None else ""
        anchors.append(_anchor(f"docx:sdt:{tag.get(qn('w:val'))}", "content_control", [], text))
    return anchors


def extract(data: bytes) -> list[Anchor]:
    doc = Document(BytesIO(data))
    return [*_body(doc), *_headers_footers(doc), *_content_controls(doc)]
