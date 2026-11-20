"""Chat markdown for `POST /v1/pipe/chat` (system-design § 9.5-9.6). Pure functions, no I/O."""

import re
from typing import Any

from doc_intel.chat.edits import EditOp
from doc_intel.chat.qa import Answer
from doc_intel.chat.router import TEMPLATE_FORMATS, doc_format
from doc_intel.db.models import Document, LibraryTemplate
from doc_intel.mapping.schemas import Evidence, FieldMappingResult, Value
from doc_intel.templates.schemas import TemplateIR

HELP_TEXT = (
    "I answer questions about your documents and fill templates from them.\n\n"
    "- Attach documents (PDF, DOCX, PPTX, XLSX, TXT, CSV, MD) and ask a question. "
    "Answers come with cited sources.\n"
    '- Attach a PPTX, DOCX or XLSX template and say "fill the template". I fill it from your '
    "documents and give you a summary and a download link.\n"
    "- Without an attached template, ask for one, for example "
    '"make a powerpoint out of this", and pick a template from the library.\n'
    '- After a template is filled, edit it in chat, for example "set the owner to Ann", '
    '"use the first value for customers affected" or "clear the date". Each edit makes a new '
    "version; earlier versions stay downloadable.\n\n"
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


def md(text: str) -> str:
    """Escape markdown/HTML metacharacters: Open WebUI renders document-derived text, so a quote
    like `![x](https://evil/p.png)` would otherwise fire an image request."""
    # one line, `~` escaped: no fence (an Open WebUI html artifact) can open; `:\/\/` breaks
    # bare-URL autolinks
    text = re.sub(r"([\\`*_\[\]()!<>#|~])", r"\\\1", " ".join(text.split()))
    return text.replace("://", r":\/\/")


# backticks and `~` too: a fence would make the escapes literal and its HTML live
_ANSWER_META = re.compile(r"([\\\[\]<>`~])")
# a plain [n] marker, never one that would open an inline link, a reference link or a definition
_MARKER = re.compile(r"\\\[(\d+)\\\](?![(:]|\\\[)")


def _answer_md(text: str) -> str:
    """LLM answer: keeps emphasis, lists and tables, but no link, image, reference definition or
    raw HTML can form: every bracket and backslash is escaped, then only [n] markers restored."""
    # cosmetic: drop inline images and keep inline link text; the escaping below is the guard
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return _MARKER.sub(r"[\1]", _ANSWER_META.sub(r"\\\1", text)).replace("://", r":\/\/")


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text[:limit] + "..." if len(text) > limit else text


def _cite(filename: str, page: int | None) -> str:
    name = md(filename)
    return f"{name} p.{page}" if page is not None else name


def _value(value: Value) -> str:
    if isinstance(value, list):
        value = "; ".join(" | ".join(r) if isinstance(r, list) else r for r in value)
    return md(_clip(str(value), _VALUE_CHARS))


def render_answer(answer: Answer) -> str:
    """Verified answer, then a source list: `[n] filename p.N: "quote"`."""
    body = _answer_md(answer.answer_markdown)
    if not answer.citations:
        return body
    lines = [
        f'[{c.marker}] {_cite(c.filename, c.page_number)}: "{md(_clip(c.quote, _QUOTE_CHARS))}"'
        for c in sorted(answer.citations, key=lambda c: c.marker)
    ]
    return body + "\n\n**Sources**\n\n" + "\n".join(lines)


def render_edits(ops: list[EditOp], ir: TemplateIR) -> list[str]:
    """One line per applied edit op, by field label (labels and values escaped)."""
    labels = {f.field_id: f.label for f in ir.fields}
    lines = []
    for op in ops:
        name = f"**{md(labels.get(op.field_id, op.field_id))}**"
        if op.op == "set_value":
            lines.append(f"- Set {name} to {_value(op.value)}")
        elif op.op == "choose_candidate":
            lines.append(f"- Used value {op.candidate_index + 1} for {name}")
        else:
            lines.append(f"- Cleared {name}")
    return lines


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
        return md(labels.get(field_id, field_id))

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
            out += [
                f"  {n}. {_value(c.value)} ({cites(c.evidence)})"
                for n, c in enumerate(details.candidates, start=1)
            ]
        out.append(
            "\nConflicts are never auto-resolved: reply with the value to use, for example "
            '"use the first value for customers affected".'
        )
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
    lines = [f"{i}. {md(d.filename)}" for i, d in enumerate(templates, 1)]
    return (
        "Which template should I fill?\n\n"
        + "\n".join(lines)
        + "\n\nReply with the number or the filename of the template to fill."
    )


def render_library(templates: list[LibraryTemplate]) -> str:
    """Numbered library templates to pick from. `library_choice` resolves a number reply by
    matching these exact line starts, so change both together."""
    lines = [
        f"{i}. {md(t.name)} ({t.format})"
        + (f": {md(_clip(t.description, 200))}" if t.description else "")
        for i, t in enumerate(templates, 1)
    ]
    return (
        "No template is attached to this chat. Which library template should I use?\n\n"
        + "\n".join(lines)
        + "\n\nReply with the number or the name of the template."
    )
