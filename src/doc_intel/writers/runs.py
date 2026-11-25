"""Paragraph and run rules shared by the PPTX (`a:`) and DOCX (`w:`) writers (system-design § 8).

Everything here works on lxml elements. A "container" is the element whose direct `p` children
are the paragraphs of an anchor: `a:txBody`, `w:tc`, or `w:sdtContent`.
"""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass

from lxml import etree

from doc_intel.templates.anchors import find_tokens
from doc_intel.writers import Content

_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


@dataclass(frozen=True)
class Flavor:
    """Tag names of one markup family."""

    ns: str
    # DrawingML keeps the paragraph mark's run properties in `a:endParaRPr`; WordprocessingML
    # keeps them in `w:pPr/w:rPr`.
    mark_in_ppr: bool

    def q(self, name: str) -> str:
        return f"{{{self.ns}}}{name}"


A = Flavor("http://schemas.openxmlformats.org/drawingml/2006/main", mark_in_ppr=False)
W = Flavor("http://schemas.openxmlformats.org/wordprocessingml/2006/main", mark_in_ppr=True)


def texts(content: Content) -> list[str]:
    if not isinstance(content, list) or any(not isinstance(i, str) for i in content):
        raise TypeError("PPTX/DOCX writers take a list of strings")
    return content  # type: ignore[return-value]


def _mark_rpr(p: etree._Element, fl: Flavor) -> etree._Element | None:
    return p.find(fl.q("pPr") + "/" + fl.q("rPr")) if fl.mark_in_ppr else p.find(fl.q("endParaRPr"))


def _run_text(r: etree._Element, fl: Flavor) -> str:
    return "".join(t.text or "" for t in r.findall(fl.q("t")))


def _set_run(r: etree._Element, text: str, fl: Flavor) -> None:
    # shortcut: only a:t/w:t text is rewritten; tabs, breaks and fields inside the touched run
    # are dropped and "\n" stays raw in the text node, upgrade to a:br/w:br when templates need it.
    rpr = r.find(fl.q("rPr"))
    for child in list(r):
        if child is not rpr:
            r.remove(child)
    t = etree.SubElement(r, fl.q("t"))
    t.text = text
    if fl is W:  # only w:t has xml:space; a:t does not
        t.set(_XML_SPACE, "preserve")


def set_paragraph(
    p: etree._Element, text: str, fl: Flavor, protected: frozenset = frozenset()
) -> None:
    """Put `text` in the first run (its rPr kept); every other child except pPr/endParaRPr goes.

    `protected` elements (inline content controls that are another item's target) stay put.
    """
    first = p.find(fl.q("r"))
    if first is None:
        first = etree.Element(fl.q("r"))
        mark = _mark_rpr(p, fl)
        if mark is not None:
            rpr = deepcopy(mark)
            rpr.tag = fl.q("rPr")
            first.append(rpr)
    keep = {fl.q("pPr"), fl.q("endParaRPr")}
    for child in list(p):
        if child is not first and child.tag not in keep and child not in protected:
            p.remove(child)
    if first.getparent() is None:
        end = p.find(fl.q("endParaRPr"))
        if end is None:
            p.append(first)
        else:
            end.addprevious(first)
    _set_run(first, text, fl)


def _clone_after(p: etree._Element, text: str, fl: Flavor) -> etree._Element:
    clone = deepcopy(p)
    for sect in clone.findall(fl.q("pPr") + "/" + fl.q("sectPr")):  # would add a section break
        sect.getparent().remove(sect)
    for attr in list(clone.attrib):  # Word's w14:paraId must stay unique
        if attr.endswith(("}paraId", "}textId")):
            del clone.attrib[attr]
    set_paragraph(clone, text, fl)
    p.addnext(clone)
    return clone


def paragraphs_of(container: etree._Element, fl: Flavor) -> list[etree._Element]:
    return container.findall(fl.q("p"))


