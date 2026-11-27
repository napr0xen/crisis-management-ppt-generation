"""Phase 24: derived values written into a PPTX. Derived numbers have no quote that states them,
so the writer must render them in the source's number convention and affix."""

from io import BytesIO

import pytest
from pptx import Presentation
from pptx.util import Inches

from doc_intel.mapping.engine import map_fields
from doc_intel.templates.schemas import Constraints
from doc_intel.writers import render
from tests.fakes import FakeLLM
from tests.integration.test_derived_e2e import EU_ROWS, HEADERS, bind, field, ingest

pytestmark = pytest.mark.integration

TOKENS = {
    "total_money_in": "{{TOTAL MONEY IN}}",
    "total_money_out": "{{TOTAL MONEY OUT}}",
    "net_movement": "{{NET MOVEMENT}}",
}


def template() -> tuple[bytes, dict[str, str]]:
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    anchors = {}
    for i, (fid, token) in enumerate(TOKENS.items()):
        box = slide.shapes.add_textbox(Inches(1), Inches(1 + i), Inches(4), Inches(0.5))
        box.text_frame.text = f"Total {token} today"
        anchors[fid] = f"pptx:s1:sp{box.shape_id}"
    table = slide.shapes.add_table(4, 2, Inches(1), Inches(4), Inches(6), Inches(2))
    for col, head in enumerate(["Category", "Amount"]):
        table.table.cell(0, col).text = head
    anchors["category_breakdown"] = f"pptx:s1:sp{table.shape_id}:r1c0"
    out = BytesIO()
    prs.save(out)
    return out.getvalue(), anchors


async def test_derived_values_reach_the_deck_in_the_source_convention(vector_store):
    ctx, doc, _, retriever = await ingest(vector_store, HEADERS, EU_ROWS)
    data, anchors = template()
    fields = []
    for fid, anchor in anchors.items():
        f = field(fid, "table" if fid == "category_breakdown" else "currency")
        f = f.model_copy(update={"anchor_id": anchor})
        if fid == "category_breakdown":
            f = f.model_copy(update={"write_mode": "fill_table_rows"})
        else:
            f = f.model_copy(
                update={
                    "write_mode": "replace_token",
                    "constraints": Constraints(token=TOKENS[fid]),
                }
            )
        fields.append(f)
    llm = FakeLLM(
        bind(op="sum", column="Money In"),
        bind(op="sum", column="Money Out"),
        bind(op="diff", column="Money In", minus="Money Out"),
        bind(op="group_sum", column="Money Out", group_by="Category", top_n=3),
    )
    results = await map_fields(ctx, fields, [doc], retriever, llm)
    out, warnings = render(
        "pptx", data, fields, results, missing_marker="[MISSING: {label}]", conflict_marker="[C]"
    )
    assert warnings == []
    prs = Presentation(BytesIO(out))
    shapes = list(prs.slides[0].shapes)
    texts = [s.text_frame.text for s in shapes if s.has_text_frame and not s.has_table]
    assert [t for t in texts if t.startswith("Total")] == [
        "Total 4.295,50 $ today",  # 3.000,00 + 45,10 + 1.250,40
        "Total 1.790,50 $ today",  # 1.200,50 + 310,25 + 89,75 + 150,00 + 40,00
        "Total 2.505,00 $ today",  # 4.295,50 - 1.790,50
    ]
    table = next(s for s in shapes if s.has_table).table
    assert [[c.text for c in r.cells] for r in table.rows] == [
        ["Category", "Amount"],
        ["Housing", "1.350,50 $"],
        ["Food", "400,00 $"],
        ["Leisure", "40,00 $"],
    ]
    assert "{{" not in "".join(texts)
