"""Synthetic template files built at test time, so no binaries live in git."""

from io import BytesIO

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches


def _sdt(tag: str | None, text: str):
    tag_xml = f'<w:tag w:val="{tag}"/>' if tag else ""
    return parse_xml(
        f"<w:sdt {nsdecls('w')}><w:sdtPr>{tag_xml}</w:sdtPr><w:sdtContent>"
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:sdtContent></w:sdt>"
    )


def pptx_template() -> bytes:
    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[1])  # Title and Content
    s1.shapes.title.text = "Situation Overview"
    s2 = prs.slides.add_slide(prs.slide_layouts[5])  # Title Only
    s2.shapes.title.text = "Incident"
    box = s2.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1))
    box.text_frame.text = "Date {{incident_date}} status TBD"
    s3 = prs.slides.add_slide(prs.slide_layouts[5])
    s3.shapes.title.text = "Stakeholders"
    s3.shapes.add_table(3, 3, Inches(1), Inches(2), Inches(6), Inches(2))
    table = s3.shapes[-1].table
    for col, head in enumerate(["Name", "Role", "Owner"]):
        table.cell(0, col).text = head
    group = s3.shapes.add_group_shape()
    group.shapes.add_textbox(Inches(1), Inches(5), Inches(3), Inches(1)).text_frame.text = "[note]"
    out = BytesIO()
    prs.save(out)
    return out.getvalue()


def docx_template() -> bytes:
    doc = Document()
    doc.add_paragraph("Incident Report", style="Title")
    doc.add_heading("Summary", level=1)
    doc.add_paragraph("")  # blank under a heading
    para = doc.add_paragraph()
    for part in ("Client: {{", "client_name", "}}"):
        para.add_run(part)
    doc.add_paragraph("Signature ____")
    doc.add_paragraph("")  # blank not under a heading, skipped
    table = doc.add_table(rows=2, cols=3)
    for col, head in enumerate(["Item", "Owner", "Date"]):
        table.cell(0, col).text = head
    table.cell(1, 0).merge(table.cell(1, 1))
    section = doc.sections[0]
    section.header.is_linked_to_previous = False
    section.header.paragraphs[0].text = "Confidential <classification>"
    section.footer.is_linked_to_previous = False
    section.footer.paragraphs[0].text = "Page footer"
    sect_pr = doc.element.body.sectPr
    sect_pr.addprevious(_sdt("owner", "Owner name"))
    sect_pr.addprevious(_sdt(None, "Untagged"))
    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def xlsx_template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws["A1"], ws["A2"], ws["A3"] = "Metric", "Cases", "Deaths"
    ws["B1"] = "Value"
    ws["A4"], ws["B4"] = "Total", "=SUM(B2:B3)"
    ws["D1"] = "[client name]"
    ws["A6"] = "Notes"
    ws.merge_cells("A7:B8")
    q1 = wb.create_sheet("Q1!Data")
    q1["A1"] = "Region"
    out = BytesIO()
    wb.save(out)
    return out.getvalue()
