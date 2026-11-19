"""Stage 1 of template intelligence: deterministic AnchorMap extraction (system-design § 4)."""

import re
import zipfile
from io import BytesIO

import structlog

from doc_intel.core.errors import ValidationFailed
from doc_intel.ingestion.pipeline import ParseError
from doc_intel.templates.schemas import MAX_ANCHORS, Anchor, AnchorMap, TemplateFormat

log = structlog.get_logger()

MAX_TEMPLATE_PARTS = 5000
MAX_TEMPLATE_XML_BYTES = 32 * 1024 * 1024

# Bounded inner lengths and no nested quantifiers keep matching linear.
_TOKEN = re.compile(
    r"\{\{[^{}\n]{1,100}\}\}|\[[A-Za-z][^\[\]\n]{0,99}\]|<[A-Za-z][^<>\n]{0,99}>|\bXXX\b|\bTBD\b|_{4,}"
)


def too_many_anchors(n: int) -> ValidationFailed:
    return ValidationFailed(f"The template has {n} fillable locations; the limit is {MAX_ANCHORS}.")


def find_tokens(text: str) -> list[str]:
    """Placeholder tokens in order of appearance, deduplicated."""
    return list(dict.fromkeys(_TOKEN.findall(text)))


def _check_zip(data: bytes) -> None:
    # lxml/openpyxl memory scales with part size; together with zipfile reading at most the
    # declared size this bounds extraction memory.
    # shortcut: limits apply to template extraction only; source ingestion keeps the 512 MB
    # declared-total cap (ingestion/upload.py), add per-part caps there if Docling OOMs.
    try:
        infos = zipfile.ZipFile(BytesIO(data)).infolist()
    except Exception as exc:
        raise ParseError("The template file could not be read.") from exc
    if len(infos) > MAX_TEMPLATE_PARTS:
        raise ValidationFailed(
            f"The template has too many parts; the limit is {MAX_TEMPLATE_PARTS}."
        )
    if any(
        i.file_size > MAX_TEMPLATE_XML_BYTES and i.filename.endswith((".xml", ".rels"))
        for i in infos
    ):
        raise ValidationFailed("The template has a part that is too large.")


def build_anchor_map(fmt: TemplateFormat, data: bytes) -> AnchorMap:
    # Imported here because the extractors import find_tokens from this module.
    from doc_intel.templates import anchors_docx, anchors_pptx, anchors_xlsx

    _check_zip(data)
    extract = {"pptx": anchors_pptx, "docx": anchors_docx, "xlsx": anchors_xlsx}[fmt].extract
    try:
        found = extract(data)
    except ValidationFailed:
        raise
    except Exception as exc:
        # Only the type: exception text may carry document content.
        log.warning("template_parse_failed", fmt=fmt, error_type=type(exc).__name__)
        raise ParseError("The template file could not be read.") from exc
    unique: dict[str, Anchor] = {}
    for anchor in found:
        # shortcut: a duplicate id (e.g. a repeated content-control tag) keeps the first
        # anchor and drops the rest, upgrade when writers must address every duplicate.
        unique.setdefault(anchor.anchor_id, anchor)
    if not unique:
        raise ValidationFailed("The template has no fillable locations.")
    if len(unique) > MAX_ANCHORS:
        raise too_many_anchors(len(unique))
    return AnchorMap(format=fmt, anchors=list(unique.values()))
