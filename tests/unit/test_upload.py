import hashlib
import io
import uuid
import zipfile

import pytest

from doc_intel.core.errors import PayloadTooLarge, UnsupportedMediaType
from doc_intel.core.tenancy import TenantContext
from doc_intel.ingestion.upload import parsed_key, upload_key, validate_upload

LIMIT = 1024


def ooxml(*parts: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for part in ("[Content_Types].xml", *parts):
            z.writestr(part, "<x/>")
    return buf.getvalue()


VALID = {
    "report.pdf": (b"%PDF-1.7\n...", "application/pdf"),
    "memo.docx": (
        ooxml("word/document.xml"),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    "deck.pptx": (
        ooxml("ppt/presentation.xml"),
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    "sheet.xlsx": (
        ooxml("xl/workbook.xml"),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    "notes.txt": ("café notes".encode(), "text/plain"),
    "data.csv": (b"\xef\xbb\xbfa,b\n1,2\n", "text/csv"),
    "readme.md": (b"# Title\n\nbody", "text/markdown"),
}


@pytest.mark.parametrize("filename", VALID)
def test_allowed_formats_are_accepted(filename):
    data, mime = VALID[filename]
    upload = validate_upload(filename, data, LIMIT)
    assert upload.ext == filename.rsplit(".", 1)[1]
    assert upload.mime_type == mime
    assert upload.size_bytes == len(data)
    assert upload.sha256 == hashlib.sha256(data).hexdigest()
    assert upload.data == data


def test_extension_is_case_insensitive():
    assert validate_upload("REPORT.PDF", b"%PDF-1.4", LIMIT).ext == "pdf"


@pytest.mark.parametrize(
    "filename, data",
    [
        ("legacy.doc", b"\xd0\xcf\x11\xe0"),
        ("legacy.ppt", b"\xd0\xcf\x11\xe0"),
        ("legacy.xls", b"\xd0\xcf\x11\xe0"),
        ("run.exe", b"MZ"),
        ("noext", b"%PDF-1.7"),
        (None, b"%PDF-1.7"),
        ("", b"%PDF-1.7"),
        # magic bytes that don't match the extension
        ("fake.pdf", b"hello"),
        ("fake.docx", b"%PDF-1.7"),
        ("fake.docx", ooxml("ppt/presentation.xml")),
        ("fake.pptx", ooxml("word/document.xml")),
        ("fake.xlsx", ooxml("word/document.xml")),
        ("no_content_types.docx", b"PK" + b"\x00" * 20),
        ("bad.txt", b"\xff\xfe\xfa"),
        ("nul.txt", b"abc\x00def"),
        ("nul.csv", b"a,b\x00"),
        ("bin.md", b"%PDF-1.7\x00\x01"),
    ],
)
def test_unsupported_or_mismatched_is_415(filename, data):
    with pytest.raises(UnsupportedMediaType):
        validate_upload(filename, data, LIMIT)


def test_zip_without_content_types_is_415():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<x/>")
    with pytest.raises(UnsupportedMediaType):
        validate_upload("a.docx", buf.getvalue(), LIMIT)


def test_size_limit_is_413():
    validate_upload("a.txt", b"x" * LIMIT, LIMIT)
    with pytest.raises(PayloadTooLarge):
        validate_upload("a.txt", b"x" * (LIMIT + 1), LIMIT)


def test_storage_keys_follow_the_data_model():
    ctx = TenantContext(user_id="user_1", project_id=uuid.UUID(int=1))
    doc = uuid.UUID(int=2)
    assert upload_key(ctx, doc, "pdf") == (
        f"users/user_1/projects/{ctx.project_id}/uploads/{doc}/original.pdf"
    )
    assert parsed_key(ctx, doc) == f"users/user_1/projects/{ctx.project_id}/parsed/{doc}.json"


def test_zip_bomb_is_rejected_before_parsing(monkeypatch):
    """A small OOXML upload must not expand into more than the unzip cap."""
    from doc_intel.ingestion import upload

    monkeypatch.setattr(upload, "MAX_UNZIPPED_BYTES", 1_000_000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<x/>")
        z.writestr("xl/workbook.xml", "<x/>")
        z.writestr("xl/worksheets/sheet1.xml", b"0" * 2_000_000)  # ~2 KB compressed
    data = buf.getvalue()
    assert len(data) < 10_000
    with pytest.raises(PayloadTooLarge):
        validate_upload("bomb.xlsx", data, LIMIT * 100)


def test_unzip_cap_default_is_512_mb():
    from doc_intel.ingestion.upload import MAX_UNZIPPED_BYTES

    assert MAX_UNZIPPED_BYTES == 512 * 1024 * 1024


def test_one_oversized_xml_part_is_413_but_big_media_is_not(monkeypatch):
    from doc_intel.ingestion import upload

    monkeypatch.setattr(upload, "MAX_XML_PART_BYTES", 1_000)

    def docx(part: str) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", "<x/>")
            z.writestr("word/document.xml", "<x/>")
            z.writestr(part, b"0" * 2_000)
        return buf.getvalue()

    for part in ("word/styles.xml", "word/Styles.XML", "word/_rels/x.RELS"):  # names ignore case
        with pytest.raises(PayloadTooLarge):
            validate_upload("a.docx", docx(part), LIMIT * 100)
    assert validate_upload("a.docx", docx("word/media/clip.mp4"), LIMIT * 100).ext == "docx"
