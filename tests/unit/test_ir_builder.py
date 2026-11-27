import json
import re
import uuid

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from doc_intel.core.errors import IRInvalid
from doc_intel.llm.gateway import RETRY_ECHO_CHARS, LLMError
from doc_intel.llm.prompts import load_prompt
from doc_intel.templates.ir_builder import (
    MAX_BATCH_CHARS,
    BatchDraft,
    BatchReply,
    _batches,
    _compact,
    _loggable,
    _repair,
    build_ir,
    validate,
)
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


def _anchor(anchor_id, kind="paragraph", tokens=(), table=None, text="x", context=None):
    return Anchor(
        anchor_id=anchor_id,
        kind=kind,
        path=["Slide 1"],
        text=text,
        tokens=list(tokens),
        is_placeholder=False,
        table=table,
        context=context,
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


def test_anchor_with_tokens_must_use_replace_token():
    errors = _errors(_field("x", "tok", "replace_text"))
    assert errors == [
        "field 'x': anchor 'tok' has tokens; use replace_token so the text around the token "
        "is kept."
    ]
    assert not _errors(_field("x", "tok", "replace_token", token="{{name}}"))


def test_fill_cell_on_a_cell_with_tokens_must_use_replace_token():
    table = TableRef(row=1, col=1, header="H")
    owner = _anchor("o", "table_cell", tokens=["[name]"], table=table, text="Owner: [name]")
    bare = _anchor("b", "table_cell", tokens=["[name]"], table=table, text="[name]")
    sheet = _anchor("s", "sheet_cell", tokens=["[name]"], text="Owner: [name]")
    amap = AnchorMap(format="docx", anchors=[owner, bare, sheet])
    (err,) = validate(IRDraft(fields=[_field("x", "o", "fill_cell")]), amap)
    assert "has tokens; use replace_token" in err
    assert validate(IRDraft(fields=[_field("y", "b", "fill_cell")]), amap) == []
    assert validate(IRDraft(fields=[_field("z", "s", "fill_cell")]), amap) == []


def test_replace_text_is_fine_when_the_text_is_exactly_one_token():
    amap = AnchorMap(format="docx", anchors=[_anchor("t", tokens=["{{a}}"], text=" {{a}} ")])
    draft = IRDraft(fields=[_field("x", "t", "replace_text")])
    assert validate(draft, amap) == []


def test_fill_table_rows_ignores_the_token_rule():
    cell = _anchor("c", "table_cell", tokens=["{{a}}"], table=TableRef(row=1, col=0, header="H"))
    amap = AnchorMap(format="docx", anchors=[cell])
    assert validate(IRDraft(fields=[_field("x", "c", "fill_table_rows")]), amap) == []


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
    llm = FakeLLM(_reply(_field("a", "a1", "replace_token", token="{{t}}")))
    ir = await build_ir(llm, long_amap, TID)
    assert (ir.template_id, ir.version, ir.format) == (TID, 1, "pptx")
    assert [f.field_id for f in ir.fields] == ["a"]
    ((messages, schema),) = llm.calls
    assert schema == BatchReply.model_json_schema()
    assert messages[0] == {"role": "system", "content": load_prompt("template_ir")}
    user = messages[1]["content"]
    assert "a1" in user and "{{t}}" in user and "y" * 200 in user and "y" * 201 not in user
    first = json.loads(user.split("\n\n", 1)[1])["anchors"][0]
    assert first["is_placeholder"] is False and "pos" not in first
    pos_amap = AnchorMap(
        format="pptx",
        anchors=[_anchor("a1", tokens=["{{t}}"]).model_copy(update={"pos": (50, 20)})],
    )
    llm2 = FakeLLM(_reply(_field("a", "a1", "replace_token", token="{{t}}")))
    await build_ir(llm2, pos_amap, TID)
    assert json.loads(llm2.calls[0][0][1]["content"].split("\n\n", 1)[1])["anchors"][0]["pos"] == [
        50,
        20,
    ]


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
    first, second = llm.calls[0][0], llm.calls[1][0]
    assert second[:2] == first and len(second) == 4
    assert [m["role"] for m in second[2:]] == ["assistant", "user"]
    assert second[2]["content"] == bad
    assert (
        second[-1]["content"].startswith("The fields have these errors")
        and "nope" in second[-1]["content"]
    )


async def test_build_ir_retry_echo_is_truncated():
    long_bad = _reply(_field("a", "nope"), *[_field(f"x{i}", "nope") for i in range(40)])
    assert len(long_bad) > RETRY_ECHO_CHARS
    llm = FakeLLM(long_bad, _reply(_field("a", "a1")))
    await build_ir(llm, AMAP, TID)
    assert llm.calls[1][0][2] == {"role": "assistant", "content": long_bad[:RETRY_ECHO_CHARS]}


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


def test_ir_prompt_has_date_and_skip_rules():
    prompt = load_prompt("template_ir")
    assert "`pos`" in prompt and "[Month YYYY]" in prompt and "[Company Logo]" in prompt


def test_field_extract_prompt_has_ordinal_and_placeholder_rules():
    prompt = load_prompt("field_extract")
    assert "one specific item" in prompt and "[brackets]" in prompt and "whole sentence" in prompt


def _pa(n, i):
    return _anchor(f"pptx:s{n}:sp{i}")


class EchoLLM:
    """One field per anchor id in the prompt; field ids are `name` or f0, f1... per call."""

    def __init__(self, name=None, first_cells=()):
        self.calls = []
        self.name = name
        self.first_cells = first_cells

    async def complete(self, messages, json_schema=None, think=False, think_budget=None):
        self.calls.append((list(messages), json_schema))
        ids = [
            a["anchor_id"]
            for a in json.loads(messages[1]["content"].split("\n\n", 1)[1])["anchors"]
        ]
        fields = []
        for i, aid in enumerate(ids):
            if self.first_cells and aid not in self.first_cells:
                continue  # the model chose one fill_table_rows field for the whole table
            mode = "fill_table_rows" if aid in self.first_cells else "replace_text"
            mode = "fill_cell" if aid.startswith("xlsx") else mode
            fields.append(_field(self.name or f"f{i}", aid, mode))
        return IRDraft(fields=fields).model_dump_json()


def _prompt_ids(call):
    return [
        a["anchor_id"] for a in json.loads(call[0][1]["content"].split("\n\n", 1)[1])["anchors"]
    ]


async def test_pptx_one_call_per_slide_and_on_batch_progress():
    amap = AnchorMap(format="pptx", anchors=[_pa(1, 1), _pa(1, 2), _pa(2, 1), _pa(3, 1)])
    llm, seen = EchoLLM(), []

    async def on_batch(done, total, part):
        seen.append((done, total, part))

    ir = await build_ir(llm, amap, TID, on_batch=on_batch)
    assert [_prompt_ids(c) for c in llm.calls] == [
        ["pptx:s1:sp1", "pptx:s1:sp2"],
        ["pptx:s2:sp1"],
        ["pptx:s3:sp1"],
    ]
    assert seen == [(0, 3, "slide 1"), (1, 3, "slide 2"), (2, 3, "slide 3")]
    assert len(ir.fields) == 4 and len({f.field_id for f in ir.fields}) == 4


async def test_xlsx_grouped_per_sheet_with_neutral_labels():
    anchors = [
        _anchor("xlsx:Secret Name!A1", "sheet_cell"),
        _anchor("xlsx:Secret Name!B2", "sheet_cell"),
        _anchor("xlsx:Other!A1", "sheet_cell"),
    ]
    amap = AnchorMap(format="xlsx", anchors=anchors)
    seen = []

    async def on_batch(done, total, part):
        seen.append((done, total, part))

    await build_ir(EchoLLM(), amap, TID, on_batch=on_batch)
    assert seen == [(0, 2, "sheet 1"), (1, 2, "sheet 2")]


async def test_docx_over_cap_is_chunked_in_order():
    amap = AnchorMap(format="docx", anchors=[_anchor(f"docx:p{i}") for i in range(90)])
    llm, seen = EchoLLM(), []

    async def on_batch(done, total, part):
        seen.append((done, total, part))

    ir = await build_ir(llm, amap, TID, on_batch=on_batch)
    assert [len(_prompt_ids(c)) for c in llm.calls] == [40, 40, 10]
    assert seen[0] == (0, 3, None)
    assert [f.anchor_id for f in ir.fields] == [a.anchor_id for a in amap.anchors]


async def test_oversized_single_slide_is_chunked():
    amap = AnchorMap(format="pptx", anchors=[_pa(1, i) for i in range(90)])
    llm = EchoLLM()
    await build_ir(llm, amap, TID)
    assert [len(_prompt_ids(c)) for c in llm.calls] == [40, 40, 10]


def _table(n, rows, cols=1):
    return [_cell(f"pptx:s{n}:sp9:r{r}c{c}", r, c) for r in range(1, rows + 1) for c in range(cols)]


async def test_table_is_not_split_but_two_tables_split_between_each_other():
    t1 = _table(1, 25)
    t2 = [_cell(f"pptx:s1:sp10:r{r}c0", r, 0) for r in range(1, 26)]
    llm = EchoLLM()
    await build_ir(llm, AnchorMap(format="pptx", anchors=[*t1, *t2]), TID)
    assert [_prompt_ids(c) for c in llm.calls] == [
        [a.anchor_id for a in t1],
        [a.anchor_id for a in t2],
    ]


async def test_oversized_table_chunks_and_later_chunks_drop_after_fill_table_rows():
    cells = _table(1, 50)  # 50 cells, r1c0 first
    amap = AnchorMap(format="pptx", anchors=cells)
    llm = EchoLLM(first_cells={cells[0].anchor_id})
    ir = await build_ir(llm, amap, TID)
    assert len(llm.calls) == 1  # the second chunk was dropped: the rows field owns the table
    assert next(f.write_mode for f in ir.fields) == "fill_table_rows"

    llm = EchoLLM()  # no rows field: both chunks run
    await build_ir(llm, amap, TID)
    assert [len(_prompt_ids(c)) for c in llm.calls] == [40, 10]


async def test_cross_batch_field_id_collisions_are_renamed():
    amap = AnchorMap(format="pptx", anchors=[_pa(1, 1), _pa(2, 1), _pa(3, 1)])
    ir = await build_ir(EchoLLM(name="x"), amap, TID)
    assert [f.field_id for f in ir.fields] == ["x", "x_2", "x_3"]

    long = "a" * 64
    ir = await build_ir(EchoLLM(name=long), amap, TID)
    ids = [f.field_id for f in ir.fields]
    assert ids == [long, "a" * 60 + "_2", "a" * 60 + "_3"]
    assert all(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", i) for i in ids)


async def test_each_batch_gets_its_own_validation_retry():
    amap = AnchorMap(format="pptx", anchors=[_pa(1, 1), _pa(2, 1)])
    llm = FakeLLM(
        _reply(_field("a", "pptx:s1:sp1")),
        _reply(_field("b", "nope")),
        _reply(_field("b", "pptx:s2:sp1")),
    )
    ir = await build_ir(llm, amap, TID)
    assert len(llm.calls) == 3 and [f.field_id for f in ir.fields] == ["a", "b"]


async def test_anchor_from_another_batch_is_rejected_as_unknown():
    amap = AnchorMap(format="pptx", anchors=[_pa(1, 1), _pa(2, 1)])
    llm = FakeLLM(
        _reply(_field("a", "pptx:s2:sp1")),  # belongs to batch 2
        _reply(_field("a", "pptx:s1:sp1")),
        _reply(_field("b", "pptx:s2:sp1")),
    )
    ir = await build_ir(llm, amap, TID)
    assert "pptx:s2:sp1" in llm.calls[1][0][-1]["content"]
    assert [f.anchor_id for f in ir.fields] == ["pptx:s1:sp1", "pptx:s2:sp1"]


async def test_batch_without_fillable_anchors_may_return_no_fields():
    """A slide of finished text must not be forced to yield a (fabricated) field."""
    amap = AnchorMap(
        format="pptx",
        anchors=[_anchor("pptx:s1:sp1", "shape_text"), _anchor("pptx:s2:sp1", "shape_text")],
    )
    llm = FakeLLM('{"fields": []}', _reply(_field("b", "pptx:s2:sp1")))
    ir = await build_ir(llm, amap, TID)
    assert [f.anchor_id for f in ir.fields] == ["pptx:s2:sp1"]
    assert "minItems" not in json.dumps(llm.calls[0][1]["properties"]["fields"])


async def test_template_with_no_fields_at_all_is_ir_invalid():
    llm = FakeLLM('{"fields": []}')
    with pytest.raises(IRInvalid):
        await build_ir(llm, AnchorMap(format="docx", anchors=[_anchor("a1")]), TID)


MULTI = _anchor(
    "pptx:s1:sp1",
    "shape_text",
    tokens=["[Presenter Name]", "[Role]", "[DD Month YYYY]"],
    text="[Presenter Name], [Role] \u00b7 [DD Month YYYY]",
)
MULTI_MAP = AnchorMap(format="pptx", anchors=[MULTI, _anchor("pptx:s1:sp2", "shape_text")])


def _tok(fid, token, anchor="pptx:s1:sp1"):
    return _field(fid, anchor, "replace_token", token=token)


def _three():
    return [
        _tok("who", "[Presenter Name]"),
        _tok("role", "[Role]"),
        _tok("when", "[DD Month YYYY]"),
    ]


def test_several_replace_token_fields_on_one_anchor_are_valid():
    assert validate(IRDraft(fields=_three()), MULTI_MAP) == []


def test_duplicate_anchor_token_pair_names_both_fields():
    fields = [*_three(), _tok("again", "[Role]")]
    (err,) = validate(IRDraft(fields=fields), MULTI_MAP)
    assert "'again'" in err and "'role'" in err and "[Role]" not in err


def test_replace_text_with_replace_token_on_one_anchor_fails():
    fields = [_tok("who", "[Presenter Name]"), _field("txt", "pptx:s1:sp1", "replace_text")]
    errs = validate(IRDraft(fields=fields), MULTI_MAP)
    assert any("'txt'" in e and "'who'" in e and "drop one of them" in e for e in errs)


def test_fill_cell_and_replace_text_duplicates_still_fail():
    assert _errors(_field("a", "a1"), _field("b", "a1", "replace_text"))
    assert _errors(_field("a", "sheet", "fill_cell"), _field("b", "sheet", "fill_cell"))


async def test_build_ir_keeps_all_token_fields_of_a_multi_token_shape():
    amap = AnchorMap(format="pptx", anchors=[MULTI, _pa(2, 1)])
    llm = FakeLLM(_reply(*_three()), _reply(_field("t2", "pptx:s2:sp1")))
    ir = await build_ir(llm, amap, TID)
    assert [f.field_id for f in ir.fields] == ["who", "role", "when", "t2"]


async def test_ir_invalid_is_logged_with_ids_only():
    secret = _anchor("a1", tokens=["[SECRET]"], text="secret text [SECRET]")
    amap = AnchorMap(format="docx", anchors=[secret])
    bad = _reply(_field("a", "a1", "replace_token", token="[WRONG]"))
    with capture_logs() as logs, pytest.raises(IRInvalid):
        await build_ir(FakeLLM(bad, bad), amap, TID)
    (event,) = [e for e in logs if e["event"] == "ir_invalid"]
    assert event["errors"] and event["anchor_count"] == 1
    assert "SECRET" not in str(event) and "WRONG" not in str(event) and "secret" not in str(event)


async def test_no_fields_at_all_is_logged_too():
    with capture_logs() as logs, pytest.raises(IRInvalid):
        await build_ir(
            FakeLLM('{"fields": []}'), AnchorMap(format="docx", anchors=[_anchor("a1")]), TID
        )
    assert [e["anchor_count"] for e in logs if e["event"] == "ir_invalid"] == [1]


async def test_ir_invalid_log_redacts_non_structural_ids():
    """Sheet names, sdt tags and invented anchor ids are template or LLM text: never logged."""
    amap = AnchorMap(
        format="xlsx", anchors=[_anchor("xlsx:Payroll Secret!B2", "sheet_cell", text="")]
    )
    bad = _reply(
        _field("a", "xlsx:Payroll Secret!B2", "replace_text"),
        _field("b", "docx:sdt:client_name"),
        _field("c", "Invented Text"),
        _field("d", "pptx:s9:sp10_2:r1c0"),
    )
    with capture_logs() as logs, pytest.raises(IRInvalid):
        await build_ir(FakeLLM(bad, bad), amap, TID)
    (event,) = [e for e in logs if e["event"] == "ir_invalid"]
    logged = str(event["errors"])
    assert "Payroll" not in logged and "client_name" not in logged and "Invented" not in logged
    assert "'a'" in logged and "'replace_text'" in logged and "'?'" in logged
    assert "'pptx:s9:sp10_2:r1c0'" in logged  # keyed pptx ids are structural


def test_batches_are_capped_by_prompt_size_not_only_anchor_count():
    """40 long-text docx paragraphs fit the anchor cap but not the token budget."""
    anchors = [_anchor(f"docx:body:p{i}", text="w" * 400) for i in range(40)]
    batches = _batches(AnchorMap(format="docx", anchors=anchors))
    assert len(batches) > 1
    assert [a for _, b in batches for a in b] == anchors  # order kept, nothing dropped
    for _, batch in batches:
        assert len(_compact(AnchorMap(format="docx", anchors=batch))) <= MAX_BATCH_CHARS


def _box(i, tokens=(), placeholder=False, text="x"):
    a = _anchor(f"pptx:s2:sp{i}", "shape_text", tokens=tokens, text=text)
    return a.model_copy(update={"is_placeholder": placeholder})


def _recon_map():
    return AnchorMap(
        format="pptx",
        anchors=[
            _box(26, text="Opening"),  # static labels
            _box(32, text="Closing"),
            _box(30, tokens=["[Amount]"], text="[Amount]"),
            _box(31, tokens=["[Amount]"], text="[Amount]"),
            _box(40, placeholder=True, text=""),
        ],
    )


def _amount(fid, anchor):
    return _field(fid, anchor, "replace_token", token="[Amount]")


def _slips():
    return [
        _field("rows", "pptx:s2:sp26", "fill_table_rows"),  # fill_table_rows on a shape_text
        _amount("label_tok", "pptx:s2:sp32"),  # replace_token on a static label
        _amount("opening", "pptx:s2:sp30"),
        _amount("opening_note", "pptx:s2:sp30"),  # exact duplicate
        _amount("closing", "pptx:s2:sp31"),
        _field("notes", "pptx:s2:sp40"),
    ]


async def test_slide_two_slips_are_repaired_without_a_retry():
    llm = FakeLLM(_reply(*_slips()))
    with capture_logs() as logs:
        ir = await build_ir(llm, _recon_map(), TID)
    assert len(llm.calls) == 1
    assert [f.field_id for f in ir.fields] == ["opening", "closing", "notes"]
    (event,) = [e for e in logs if e["event"] == "ir_repaired"]
    assert event["dropped"] == 3 and event["anchor_count"] == 5
    assert set(event) - {"dropped", "anchor_count", "event", "log_level"} == set()


async def test_repair_applies_to_the_retry_reply_too():
    amap = _recon_map()
    llm = FakeLLM(
        _reply(_field("a", "invented")),
        _reply(_amount("opening", "pptx:s2:sp30"), _amount("label_tok", "pptx:s2:sp26")),
    )
    ir = await build_ir(llm, amap, TID)
    # sp31 carries the same token on the slide, so it is backfilled as a copy of sp30's field
    assert len(llm.calls) == 2 and [f.field_id for f in ir.fields] == ["opening", "amount"]


def _placeholder_zero_token():
    return AnchorMap(format="docx", anchors=[_box(1, placeholder=True, text="")])


@pytest.mark.parametrize(
    ("amap", "fields"),
    [
        (AMAP, [_field("a", "invented")]),
        (AMAP, [_field("a", "tok", "replace_token", token="{{nope}}")]),
        (
            _placeholder_zero_token(),
            [_field("a", "pptx:s2:sp1", "replace_token", token="[X]")],
        ),
        (
            MULTI_MAP,
            [_tok("who", "[Presenter Name]"), _field("txt", "pptx:s1:sp1", "replace_text")],
        ),
    ],
)
async def test_real_errors_stay_strict(amap, fields):
    bad = _reply(*fields)
    llm = FakeLLM(bad, bad)
    with pytest.raises(IRInvalid):
        await build_ir(llm, amap, TID)
    assert len(llm.calls) == 2


def test_repair_keeps_what_validate_must_judge():
    amap = AnchorMap(
        format="pptx",
        anchors=[_box(1, tokens=["[A]", "[B]"]), _cell("pptx:s1:sp9:r1c0", 1, 0)],
    )
    fields = [
        _tok("a", "[A]", "pptx:s2:sp1"),
        _tok("b", "[B]", "pptx:s2:sp1"),
        _field("ghost", "nowhere", "fill_table_rows"),
        _field("rows", "pptx:s1:sp9:r1c0", "fill_table_rows"),
    ]
    draft = BatchDraft(fields=fields)
    assert _repair(draft, amap).fields == fields


@pytest.mark.parametrize("heading", ["Emergency department", "Wheat yield"])
async def test_context_reaches_the_prompt_and_every_field_even_if_the_llm_omits_it(heading):
    amap = AnchorMap(
        format="pptx",
        anchors=[_anchor("a1", tokens=["{{t}}"]).model_copy(update={"context": heading})],
    )
    llm = FakeLLM(_reply(_field("a", "a1", "replace_token", token="{{t}}")))
    ir = await build_ir(llm, amap, TID)
    item = json.loads(llm.calls[0][0][1]["content"].split("\n\n", 1)[1])["anchors"][0]
    assert item["context"] == heading
    assert [f.context for f in ir.fields] == [heading]
    # an LLM-invented context is overwritten by the anchor's
    forged = _field("a", "a1", "replace_token", token="{{t}}").model_copy(
        update={"context": "forged"}
    )
    ir = await build_ir(FakeLLM(_reply(forged)), amap, TID)
    assert ir.fields[0].context == heading


async def test_no_context_means_no_key_and_none():
    llm = FakeLLM(_reply(_field("a", "a1")))
    ir = await build_ir(llm, AMAP, TID)
    assert '"context"' not in llm.calls[0][0][1]["content"]
    assert ir.fields[0].context is None


def test_context_is_hidden_from_the_llm_schema_but_optional_in_stored_json():
    assert "context" not in IRField.model_json_schema()["properties"]
    assert "context" not in BatchDraft.model_json_schema()["$defs"]["IRField"]["properties"]
    stored = _field("a", "a1").model_dump(mode="json")
    stored.pop("context", None)
    assert IRField.model_validate(stored).context is None
    assert "context" in load_prompt("template_ir")


@pytest.mark.parametrize("card", ["1.1", "2.3"])
async def test_card_is_copied_by_code_and_never_sent_to_the_llm(card):
    amap = AnchorMap(
        format="pptx",
        anchors=[_anchor("a1", tokens=["{{t}}"]).model_copy(update={"card": card})],
    )
    llm = FakeLLM(_reply(_field("a", "a1", "replace_token", token="{{t}}")))
    ir = await build_ir(llm, amap, TID)
    assert '"card"' not in llm.calls[0][0][1]["content"]
    assert [f.card for f in ir.fields] == [card]
    forged = _field("a", "a1", "replace_token", token="{{t}}").model_copy(update={"card": "9.9"})
    ir = await build_ir(FakeLLM(_reply(forged)), amap, TID)
    assert ir.fields[0].card == card


async def test_an_anchor_without_a_card_overwrites_a_forged_one_with_none():
    forged = _field("a", "a1").model_copy(update={"card": "9.9"})
    ir = await build_ir(FakeLLM(_reply(forged)), AMAP, TID)
    assert ir.fields[0].card is None


def test_card_is_hidden_from_the_llm_schema_but_optional_in_stored_json():
    assert "card" not in BatchDraft.model_json_schema()["$defs"]["IRField"]["properties"]
    stored = _field("a", "a1").model_dump(mode="json")
    stored.pop("card", None)
    assert IRField.model_validate(stored).card is None
    assert Anchor.model_validate(_anchor("a1").model_dump()).card is None


# ---- symbolic count tokens and skipped empty tables ------------------------------------------


@pytest.mark.parametrize(
    "token",
    ["[# 1]", "[# 6]", "[# items]", "[#items]", "{{# visitors}}"],
)
async def test_a_hash_count_token_is_typed_number_whatever_the_llm_said(token):
    amap = AnchorMap(format="pptx", anchors=[_anchor("a1", tokens=[token])])
    field = _field("a", "a1", "replace_token", token=token)
    llm = FakeLLM(_reply(field))  # the LLM typed it text, as the live pipeline slide did
    ir = await build_ir(llm, amap, TID)
    assert [f.expected_type for f in ir.fields] == ["number"]


@pytest.mark.parametrize("token", ["[Item #3]", "[Category name 1]", "{{count}}"])
async def test_other_tokens_keep_the_llm_type(token):
    amap = AnchorMap(format="pptx", anchors=[_anchor("a1", tokens=[token])])
    ir = await build_ir(FakeLLM(_reply(_field("a", "a1", "replace_token", token=token))), amap, TID)
    assert [f.expected_type for f in ir.fields] == ["text"]


def test_ir_prompt_never_skips_an_empty_native_table():
    assert "header row and empty body cells is always a slot" in load_prompt("template_ir")


@pytest.mark.parametrize(
    "token",
    ["[Month YYYY 1]", "[Mon YYYY]", "[MM/YYYY]", "[YYYY-MM]", "{{month yyyy}}", "[Mes AAAA]"],
)
async def test_a_month_year_token_is_text_not_a_day_less_date(token):
    amap = AnchorMap(format="pptx", anchors=[_anchor("a1", tokens=[token])])
    field = _field("a", "a1", "replace_token", token=token).model_copy(
        update={"expected_type": "date"}
    )
    ir = await build_ir(FakeLLM(_reply(field)), amap, TID)
    assert [f.expected_type for f in ir.fields] == ["text"]


@pytest.mark.parametrize("token", ["[DD Month YYYY]", "[Date]", "{{due_date}}", "[Día Mes AAAA]"])
async def test_a_date_token_with_a_day_stays_a_date(token):
    amap = AnchorMap(format="pptx", anchors=[_anchor("a1", tokens=[token])])
    field = _field("a", "a1", "replace_token", token=token).model_copy(
        update={"expected_type": "date"}
    )
    ir = await build_ir(FakeLLM(_reply(field)), amap, TID)
    assert [f.expected_type for f in ir.fields] == ["date"]


def _llm_field_schema() -> dict:
    schema = BatchReply.model_json_schema()
    ref = schema["properties"]["fields"]["items"]["$ref"].rsplit("/", 1)[-1]
    return schema["$defs"][ref]


def test_the_llm_must_classify_every_field_mode():
    item = _llm_field_schema()
    assert "mode" in item["required"]
    assert item["properties"]["mode"]["enum"] == [
        "extractive",
        "synthesis",
        "derived",
    ]
    assert "default" not in item["properties"]["mode"]
    assert "synthesis" in item["properties"]["mode"]["description"]


def test_a_reply_without_mode_is_invalid_but_a_stored_ir_without_it_reads_extractive():
    raw = _field("a", "a1").model_dump(mode="json", exclude={"mode"})
    with pytest.raises(ValidationError):
        BatchReply.model_validate({"fields": [raw]})
    assert IRField.model_validate(raw).mode == "extractive"


async def test_a_synthesis_mode_the_llm_chose_is_kept():
    amap = AnchorMap(format="pptx", anchors=[_anchor("a1", tokens=["[Region]"])])
    field = _field("a", "a1", "replace_token", token="[Region]").model_copy(
        update={"mode": "synthesis"}
    )
    ir = await build_ir(FakeLLM(_reply(field)), amap, TID)
    assert [f.mode for f in ir.fields] == ["synthesis"]


def _grid(table, headers, body_rows, body_text=""):
    def cell(r, c, h, text):
        return _anchor(f"{table}:r{r}c{c}", "table_cell", text=text).model_copy(
            update={"table": TableRef(row=r, col=c, header=h)}
        )

    head = [cell(0, c, h, h) for c, h in enumerate(headers)]
    body = [
        cell(r, c, h, body_text) for r in range(1, body_rows + 1) for c, h in enumerate(headers)
    ]
    return head + body


EMPTY_TABLES = {
    "pptx": ("pptx", "pptx:s9:sp10", "pptx:s9:p1", ["Action", "Owner", "Due"], 4),
    "docx": ("docx", "docx:body:t0", "docx:body:p1", ["Destination", "Date", "Students"], 2),
}


@pytest.mark.parametrize("name", EMPTY_TABLES)
async def test_an_empty_table_the_llm_skipped_gets_a_fill_table_rows_field(name):
    fmt, table, para, headers, n = EMPTY_TABLES[name]
    other = _anchor(para, tokens=["[Summary]"])
    amap = AnchorMap(format=fmt, anchors=[other, *_grid(table, headers, n)])
    llm = FakeLLM(_reply(_field("summary", para, "replace_token", token="[Summary]")))
    ir = await build_ir(llm, amap, TID)
    (f,) = [f for f in ir.fields if f.write_mode == "fill_table_rows"]
    assert (f.anchor_id, f.expected_type, f.required) == (f"{table}:r1c0", "table", False)
    assert f.constraints.columns == headers and f.constraints.max_items == n
    assert all(h in f.label for h in headers)
    # a template of nothing but an empty table is fillable too
    amap = AnchorMap(format=fmt, anchors=_grid(table, headers, n))
    ir = await build_ir(FakeLLM(IRDraft.model_construct(fields=[]).model_dump_json()), amap, TID)
    assert [f.write_mode for f in ir.fields] == ["fill_table_rows"]


@pytest.mark.parametrize("name", EMPTY_TABLES)
async def test_a_used_static_or_headerless_table_gets_no_extra_field(name):
    fmt, table, para, headers, n = EMPTY_TABLES[name]
    other = _anchor(para, tokens=["[Summary]"])
    summary = _field("summary", para, "replace_token", token="[Summary]")
    amap = AnchorMap(format=fmt, anchors=[other, *_grid(table, headers, n)])
    cell = _field("c", f"{table}:r2c1", "fill_cell")
    ir = await build_ir(FakeLLM(_reply(summary, cell)), amap, TID)
    assert [f.write_mode for f in ir.fields] == ["replace_token", "fill_cell"]
    for anchors in (
        _grid(table, headers, n, body_text="Done"),
        _grid(table, [*headers[:-1], ""], n),
    ):
        amap = AnchorMap(format=fmt, anchors=[other, *anchors])
        ir = await build_ir(FakeLLM(_reply(summary)), amap, TID)
        assert [f.field_id for f in ir.fields] == ["summary"]


def _token_amap(*tokens):
    return AnchorMap(format="pptx", anchors=[_anchor("pptx:s6:sp3", tokens=tokens)])


def _repaired_token(amap, token):
    f = _field("f", "pptx:s6:sp3", "replace_token", token=token)
    out = _repair(BatchDraft(fields=[f]), amap)
    return out, validate(out, amap)


@pytest.mark.parametrize(
    ("real", "given"),
    [
        ("[Strategic supplier scorecard - period]", "Strategic supplier scorecard - period"),
        ("[Account Holder]", "  account   HOLDER "),
        ("{{name}}", "name"),
        ("<name>", "name"),
        ("[Name]", "{{ name }}"),
    ],
)
def test_repair_snaps_a_bracketless_token_to_the_real_one(real, given):
    amap = _token_amap(real, "[Other]")
    out, errors = _repaired_token(amap, given)
    assert out.fields[0].constraints.token == real
    assert errors == []


@pytest.mark.parametrize(
    ("tokens", "given"),
    [(["[Name]", "{{name}}"], "name"), (["[Name]", "[Date]"], "nothing")],
)
def test_repair_leaves_an_ambiguous_or_unknown_token(tokens, given):
    amap = _token_amap(*tokens)
    out, errors = _repaired_token(amap, given)
    assert out.fields[0].constraints.token == given
    assert errors


def test_repair_keeps_a_token_that_is_already_real():
    amap = _token_amap("[Name]")
    draft = BatchDraft(fields=[_field("f", "pptx:s6:sp3", "replace_token", token="[Name]")])
    assert _repair(draft, amap) is draft


def test_token_error_has_no_apostrophe_so_ids_survive_log_masking():
    amap = _token_amap("[Name]")
    f = _field("holder_name", "pptx:s6:sp3", "replace_token", token="nope")
    (err,) = validate(IRDraft(fields=[f]), amap)
    (logged,) = _loggable([err])
    assert "'pptx:s6:sp3'" in logged and "'holder_name'" in logged and "'?'" not in logged


def _card(anchor_id, token, card="1.1"):
    return _anchor(anchor_id, "shape_text", tokens=[token]).model_copy(update={"card": card})


def _typed(anchor_id, token, expected_type, fid):
    return _field(fid, anchor_id, "replace_token", token=token).model_copy(
        update={"expected_type": expected_type}
    )


async def _types(anchors, *fields, replies=None):
    amap = AnchorMap(format="pptx", anchors=anchors)
    ir = await build_ir(FakeLLM(*(replies or [_reply(*fields)])), amap, TID)
    return {f.field_id: f.expected_type for f in ir.fields}


async def test_a_caption_token_beside_a_numbered_value_is_retyped_to_text():
    anchors = [
        _card("pptx:s2:sp1", "[Value 1]"),
        _card("pptx:s2:sp2", "[Total spend]"),
        _card("pptx:s2:sp3", "[# items]"),
        _card("pptx:s2:sp4", "[FY2025]"),
    ]
    types = await _types(
        anchors,
        _typed("pptx:s2:sp1", "[Value 1]", "number", "v"),
        _typed("pptx:s2:sp2", "[Total spend]", "currency", "cap"),
        _typed("pptx:s2:sp3", "[# items]", "currency", "n"),
        _typed("pptx:s2:sp4", "[FY2025]", "percent", "year"),
    )
    # a year is no ordinal, so "[FY2025]" is a caption too; a count token stays a number
    assert types == {"v": "number", "cap": "text", "n": "number", "year": "text"}


async def test_a_card_without_a_numbered_token_keeps_its_types():
    anchors = [_card("pptx:s2:sp1", "[Revenue]"), _card("pptx:s2:sp2", "[Growth]")]
    types = await _types(
        anchors,
        _typed("pptx:s2:sp1", "[Revenue]", "currency", "rev"),
        _typed("pptx:s2:sp2", "[Growth]", "percent", "growth"),
    )
    assert types == {"rev": "currency", "growth": "percent"}


async def test_a_numbered_token_in_another_card_or_slide_does_not_retype():
    anchors = [
        _card("pptx:s2:sp1", "[Revenue]", "1.1"),
        _card("pptx:s2:sp2", "[Value 1]", "1.2"),
        _card("pptx:s3:sp1", "[Value 1]", "1.1"),
    ]
    rev = _typed("pptx:s2:sp1", "[Revenue]", "currency", "rev")
    a = _typed("pptx:s2:sp2", "[Value 1]", "number", "a")
    b = _typed("pptx:s3:sp1", "[Value 1]", "number", "b")
    types = await _types(anchors, replies=[_reply(rev, a), _reply(b)])
    assert types["rev"] == "currency"


def test_repair_drops_a_tokenless_header_cell_but_keeps_a_token_header():
    plain = _anchor("t:r0c0", "table_cell", table=TableRef(row=0, col=0, header="H"))
    token = _anchor(
        "t:r0c1", "table_cell", tokens=["[Period]"], table=TableRef(row=0, col=1, header="H")
    )
    amap = AnchorMap(format="pptx", anchors=[plain, token])
    draft = BatchDraft(
        fields=[
            _field("h", "t:r0c0", "fill_cell"),
            _field("p", "t:r0c1", "replace_token", token="[Period]"),
        ]
    )
    assert [f.field_id for f in _repair(draft, amap).fields] == ["p"]


def test_repair_keeps_an_empty_first_row_cell_of_a_headerless_table():
    empty = _anchor("t:r0c1", "table_cell", table=TableRef(row=0, col=1, header=None), text="")
    draft = BatchDraft(fields=[_field("v", "t:r0c1", "fill_cell")])
    amap = AnchorMap(format="pptx", anchors=[empty])
    assert [f.field_id for f in _repair(draft, amap).fields] == ["v"]


async def test_fit_chars_is_a_hint_on_the_field_and_never_a_constraint_or_prompt_text():
    a = _anchor("pptx:s1:sp1", "shape_text", tokens=["[T]"]).model_copy(update={"fit_chars": 41})
    llm = FakeLLM(_reply(_field("t", "pptx:s1:sp1", "replace_token", token="[T]")))
    ir = await build_ir(llm, AnchorMap(format="pptx", anchors=[a]), TID)
    (f,) = ir.fields
    assert f.fit_chars == 41 and f.constraints.max_chars is None
    assert "fit_chars" not in llm.calls[0][0][1]["content"]
    assert "fit_chars" not in IRField.model_json_schema()["properties"]


def test_prompt_names_the_tile_caption_rule():
    assert "caption" in load_prompt("template_ir")


@pytest.mark.parametrize(
    ("token", "dropped"),
    [
        ("[Company Logo 1]", True),
        ("[Logo]", True),
        ("[Client logo placeholder]", True),
        ("[Image 2]", True),
        ("{{photo}}", True),
        ("[Image caption]", False),
        ("[Logo usage notes]", False),
        ("[Presenter Name]", False),
    ],
)
def test_repair_drops_a_logo_or_image_placeholder_from_text_mapping(token, dropped):
    amap = _token_amap(token, "[Title]")
    draft = BatchDraft(
        fields=[
            _field("img", "pptx:s6:sp3", "replace_token", token=token),
            _field("t", "pptx:s6:sp3", "replace_token", token="[Title]"),
        ]
    )
    kept = [f.field_id for f in _repair(draft, amap).fields]
    assert kept == (["t"] if dropped else ["img", "t"])


def test_ir_builder_version_is_bumped_for_the_logo_rule():
    from doc_intel.templates.ir_builder import IR_BUILDER_VERSION

    assert IR_BUILDER_VERSION >= 2


# ---- token coverage for every anchor kind ------------------------------------------------------


def _shape(anchor_id, token, slide):
    return _anchor(anchor_id, "shape_text", tokens=[token]).model_copy(
        update={"path": [f"Slide {slide}"]}
    )


async def _backfilled(amap, *fields):
    ir = await build_ir(FakeLLM(_reply(*fields)), amap, TID)
    return {f.field_id: f for f in ir.fields}


async def test_an_uncovered_text_box_token_copies_the_same_slide_field():
    made = _typed("pptx:s2:sp11", "[Opening Balance]", "currency", "opening").model_copy(
        update={
            "label": "Opening Balance",
            "description": "Balance at the start.",
            "mode": "synthesis",
        }
    )
    amap = AnchorMap(
        format="pptx",
        anchors=[
            _shape("pptx:s2:sp11", "[Opening Balance]", 2),
            _shape("pptx:s2:sp28", "[Opening Balance]", 2),
        ],
    )
    out = await _backfilled(amap, made)
    (copy,) = [f for f in out.values() if f.anchor_id == "pptx:s2:sp28"]
    assert copy.field_id not in {"opening"} and copy.write_mode == "replace_token"
    assert copy.constraints.token == "[Opening Balance]" and not copy.required
    assert (copy.label, copy.description, copy.expected_type, copy.mode) == (
        "Opening Balance",
        "Balance at the start.",
        "currency",
        "synthesis",
    )


async def test_an_uncovered_lone_text_box_token_gets_a_text_field():
    amap = AnchorMap(
        format="pptx", anchors=[_shape("pptx:s6:sp3", "[Scoring method / thresholds]", 6)]
    )
    filler = _anchor("pptx:s6:sp4", "shape_text", tokens=["[Title]"])
    amap.anchors.append(filler)
    out = await _backfilled(amap, _field("t", "pptx:s6:sp4", "replace_token", token="[Title]"))
    (f,) = [f for f in out.values() if f.anchor_id == "pptx:s6:sp3"]
    assert (f.expected_type, f.write_mode, f.constraints.token) == (
        "text",
        "replace_token",
        "[Scoring method / thresholds]",
    )
    assert f.description == "The value for Scoring method / thresholds."


@pytest.mark.parametrize("blank", ["TBD", "XXX", "____"])
async def test_a_generic_blank_in_a_text_box_is_left_to_the_ir_model(blank):
    # a bare blank in prose ("status TBD") may be the author's text; a named token never is
    amap = AnchorMap(
        format="pptx",
        anchors=[
            _anchor("pptx:s4:sp2", "shape_text", tokens=[blank]),
            _anchor("pptx:s4:sp3", "shape_text", tokens=["[Title]"]),
        ],
    )
    out = await _backfilled(amap, _field("t", "pptx:s4:sp3", "replace_token", token="[Title]"))
    assert [f.anchor_id for f in out.values()] == ["pptx:s4:sp3"]


async def test_a_token_copy_on_another_slide_does_not_copy_the_field():
    made = _typed("pptx:s2:sp11", "[Opening Balance]", "currency", "opening").model_copy(
        update={"label": "Opening Balance", "description": "Balance at the start."}
    )
    amap = AnchorMap(
        format="pptx",
        anchors=[
            _shape("pptx:s2:sp11", "[Opening Balance]", 2),
            _shape("pptx:s3:sp1", "[Opening Balance]", 3),
        ],
    )
    empty = IRDraft.model_construct(fields=[]).model_dump_json()  # one batch per slide
    ir = await build_ir(FakeLLM(_reply(made), empty), amap, TID)
    out = {f.field_id: f for f in ir.fields}
    (other,) = [f for f in out.values() if f.anchor_id == "pptx:s3:sp1"]
    assert (other.expected_type, other.description) == ("text", "The value for Opening Balance.")


async def test_a_sheet_cell_token_gets_no_replace_token_field():
    sheet = _anchor("xlsx:S!A1", "sheet_cell", tokens=["[Total]"])
    para = _anchor("xlsx:S!A2", "sheet_cell", tokens=["[Name]"])
    amap = AnchorMap(format="xlsx", anchors=[sheet, para])
    out = await _backfilled(amap, _field("n", "xlsx:S!A2", "fill_cell"))
    assert list(out) == ["n"]


def test_derived_mode_and_hidden_derivation():
    from doc_intel.templates.ir_builder import LLMIRField
    from doc_intel.templates.schemas import IRField

    assert _version() == 7
    assert "derived" in LLMIRField.model_json_schema()["properties"]["mode"]["enum"]
    assert "derivation" not in IRField.model_json_schema()["properties"]
    assert "derivation" not in LLMIRField.model_json_schema()["properties"]
    assert IRField.model_fields["derivation"].default is None


def _version():
    from doc_intel.templates.ir_builder import IR_BUILDER_VERSION

    return IR_BUILDER_VERSION


def _typed_field(fid, label, expected_type="currency"):
    return _field(fid, fid, "replace_token", token="{{t}}").model_copy(
        update={"label": label, "expected_type": expected_type, "mode": "extractive"}
    )


async def test_a_net_movement_label_is_forced_to_derived_whatever_the_llm_said():
    ids = ("net", "es", "close", "txt")
    amap = AnchorMap(format="pptx", anchors=[_anchor(i, tokens=["{{t}}"]) for i in ids])
    fields = [
        _typed_field("net", "NET MOVEMENT"),
        _typed_field("es", "Variación Neta"),
        _typed_field("close", "Closing Balance"),
        _typed_field("txt", "Net movement note", "text"),
    ]
    ir = await build_ir(FakeLLM(_reply(*fields)), amap, TID)
    got = {f.field_id: f.mode for f in ir.fields}
    assert got == {"net": "derived", "es": "derived", "close": "extractive", "txt": "extractive"}
    assert all(f.derivation is None for f in ir.fields)


async def test_numbered_ranking_and_breakdown_slots_are_forced_to_derived():
    ids = ("pay_1", "cat_1_amt", "cat_es", "obs", "plain_1")
    amap = AnchorMap(format="pptx", anchors=[_anchor(i, tokens=["{{t}}"]) for i in ids])
    fields = [
        _typed_field("payment_amount_1", "Payment amount 1").model_copy(
            update={"anchor_id": "pay_1", "section_path": ["Slide 6", "LARGEST PAYMENTS"]}
        ),
        _typed_field("category_1_amount", "Amount").model_copy(
            update={"anchor_id": "cat_1_amt", "section_path": ["Slide 5", "CATEGORY BREAKDOWN"]}
        ),
        _typed_field("categoria_2", "Categoría 2", "currency").model_copy(
            update={"anchor_id": "cat_es"}
        ),
        _typed_field("highest_spending_category", "Highest spending category", "text").model_copy(
            update={"anchor_id": "obs"}
        ),
        _typed_field("note_1", "Note 1", "text").model_copy(update={"anchor_id": "plain_1"}),
    ]
    ir = await build_ir(FakeLLM(_reply(*fields)), amap, TID)
    assert {f.field_id: f.mode for f in ir.fields} == {
        "payment_amount_1": "derived",
        "category_1_amount": "derived",
        "categoria_2": "derived",
        "highest_spending_category": "extractive",
        "note_1": "extractive",
    }


async def test_ranking_rule_needs_a_numeric_slot_in_the_numbered_grid():
    spec = [  # (field_id, label, type, heading)
        ("risk_1", "Risk 1", "text", "TOP RISKS"),
        ("risk_2", "Risk 2", "text", "TOP RISKS"),
        ("category_1", "Category 1", "text", "CATEGORY BREAKDOWN"),
        ("category_1_amount", "Amount 1", "currency", "CATEGORY BREAKDOWN"),
        ("payment_date_1", "Date 1", "date", "LARGEST PAYMENTS"),
        ("payment_payee_1", "Payee 1", "text", "LARGEST PAYMENTS"),
        ("payment_amount_1", "Amount 1", "currency", "LARGEST PAYMENTS"),
    ]
    amap = AnchorMap(format="pptx", anchors=[_anchor(i, tokens=["{{t}}"]) for i, *_ in spec])
    fields = [
        _typed_field(i, label, t).model_copy(update={"anchor_id": i, "section_path": ["S", h]})
        for i, label, t, h in spec
    ]
    ir = await build_ir(FakeLLM(_reply(*fields)), amap, TID)
    got = {f.field_id: f.mode for f in ir.fields}
    assert {k for k, v in got.items() if v == "derived"} == {
        "category_1",
        "category_1_amount",
        "payment_date_1",
        "payment_payee_1",
        "payment_amount_1",
    }


async def _modes(*spec):
    """spec: (field_id, label, type, context, section_path heading) -> {field_id: mode}"""
    anchors = [_anchor(s[0], tokens=["{{t}}"], context=s[3]) for s in spec]
    fields = [
        _typed_field(i, label, t).model_copy(update={"anchor_id": i, "section_path": ["S", h]})
        for i, label, t, _, h in spec
    ]
    ir = await build_ir(FakeLLM(_reply(*fields)), AnchorMap(format="pptx", anchors=anchors), TID)
    return {f.field_id: f.mode for f in ir.fields}


async def test_directional_totals_and_largest_singletons_are_forced_to_derived():
    got = await _modes(
        ("tdv", "Total deposits value", "currency", "TOTAL MONEY IN", "Slide 1"),
        ("twv", "Total withdrawals value", "currency", "TOTAL MONEY OUT", "Slide 1"),
        ("td", "Total Deposits", "currency", "TOTAL DEPOSITS", "Slide 3"),
        ("tw", "Total Withdrawals", "currency", "TOTAL WITHDRAWALS", "Slide 4"),
        ("lca", "Largest Credit Amount", "currency", "LARGEST CREDIT", "Slide 3"),
        ("lda", "Largest Debit Amount", "currency", "LARGEST DEBIT", "Slide 4"),
        ("nm", "Net Cash Movement Value", "currency", "NET MOVEMENT", "Slide 2"),
        ("ctx_net", "Value", "currency", "Net movement", "Slide 2"),
        ("emp", "Total employees", "number", "HEADCOUNT", "Slide 7"),
        ("notes", "Notes", "text", "TOTAL DEPOSITS", "Slide 3"),
        ("close", "Closing Balance Value", "currency", "CLOSING", "Slide 2"),
    )
    assert {k for k, v in got.items() if v == "derived"} == {
        "tdv",
        "twv",
        "td",
        "tw",
        "lca",
        "lda",
        "nm",
        "ctx_net",
    }


async def test_a_category_grid_with_text_typed_amounts_is_retyped_and_derived():
    spec = [
        (f"category_{i}{s}", f"Category {i}{lbl}", "text", "CATEGORY BREAKDOWN", "Slide 5")
        for i in (1, 2)
        for s, lbl in (("", ""), ("_amount", " Amount"))
    ]
    amap = AnchorMap(format="pptx", anchors=[_anchor(s[0], tokens=["{{t}}"]) for s in spec])
    fields = [
        _typed_field(i, label, t).model_copy(
            update={"anchor_id": i, "context": c, "section_path": ["S", h]}
        )
        for i, label, t, c, h in spec
    ]
    ir = await build_ir(FakeLLM(_reply(*fields)), amap, TID)
    got = {f.field_id: (f.mode, f.expected_type) for f in ir.fields}
    assert got == {
        "category_1": ("derived", "text"),
        "category_1_amount": ("derived", "currency"),
        "category_2": ("derived", "text"),
        "category_2_amount": ("derived", "currency"),
    }


@pytest.mark.parametrize(
    ("fid", "label", "ctx", "path", "want"),
    [
        ("t", "Total deposits value", "TOTAL MONEY IN", [], ("sum", "CR")),
        ("t", "Total", "TOTAL MONEY OUT", [], ("sum", "DR")),
        ("t", "Total Retiros", "", [], ("sum", "DR")),
        ("t", "Net Cash Movement", "NET MOVEMENT", [], ("diff", None)),
        ("largest_credit_amount", "Largest Credit Amount", "", [], ("top", "CR")),
        ("largest_debit_amount", "Largest Debit Amount", "", [], ("top", "DR")),
        ("payment_amount_1", "Amount", "Amount", ["S", "LARGEST PAYMENTS"], ("top", "DR")),
        ("receipt_date_2", "Date", "Date", ["S", "LARGEST RECEIPTS"], ("top", "CR")),
        ("category_1", "Category 1", "", ["S", "CATEGORY BREAKDOWN"], ("group_sum", None)),
        ("category_1_amount", "Amount", "", ["S", "SPENDING BY CATEGORY"], ("group_sum", "DR")),
        ("t", "Total employees", "", [], None),
        ("t", "Total deposits and withdrawals", "", [], None),
        ("t", "Closing balance", "", [], None),
        ("t", "Value", "NET MOVEMENT", [], ("diff", None)),
        ("t", "Total", "TOTAL MONEY OUT", [], ("sum", "DR")),
        ("t", "Opening balance", "NET CASH FLOW", [], None),  # context only for a bare label
        ("t", "Count", "TOTAL DEPOSITS", [], None),
        ("total_number_of_deposits", "Total number of deposits", "", [], None),
        ("t", "Total Credit Limit", "", [], None),
        ("t", "Total payment due", "", [], None),
        ("t", "Total", "INCOME STATEMENT", [], None),
        ("t", "Total cargo", "", [], None),
        ("t", "Total cargos", "", [], ("sum", "DR")),
        (
            "merchant_amount_1",
            "Amount",
            "",
            ["Credit card statement", "Top merchants"],
            ("top", None),
        ),
        ("total_deposits_2", "Total Deposits", "", [], ("sum", "CR")),
    ],
)
def test_derivation_intent(fid, label, ctx, path, want):
    from doc_intel.templates.ir_builder import derivation_intent

    f = _typed_field(fid, label).model_copy(update={"context": ctx, "section_path": path})
    assert derivation_intent(f) == want


async def test_a_value_n_text_grid_under_a_top_heading_is_not_retyped():
    spec = [(f"value_{i}", f"Value {i}", "text", "", "OUR TOP VALUES") for i in (1, 2)]
    amap = AnchorMap(format="pptx", anchors=[_anchor(s[0], tokens=["{{t}}"]) for s in spec])
    fields = [
        _typed_field(i, label, t).model_copy(update={"anchor_id": i, "section_path": ["S", h]})
        for i, label, t, _, h in spec
    ]
    ir = await build_ir(FakeLLM(_reply(*fields)), amap, TID)
    assert {(f.expected_type, f.mode) for f in ir.fields} == {("text", "extractive")}
