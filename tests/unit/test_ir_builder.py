import json
import uuid

import pytest

from doc_intel.core.errors import IRInvalid
from doc_intel.llm.gateway import LLMError
from doc_intel.llm.prompts import load_prompt
from doc_intel.templates.ir_builder import build_ir, validate
from doc_intel.templates.schemas import (
    WRITE_MODE_KINDS,
    Anchor,
    AnchorMap,
    Constraints,
    IRDraft,
    IRField,
    TableRef,
)
from tests.fakes import FakeLLM


def _anchor(anchor_id, kind="paragraph", tokens=(), table=None, text="x"):
    return Anchor(
        anchor_id=anchor_id,
        kind=kind,
        path=["Slide 1"],
        text=text,
        tokens=list(tokens),
        is_placeholder=False,
        table=table,
    )


def _field(field_id="f1", anchor_id="a1", write_mode="replace_text", **constraints):
    return IRField(
        field_id=field_id,
        anchor_id=anchor_id,
        section_path=["Slide 1"],
        label="L",
        description="D",
        expected_type="text",
        required=True,
        write_mode=write_mode,
        constraints=Constraints(**constraints),
    )


def _cell(anchor_id, row, col):
    return _anchor(anchor_id, "table_cell", table=TableRef(row=row, col=col, header="H"))


AMAP = AnchorMap(
    format="docx",
    anchors=[
        _anchor("a1"),
        _anchor("a2"),
        _anchor("tok", tokens=["{{name}}", "{{date}}"]),
        _anchor("sheet", "sheet_cell"),
        _cell("r1c0", 1, 0),
        _cell("r2c0", 2, 0),
        _cell("r1c1", 1, 1),
    ],
)


def _errors(*fields):
    return validate(IRDraft(fields=list(fields)), AMAP)


def test_valid_draft_has_no_errors():
    ok = _errors(
        _field("a", "a1"),
        _field("b", "tok", "replace_token", token="{{date}}"),
        _field("c", "sheet", "fill_cell"),
        _field("d", "r1c0", "fill_table_rows", columns=["H"]),
    )
    assert ok == []


def test_unknown_anchor():
    (err,) = _errors(_field("a", "nope"))
    assert "nope" in err


def test_duplicate_anchor():
    errs = _errors(_field("a", "a1"), _field("b", "a1"))
    assert len(errs) == 1 and "a1" in errs[0]
    assert "'a'" in errs[0]  # names the field that already holds it, so the retry can fix it


def test_fill_table_rows_owns_its_whole_table():
    # Phase 7 writes every body row of that table; another field there would be a second write.
    amap = AnchorMap(
        format="docx",
        anchors=[
            _cell("docx:body:t0:r1c0", 1, 0),
            _cell("docx:body:t0:r1c2", 1, 2),
            _cell("docx:body:t1:r1c0", 1, 0),
        ],
    )
    fields = [
        _field("cell_first", "docx:body:t0:r1c2", "fill_cell"),
        _field("rows", "docx:body:t0:r1c0", "fill_table_rows"),
        _field("other_table", "docx:body:t1:r1c0", "fill_cell"),
    ]
    (err,) = validate(IRDraft(fields=fields), amap)
    assert "'cell_first'" in err and "'rows'" in err


def test_duplicate_field_id():
    errs = _errors(_field("a", "a1"), _field("a", "a2"))
    assert len(errs) == 1 and "'a'" in errs[0]


def test_write_mode_must_fit_kind():
    assert _errors(_field("a", "sheet", "replace_text"))
    assert _errors(_field("a", "a1", "fill_cell"))
    assert WRITE_MODE_KINDS["fill_cell"] >= {"sheet_cell"}


def test_replace_token_needs_tokens_and_matching_token():
    assert _errors(_field("a", "a1", "replace_token", token="{{x}}"))
    assert _errors(_field("a", "tok", "replace_token", token="{{nope}}"))
    assert _errors(_field("a", "tok", "replace_token"))


@pytest.mark.parametrize(("anchor_id", "ok"), [("r1c0", True), ("r2c0", False), ("r1c1", False)])
def test_fill_table_rows_needs_first_body_cell(anchor_id, ok):
    assert (_errors(_field("a", anchor_id, "fill_table_rows")) == []) is ok


def test_fill_table_rows_on_cell_without_table():
    amap = AnchorMap(format="docx", anchors=[_anchor("c", "table_cell")])
    draft = IRDraft(fields=[_field("a", "c", "fill_table_rows")])
    assert validate(draft, amap)


def _reply(*fields):
    return IRDraft(fields=list(fields)).model_dump_json()


TID = uuid.uuid4()


async def test_build_ir_happy_path():
    long_amap = AnchorMap(format="pptx", anchors=[_anchor("a1", text="y" * 500, tokens=["{{t}}"])])
    llm = FakeLLM(_reply(_field("a", "a1")))
    ir = await build_ir(llm, long_amap, TID)
    assert (ir.template_id, ir.version, ir.format) == (TID, 1, "pptx")
    assert [f.field_id for f in ir.fields] == ["a"]
    ((messages, schema),) = llm.calls
    assert schema == IRDraft.model_json_schema()
    assert messages[0] == {"role": "system", "content": load_prompt("template_ir")}
    user = messages[1]["content"]
    assert "a1" in user and "{{t}}" in user and "y" * 200 in user and "y" * 201 not in user
    assert json.loads(user.split("\n\n", 1)[1])["anchors"][0]["is_placeholder"] is False


async def test_build_ir_includes_table_header():
    amap = AnchorMap(format="docx", anchors=[_cell("c", 1, 0)])
    llm = FakeLLM(_reply(_field("a", "c", "fill_table_rows")))
    await build_ir(llm, amap, TID)
    assert json.dumps("H") in llm.calls[0][0][1]["content"]


async def test_build_ir_retries_once_with_errors():
    bad = _reply(_field("a", "nope"))
    llm = FakeLLM(bad, _reply(_field("a", "a1")))
    ir = await build_ir(llm, AMAP, TID)
    assert ir.fields[0].anchor_id == "a1"
    second = llm.calls[1][0]
    assert second[-2] == {"role": "assistant", "content": bad}
    assert "nope" in second[-1]["content"]


async def test_build_ir_raises_after_second_invalid():
    bad = _reply(_field("a", "nope"))
    llm = FakeLLM(bad, bad)
    with pytest.raises(IRInvalid):
        await build_ir(llm, AMAP, TID)
    assert len(llm.calls) == 2


async def test_build_ir_propagates_llm_error():
    with pytest.raises(LLMError):
        await build_ir(FakeLLM("no", "no"), AMAP, TID)


def test_prompt_mentions_every_write_mode():
    prompt = load_prompt("template_ir")
    assert all(mode in prompt for mode in WRITE_MODE_KINDS)
