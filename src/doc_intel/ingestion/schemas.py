"""Shared ingestion contract: parsers produce `ParsedDocument`, the chunker emits `ChunkPayload`.

Field names match the Qdrant payload in docs/data-model.md; `user_id`/`project_id` are added at
upsert time (Phase 3) from the job's tenant.
"""

from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

ElementType = Literal["text", "table"]  # chunk kinds
ParsedType = Literal["text", "table", "kv"]
Region = Literal["header", "kv", "table", "summary", "footer", "body"]


class ParsedElement(BaseModel):
    """One paragraph or one table, in reading order."""

    model_config = ConfigDict(extra="forbid")

    element_type: ParsedType
    text: str = ""  # paragraph text; "" for tables
    rows: list[list[str]] | None = None  # tables only; rows[0] is the header row
    page_number: int | None = None  # PDF page / PPTX slide; None for docx/xlsx/csv/txt/md
    section_heading: str | None = None  # nearest heading (sheet name for xlsx)
    region: Region = "body"  # Phase 27 layout pass (PDF only)
    bbox: tuple[float, float, float, float] | None = None  # l, t, r, b; PDF points, top-left origin
    label: str | None = None  # kv only
    value: str | None = None  # kv only
    caption: str | None = None  # tables only: a banner row 0 taken out of `rows`


class ParsedDocument(BaseModel):
    """Parser output, stored as `parsed/{document_id}.json`."""

    model_config = ConfigDict(extra="forbid")

    elements: list[ParsedElement]
    page_count: int | None = None


class TableRow(BaseModel):
    """One data row of a table chunk: provenance id and cells keyed by column header."""

    model_config = ConfigDict(extra="forbid")

    id: str
    cells: dict[str, str]
    row_role: Literal["data", "carry_forward", "total"] = "data"


class ChunkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    filename: str
    page_number: int | None
    section_heading: str | None
    element_type: ElementType
    chunk_index: int
    text: str
    block_ids: list[str] = []  # provenance ids of the text blocks in a text chunk
    rows: list[TableRow] | None = None  # table chunks: the data rows in `text`, in line order


class Parser(Protocol):
    def __call__(self, data: bytes) -> ParsedDocument: ...
