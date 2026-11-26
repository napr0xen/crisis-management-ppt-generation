"""AnchorMap and TemplateIR contracts (docs/system-design.md § 4-5).

Phase 6 mapping and Phase 7 writers read these shapes; change them only with the docs.
"""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
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
# extractive: copied verbatim from a quote; synthesis: written (summary, trend, observation) or
# computed from verified quotes; derived: an aggregate computed in code over a table's rows
FieldMode = Literal["extractive", "synthesis", "derived"]

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
    # pptx only: about how many characters fit one token of this text box (box size and font)
    fit_chars: int | None = Field(None, ge=1)


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


class Derivation(_Strict):
    """An aggregate over one source table's rows (ADR-018), computed by code, never by the LLM."""

    op: Literal["sum", "diff", "count", "min", "max", "group_sum"]
    column: str
    minus: str | None = None  # diff: the column subtracted from `column`
    where: dict[str, str] = {}  # header -> required cell value
    tag: Literal["CR", "DR"] | None = None  # keep only cells tagged so
    group_by: str | None = None  # group_sum: the label column
    top_n: int | None = Field(None, ge=1)
    exclude_rows: list[str] = []  # row ids left out (printed total rows)

    @model_validator(mode="after")
    def _consistent(self) -> "Derivation":
        if (self.group_by is None) == (self.op == "group_sum"):
            raise ValueError("group_by is required for group_sum and only for it")
        if self.minus is not None and self.op != "diff":
            raise ValueError("minus is only for diff")
        if self.op == "diff" and self.minus is None and self.tag is not None:
            raise ValueError("diff without minus uses both tags: no tag filter")
        return self


class IRField(_Strict):
    field_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    anchor_id: str
    section_path: list[str]
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(max_length=500)
    expected_type: ExpectedType
    mode: FieldMode = "extractive"  # an IR stored before Phase 19 reads as extractive
    required: bool
    write_mode: WriteMode
    constraints: Constraints
    # copied from the anchor by code in build_ir; hidden from the LLM schema
    context: SkipJsonSchema[str | None] = None
    card: SkipJsonSchema[str | None] = None
    # a soft length hint for the extraction prompt, never a constraint or a warning
    fit_chars: SkipJsonSchema[int | None] = None
    # mode == "derived": the aggregation, from the template or bound at mapping time; the IR
    # builder only classifies the mode
    derivation: SkipJsonSchema[Derivation | None] = None


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
