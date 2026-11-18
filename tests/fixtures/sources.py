"""Synthetic source documents built at test time, so no binaries live in git."""

from io import BytesIO

from docx import Document
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.util import Inches

TABLE = [["Region", "Status", "Owner"], ["North", "Contained", "Alice"], ["South", "Open", "Bob"]]


def _pdf(page_streams: list[bytes]) -> bytes:
    """Minimal PDF: one Helvetica font, one content stream per page."""
    n = len(page_streams)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
    ]
    font_id = 3 + 2 * n
    for i, stream in enumerate(page_streams):
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {4 + 2 * i} 0 R >>".encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, obj)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def _lines(lines: list[tuple[int, int, str]]) -> bytes:
    """(font size, y, text) -> PDF text operators."""
    return b"\n".join(b"BT /F1 %d Tf 72 %d Td (%s) Tj ET" % (s, y, t.encode()) for s, y, t in lines)


def text_pdf() -> bytes:
    return _pdf(
        [
            _lines(
                [
                    (24, 700, "Incident Overview"),
                    (12, 660, "The flood response began on Monday morning."),
                    (12, 644, "Evacuation centres opened in three districts."),
                ]
            ),
            _lines(
                [
                    (12, 700, "Recovery funding was approved by the council."),
                    (12, 684, "Power was restored to most homes within a week."),
                ]
            ),
        ]
    )


def scanned_pdf() -> bytes:
    img = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(img)
    draw.text((100, 150), "Shelter Report", fill="black", font=ImageFont.load_default(size=64))
    body = ImageFont.load_default(size=36)
    for i, line in enumerate(
        [
            "Volunteers distributed blankets and hot meals",
            "to every family that arrived at the shelter",
            "during the first night of the evacuation.",
        ]
    ):
        draw.text((100, 400 + 50 * i), line, fill="black", font=body)
    buf = BytesIO()
    img.save(buf, "PDF", resolution=150)
    return buf.getvalue()


def docx() -> bytes:
    doc = Document()
    doc.add_heading("Executive Summary", level=1)
    doc.add_paragraph("The storm caused widespread outages.")
    doc.add_paragraph("Crews worked through the night.")
    doc.add_heading("Regional Status", level=1)
    doc.add_paragraph("Each region reported its status below.")
    table = doc.add_table(rows=3, cols=3)
    for r, row in enumerate(TABLE):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def pptx() -> bytes:
    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[1])
    s1.shapes.title.text = "Situation Update"
    s1.placeholders[1].text = "Rivers peaked overnight."
    s2 = prs.slides.add_slide(prs.slide_layouts[5])
    s2.shapes.title.text = "Response Teams"
    shape = s2.shapes.add_table(3, 3, Inches(1), Inches(2), Inches(6), Inches(2))
    for r, row in enumerate(TABLE):
        for c, value in enumerate(row):
            shape.table.cell(r, c).text = value
    buf = BytesIO()
    prs.save(buf)
    return buf.getvalue()


def xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Shelters"
    ws.append(["Name", "Capacity"])
    ws.append(["Town Hall", 120])
    ws.append([None, None])
    ws.append(["School Gym", 80])
    ws2 = wb.create_sheet("Supplies")
    ws2.append(["Item", "Count"])
    ws2.append(["Water", 500])
    wb.create_sheet("Empty")
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv_() -> bytes:
    return "﻿Region,Status,Owner\nNorth,Contained,Alice\nSouth,Open,Bob\n".encode()


def txt() -> bytes:
    return b"First paragraph line one.\nline two.\n\n\nSecond paragraph.\n"


def md() -> bytes:
    return (
        b"Intro before any heading.\n\n"
        b"# Overview\n\nThe overview text.\n\nMore overview.\n\n"
        b"## Details\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    )
