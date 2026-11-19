"""Stage 1 of template intelligence: deterministic AnchorMap extraction (system-design § 4)."""

import re

from doc_intel.core.errors import ValidationFailed
from doc_intel.ingestion.pipeline import ParseError
from doc_intel.templates.schemas import MAX_ANCHORS, Anchor, AnchorMap, TemplateFormat

# Bounded inner lengths and no nested quantifiers keep matching linear.
_TOKEN = re.compile(
    r"\{\{[^{}\n]{1,100}\}\}|\[[^\[\]\n]{1,100}\]|<[^<>\n]{1,100}>|\bXXX\b|\bTBD\b|_{4,}"
)


def find_tokens(text: str) -> list[str]:
    """Placeholder tokens in order of appearance, deduplicated."""
    return list(dict.fromkeys(_TOKEN.findall(text)))


def build_anchor_map(fmt: TemplateFormat, data: bytes) -> AnchorMap:
    # Imported here because the extractors import find_tokens from this module.
    from doc_intel.templates import anchors_docx, anchors_pptx, anchors_xlsx

    extract = {"pptx": anchors_pptx, "docx": anchors_docx, "xlsx": anchors_xlsx}[fmt].extract
    try:
        found = extract(data)
    except Exception as exc:
        raise ParseError("The template file could not be read.") from exc
    unique: dict[str, Anchor] = {}
    for anchor in found:
        # shortcut: a duplicate id (e.g. a repeated content-control tag) keeps the first
        # anchor and drops the rest, upgrade when writers must address every duplicate.
        unique.setdefault(anchor.anchor_id, anchor)
    if not unique:
        raise ValidationFailed("The template has no fillable locations.")
    if len(unique) > MAX_ANCHORS:
        raise ValidationFailed(
            f"The template has {len(unique)} fillable locations; the limit is {MAX_ANCHORS}."
        )
    return AnchorMap(format=fmt, anchors=list(unique.values()))
