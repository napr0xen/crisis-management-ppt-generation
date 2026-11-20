"""Stage 4 surgical writers (docs/system-design.md § 8).

`render` is the one entry point: it turns each IR field's mapping result into writer content
(`markers.content`) and patches a copy of the template bytes. Pure and synchronous: the caller
runs it with `asyncio.to_thread`.
"""

from collections.abc import Callable
from dataclasses import dataclass

from doc_intel.mapping.schemas import FieldMappingResult, MappingWarning
from doc_intel.templates.schemas import IRField, TemplateFormat

# list[str]: paragraphs (one per item); list[list[str]]: table rows (fill_table_rows);
# int | float: a typed xlsx value (numbers, percent as a fraction, dates as Excel serials).
Content = list[str] | list[list[str]] | int | float


@dataclass(frozen=True)
class WriteItem:
    field: IRField
    content: Content


Writer = Callable[[bytes, list[WriteItem]], tuple[bytes, list[MappingWarning]]]


def render(
    fmt: TemplateFormat,
    template: bytes,
    fields: list[IRField],
    results: dict[str, FieldMappingResult],
    *,
    missing_marker: str,
    conflict_marker: str,
) -> tuple[bytes, list[MappingWarning]]:
    """The filled document and the writer warnings (`formula_cell`, `table_overflow`)."""
    # Imported here: the format writers import WriteItem from this package.
    from doc_intel.writers import docx_writer, pptx_writer, xlsx_writer
    from doc_intel.writers.markers import content

    writers: dict[str, Writer] = {
        "pptx": pptx_writer.write,
        "docx": docx_writer.write,
        "xlsx": xlsx_writer.write,
    }
    items = [
        WriteItem(f, content(f, results[f.field_id], fmt, missing_marker, conflict_marker))
        for f in fields
    ]
    # Longer tokens first: replacing a token that is a substring of another ("____" in "______")
    # would destroy the longer one. Safe because pptx/docx writers resolve every target first.
    # shortcut: a value containing a later token's text on the same anchor is replaced again,
    # upgrade by substituting all tokens of an anchor in one pass.
    items.sort(
        key=lambda i: (
            -len(i.field.constraints.token or "") if i.field.write_mode == "replace_token" else 0
        )
    )
    return writers[fmt](template, items)
