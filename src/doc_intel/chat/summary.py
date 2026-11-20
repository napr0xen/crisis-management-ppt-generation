"""Chat markdown for `POST /v1/pipe/chat` (system-design § 9.5-9.6). Pure functions, no I/O."""

import re
from typing import Any

from doc_intel.chat.qa import Answer
from doc_intel.chat.router import TEMPLATE_FORMATS, doc_format
from doc_intel.db.models import Document
from doc_intel.mapping.schemas import Evidence, FieldMappingResult, Value
from doc_intel.templates.schemas import TemplateIR

HELP_TEXT = (
    "I answer questions about your documents and fill templates from them.\n\n"
    "- Attach documents (PDF, DOCX, PPTX, XLSX, TXT, CSV, MD) and ask a question. "
    "Answers come with cited sources.\n"
    '- Attach a PPTX, DOCX or XLSX template and say "fill the template". I fill it from your '
    "documents and give you a summary and a download link.\n\n"
    "A value is only filled in when it is backed by a verified quote from your documents."
)

_QUOTE_CHARS = 300
_VALUE_CHARS = 200
_REASONS = {
    "no_relevant_chunks": "no relevant passages found",
    "not_found_by_llm": "not stated in the documents",
    "evidence_failed_verification": "the quote could not be verified",
    "insufficient_evidence": "not enough evidence",
    "llm_error": "the model failed",
    "cleared_by_user": "cleared by user",
}
_WARNINGS = {
    "max_chars_exceeded": "the value exceeds the character limit of the field",
    "formula_cell": "the cell contains a formula and was left unchanged",
    "table_overflow": "the table has fewer rows than the data, extra rows were not written",
}


def _md(text: str) -> str:
    """Escape markdown/HTML metacharacters: Open WebUI renders document-derived text, so a quote
    like `![x](https://evil/p.png)` would otherwise fire an image request."""
    return re.sub(r"([\\`*_\[\]()!<>#|])", r"\\\1", text)


def _answer_md(text: str) -> str:
    """LLM answer: stays markdown (for the [n] markers) but loses images, links and raw HTML."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return text.replace("<", "\\<").replace(">", "\\>")


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text[:limit] + "..." if len(text) > limit else text


def _cite(filename: str, page: int | None) -> str:
    name = _md(filename)
    return f"{name} p.{page}" if page is not None else name


def _value(value: Value) -> str:
    if isinstance(value, list):
        value = "; ".join(" | ".join(r) if isinstance(r, list) else r for r in value)
    return _md(_clip(str(value), _VALUE_CHARS))


def render_answer(answer: Answer) -> str:
    """Verified answer, then a source list: `[n] filename p.N: "quote"`."""
    body = _answer_md(answer.answer_markdown)
    if not answer.citations:
        return body
    lines = [
        f'[{c.marker}] {_cite(c.filename, c.page_number)}: "{_md(_clip(c.quote, _QUOTE_CHARS))}"'
        for c in sorted(answer.citations, key=lambda c: c.marker)
    ]
    return body + "\n\n**Sources**\n\n" + "\n".join(lines)


def render_summary(
    results: dict[str, FieldMappingResult],
    summary: dict[str, Any],
    ir: TemplateIR,
    download_url: str | None,
) -> str:
    """Counts by status, every missing field (label + reason), every conflict (each candidate
    value + citations), partial fields, warnings, and the signed download link."""
    labels = {f.field_id: f.label for f in ir.fields}

    def label(field_id: str) -> str:
        return _md(labels.get(field_id, field_id))

    def cites(evidence: list[Evidence]) -> str:
        return ", ".join(_cite(e.filename, e.page_number) for e in evidence)

    counts = ", ".join(f"{summary[k]} {k}" for k in ("populated", "partial", "missing", "conflict"))
    out = [f"**Template mapping finished:** {counts}."]
    # headings start with a blank line; the lines join with single newlines
    missing = [r for r in results.values() if r.status == "missing"]
    conflicts = [
        (r, r.conflict_details)
        for r in results.values()
        if r.status == "conflict" and r.conflict_details
    ]
    partial = [r for r in results.values() if r.status == "partial" and r.value is not None]
    if missing:
        out.append("\n**Missing**")
        out += [
            f"- {label(r.field_id)}: {_REASONS.get(r.missing_reason or '', 'unknown reason')}"
            for r in missing
        ]
    if conflicts:
        out.append("\n**Conflicts**")
        for r, details in conflicts:
            out.append(f"- {label(r.field_id)}")
            out += [f"  - {_value(c.value)} ({cites(c.evidence)})" for c in details.candidates]
        out.append("\nConflicts are never auto-resolved: please choose the correct value.")
    if partial:
        out.append("\n**Partial**")
        out += [f"- {label(r.field_id)}: {_value(r.value)}" for r in partial if r.value is not None]
    if summary.get("warnings"):
        out.append("\n**Warnings**")
        out += [
            f"- {label(w['field_id'])}: {_WARNINGS.get(w['code'], w['code'])}"
            for w in summary["warnings"]
        ]
    if download_url is None:
        out.append("\nNo file was produced.")
    else:
        out.append(f"\n[Download the filled template]({download_url})")
        out.append("The download link expires, so download the file soon.")
    return "\n".join(out)


def render_candidates(documents: list[Document]) -> str:
    """Numbered list of template-capable documents (pptx/docx/xlsx) asking the user to pick one.
    No candidates at all -> ask the user to upload a PPTX, DOCX or XLSX template."""
    templates = [d for d in documents if doc_format(d) in TEMPLATE_FORMATS]
    if not templates:
        return "I found no template. Please attach a PPTX, DOCX or XLSX template to fill."
    lines = [f"{i}. {_md(d.filename)}" for i, d in enumerate(templates, 1)]
    return (
        "Which template should I fill?\n\n"
        + "\n".join(lines)
        + "\n\nReply with the number or the filename of the template to fill."
    )
