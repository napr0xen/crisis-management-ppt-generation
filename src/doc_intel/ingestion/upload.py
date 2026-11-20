"""Upload validation (allow-list, magic bytes, size) and storage keys (docs/data-model.md)."""

import hashlib
import io
import uuid
import zipfile
from dataclasses import dataclass

from doc_intel.core.errors import PayloadTooLarge, UnsupportedMediaType
from doc_intel.core.tenancy import TenantContext

MIME_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "txt": "text/plain",
    "csv": "text/csv",
    "md": "text/markdown",
}
# shortcut: zip metadata can understate sizes, so this stops honest-looking bombs only; parse in a
# memory-limited subprocess if hostile uploads become a real threat.
MAX_UNZIPPED_BYTES = 512 * 1024 * 1024  # an XML part this large would not fit a parser's memory
MAX_XML_PART_BYTES = 32 * 1024 * 1024  # same as the template per-part cap
OOXML_MAIN_PART = {
    "docx": "word/document.xml",
    "pptx": "ppt/presentation.xml",
    "xlsx": "xl/workbook.xml",
}


@dataclass(frozen=True)
class ValidatedUpload:
    ext: str
    mime_type: str
    data: bytes
    sha256: str

    @property
    def size_bytes(self) -> int:
        return len(self.data)


def _content_matches(ext: str, data: bytes) -> bool:
    if ext == "pdf":
        return data.startswith(b"%PDF-")
    if ext in OOXML_MAIN_PART:
        try:
            members = zipfile.ZipFile(io.BytesIO(data)).infolist()
        except zipfile.BadZipFile:
            return False
        if sum(m.file_size for m in members) > MAX_UNZIPPED_BYTES:
            raise PayloadTooLarge("Unzipped content is too large.")
        # one huge XML part becomes a multi-GB lxml tree in the parser (media parts may be big)
        # shortcut: by part name; an XML part declared under another name in [Content_Types].xml
        # escapes this cap (the 512 MB total still holds), check content types if that appears.
        if any(
            m.file_size > MAX_XML_PART_BYTES and m.filename.endswith((".xml", ".rels"))
            for m in members
        ):
            raise PayloadTooLarge("A part of the file is too large.")
        return {"[Content_Types].xml", OOXML_MAIN_PART[ext]} <= {m.filename for m in members}
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    return True


def validate_upload(filename: str | None, data: bytes, max_bytes: int) -> ValidatedUpload:
    """The extension picks the format; the bytes must match it. Errors never echo the filename."""
    ext = filename.rsplit(".", 1)[1].lower() if filename and "." in filename else ""
    if ext not in MIME_TYPES:
        raise UnsupportedMediaType()
    if len(data) > max_bytes:
        raise PayloadTooLarge()
    if not _content_matches(ext, data):
        raise UnsupportedMediaType("File content does not match its extension.")
    return ValidatedUpload(ext, MIME_TYPES[ext], data, hashlib.sha256(data).hexdigest())


def _project_prefix(ctx: TenantContext) -> str:
    return f"users/{ctx.user_id}/projects/{ctx.project_id}"


def upload_key(ctx: TenantContext, document_id: uuid.UUID, ext: str) -> str:
    return f"{_project_prefix(ctx)}/uploads/{document_id}/original.{ext}"


def parsed_key(ctx: TenantContext, document_id: uuid.UUID) -> str:
    return f"{_project_prefix(ctx)}/parsed/{document_id}.json"


def artifact_key(ctx: TenantContext, artifact_id: uuid.UUID, ext: str) -> str:
    return f"{_project_prefix(ctx)}/artifacts/{artifact_id}.{ext}"


def library_key(template_id: uuid.UUID, ext: str) -> str:
    return f"library/{template_id}/original.{ext}"
