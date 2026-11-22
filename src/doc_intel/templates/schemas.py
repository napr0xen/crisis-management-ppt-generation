"""AnchorMap and TemplateIR contracts (docs/system-design.md § 4-5).

Phase 6 mapping and Phase 7 writers read these shapes; change them only with the docs.
"""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

TemplateFormat = Literal["pptx", "docx", "xlsx"]
AnchorKind = Literal[
    "shape_text",
    "table_cell",
    "paragraph",
    "header_paragraph",
    "footer_paragraph",
    "content_control",
    "sheet_cell",
    "chart",
]
WriteMode = Literal[
    "replace_text", "replace_token", "fill_cell", "fill_table_rows", "fill_chart", "insert_chart"
]
ExpectedType = Literal[
    "text", "paragraphs", "bullet_list", "number", "currency", "percent", "date", "table", "chart"
]

MAX_ANCHORS = 400


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TableRef(_Strict):
    row: int  # 0-based; row 0 is the header row
    col: int  # 0-based
    header: str | None  # text of row 0 in this column


class ChartRef(_Strict):
    chart_type: str  # the XL_CHART_TYPE member name, e.g. "BAR_CLUSTERED"
    series: list[str]
    categories: int  # count


class Anchor(_Strict):
    anchor_id: str
    kind: AnchorKind
    path: list[str]
    text: str
    tokens: list[str]
    is_placeholder: bool
    table: TableRef | None = None
    # pptx only: shape centre as [x, y] percent of slide width/height, so the IR model can
    # pair a placeholder with the heading next to it
    pos: tuple[int, int] | None = None
    # the static heading or label next to this slot (card title, row label, column header)
    context: str | None = None
    # pptx only: "set.member" of the repeating card (identical containers) the shape sits in
    card: str | None = None
    # pptx only: a native category chart (kind "chart")
    chart: ChartRef | None = None


class AnchorMap(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    format: TemplateFormat
    anchors: list[Anchor]


class Constraints(_Strict):
    """Stored with exclude_none=True so the JSON matches the docs' TemplateIR JSON Schema."""

    max_chars: int | None = Field(None, ge=1)
    min_items: int | None = Field(None, ge=0)
    max_items: int | None = Field(None, ge=1)
    date_format: str | None = None
    unit: str | None = None
    allowed_values: list[str] | None = None
    token: str | None = None
    columns: list[str] | None = None


class IRField(_Strict):
    field_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    anchor_id: str
    section_path: list[str]
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(max_length=500)
    expected_type: ExpectedType
    required: bool
    write_mode: WriteMode
    constraints: Constraints
    # copied from the anchor by code in build_ir; hidden from the LLM schema
    context: SkipJsonSchema[str | None] = None
    card: SkipJsonSchema[str | None] = None


class IRDraft(_Strict):
    """The only shape the LLM produces; ids and version are set by code."""

    fields: list[IRField] = Field(min_length=1)


class TemplateIR(IRDraft):
    schema_version: Literal["1.0"] = "1.0"
    template_id: uuid.UUID
    version: int = Field(ge=1)
    format: TemplateFormat


TEXT_KINDS: frozenset[str] = frozenset(
    {
        "shape_text",
        "paragraph",
        "header_paragraph",
        "footer_paragraph",
        "content_control",
        "table_cell",
    }
)
# replace_token also needs non-empty anchor tokens; fill_table_rows also needs table row 1, col 0.
WRITE_MODE_KINDS: dict[str, frozenset[str]] = {
    "replace_text": TEXT_KINDS,
    "replace_token": TEXT_KINDS,
    "fill_cell": frozenset({"table_cell", "sheet_cell"}),
    "fill_table_rows": frozenset({"table_cell"}),
    "fill_chart": frozenset({"chart"}),
    "insert_chart": frozenset({"shape_text"}),
}