def write_paragraphs(
    paragraphs: list[etree._Element],
    items: list[str],
    fl: Flavor,
    protected: frozenset = frozenset(),
) -> None:
    """replace_text: one paragraph per item, styled like the first paragraph."""
    first, *rest = paragraphs
    for p in rest:
        p.getparent().remove(p)
    items = items or [""]
    set_paragraph(first, items[0], fl, protected)
    last = first
    for item in items[1:]:
        last = _clone_after(last, item, fl)


def _trim_marker(value: str, before: str, after: str) -> str:
    """Drop the value's non-numeric prefix/suffix the template already has beside the token."""
    digits = [i for i, c in enumerate(value) if c.isdigit()]
    if not digits:
        return value
    start, end = digits[0], digits[-1] + 1
    if start and value[start - 1] == "-":  # the sign belongs to the number
        start -= 1
    prefix, suffix = value[:start].strip(), value[end:].strip()
    if prefix and before.rstrip().endswith(prefix):
        value = value[start:]
        end -= start
    rest = after.lstrip()
    # whole word only: "M" is not trimmed before "Managed"
    if suffix and rest.startswith(suffix) and not rest[len(suffix) : len(suffix) + 1].isalnum():
        value = value[:end]
    return value


def replace_token(
    paragraphs: list[etree._Element], token: str, value: str, fl: Flavor, typed: bool = False
) -> None:
    """replace_token: merge the runs the token spans into the first one, then replace in place.

    `typed` values (number, currency, percent, date) lose a marker the template already has."""
    found = False
    for p in paragraphs:
        pos = 0
        while True:
            runs = list(p.iter(fl.q("r")))
            run_texts = [_run_text(r, fl) for r in runs]
            full = "".join(run_texts)
            start = full.find(token, pos)
            if start < 0:
                break
            found = True
            end = start + len(token)
            put = _trim_marker(value, full[:start], full[end:]) if typed else value
            # the runs overlapping [start, end)
            offset, spanning = 0, []
            for r, text in zip(runs, run_texts, strict=True):
                if offset < end and offset + len(text) > start:
                    spanning.append((r, offset, text))
                offset += len(text)
            first, first_off, first_text = spanning[0]
            _, last_off, last_text = spanning[-1]
            merged = first_text[: start - first_off] + put + last_text[end - last_off :]
            for r, _, _ in spanning[1:]:
                r.getparent().remove(r)
            _set_run(first, merged, fl)
            pos = start + len(put)
    if not found:
        raise KeyError("token_not_found")


def _clear_tokens(cell: etree._Element, fl: Flavor) -> None:
    """Remove every placeholder token left in a cell the content did not write; text stays."""
    ps = paragraphs_of(cell, fl)
    full = "\n".join("".join(_run_text(r, fl) for r in p.iter(fl.q("r"))) for p in ps)
    for token in sorted(find_tokens(full), key=len, reverse=True):
        replace_token(ps, token, "", fl)


def fill_rows(
    cells_of: Callable[[int], list[etree._Element]],
    n_rows: int,
    content: Content,
    fl: Flavor,
) -> bool:
    """fill_table_rows from row 1 down into unique cells; True when values did not fit."""
    marker = bool(content) and isinstance(content[0], str)
    if marker:  # a MISSING/CONFLICT marker
        write_paragraphs(paragraphs_of(cells_of(1)[0], fl), texts(content), fl)
    overflow = False
    written = {(1, 0)} if marker else set()
    for i, row in [] if marker else enumerate(content):
        r = i + 1
        if r >= n_rows:
            overflow = True
            break
        cells = cells_of(r)
        overflow = overflow or len(row) > len(cells)
        for c, (cell, value) in enumerate(zip(cells, row, strict=False)):  # type: ignore[arg-type]
            write_paragraphs(paragraphs_of(cell, fl), [value], fl)
            written.add((r, c))
    for r in range(1, n_rows):
        for c, cell in enumerate(cells_of(r)):
            if (r, c) not in written:
                _clear_tokens(cell, fl)
    return overflow
