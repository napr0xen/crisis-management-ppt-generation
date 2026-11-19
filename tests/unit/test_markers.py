import pytest

from doc_intel.mapping.schemas import FieldMappingResult
from doc_intel.templates.schemas import Constraints, IRField
from doc_intel.writers.markers import content

MISSING = "[MISSING: {label}]"
CONFLICT = "[CONFLICT: See Chat Summary]"


def _field(etype="text", mode="replace_text", **constraints) -> IRField:
    return IRField(
        field_id="f1",
        anchor_id="a",
        section_path=[],
        label="Cases",
        description="",
        expected_type=etype,
        required=True,
        write_mode=mode,
        constraints=Constraints(**constraints),
    )


def _result(status="populated", value=None) -> FieldMappingResult:
    base = {
        "field_id": "f1",
        "status": status,
        "value": value,
        "evidence": [],
        "confidence": 1.0,
        "missing_reason": "not_found_by_llm" if status == "missing" else None,
        "checked_documents": [],
        "conflict_details": None,
        "provenance": "user",
    }
    ev = {
        "document_id": "00000000-0000-0000-0000-000000000001",
        "chunk_id": "00000000-0000-0000-0000-000000000002",
        "filename": "f",
        "page_number": None,
        "section_heading": None,
        "quote": "q" * 30,
    }
    if status == "partial":
        base["evidence"] = [ev]
    if status == "conflict":
        cand = {"value": "x", "evidence": [ev]}
        base["conflict_details"] = {"candidates": [cand, cand]}
    return FieldMappingResult.model_validate(base)


def _c(field, result, fmt="pptx", missing=MISSING, conflict=CONFLICT):
    return content(field, result, fmt, missing, conflict)


@pytest.mark.parametrize("fmt", ["pptx", "docx", "xlsx"])
def test_missing_and_conflict_markers(fmt):
    assert _c(_field(), _result("missing"), fmt) == ["[MISSING: Cases]"]
    assert _c(_field(), _result("conflict"), fmt) == [CONFLICT]


def test_label_with_braces_is_never_formatted():
    f = _field().model_copy(update={"label": "{0} {x} }{"})
    assert _c(f, _result("missing")) == ["[MISSING: {0} {x} }{]"]


def test_marker_for_table_is_one_item_list():
    f = _field("table", "fill_table_rows")
    assert _c(f, _result("missing")) == ["[MISSING: Cases]"]


def test_text_and_lists():
    assert _c(_field(), _result(value="hello")) == ["hello"]
    assert _c(_field("paragraphs"), _result(value=["a", "b"])) == ["a", "b"]
    assert _c(_field("bullet_list"), _result("partial", ["a"])) == ["a"]


def test_max_items_not_enforced():
    assert _c(_field("bullet_list", max_items=1), _result(value=["a", "b", "c"])) == ["a", "b", "c"]


def test_table_modes():
    rows = [["a", "b"], ["c", "d"]]
    assert _c(_field("table", "fill_table_rows"), _result(value=rows)) == rows
    assert _c(_field("table", "replace_text"), _result(value=rows)) == ["a | b", "c | d"]


def test_numbers_text_targets():
    assert _c(_field("number"), _result(value=12)) == ["12"]
    assert _c(_field("currency", unit="USD"), _result(value=1.5)) == ["1.5 USD"]
    assert _c(_field("percent"), _result(value=12.5)) == ["12.5%"]


def test_numbers_xlsx_typed():
    assert _c(_field("number", "fill_cell"), _result(value=12), "xlsx") == 12
    assert _c(_field("currency", "fill_cell", unit="USD"), _result(value=1.5), "xlsx") == 1.5
    assert _c(_field("percent", "fill_cell"), _result(value=50), "xlsx") == 0.5


def test_date_text_is_iso_and_xlsx_is_serial():
    assert _c(_field("date"), _result(value="2024-03-05")) == ["2024-03-05"]
    assert _c(_field("date", "fill_cell"), _result(value="1900-01-01"), "xlsx") == 2
    assert _c(_field("date", "fill_cell"), _result(value="2024-03-05"), "xlsx") == 45356


def test_xlsx_lists_join_into_one_string():
    f = _field("bullet_list", "fill_cell")
    assert _c(f, _result(value=["a", "b"]), "xlsx") == ["a; b"]
    assert _c(_field(), _result(value="x"), "xlsx") == ["x"]


def test_xml_illegal_characters_are_stripped():
    dirty = "a\x00b\x0bc\ud800d￾e\x1ff ok\t\n"
    assert _c(_field(), _result(value=dirty)) == ["abcdef ok\t\n"]
    assert _c(_field("bullet_list"), _result(value=["\x01x"])) == ["x"]
    assert _c(_field("table", "fill_table_rows"), _result(value=[["\x02y"]])) == [["y"]]
    f = _field().model_copy(update={"label": "L\x00"})
    assert _c(f, _result("missing")) == ["[MISSING: L]"]
    assert _c(_field(), _result("conflict"), conflict="C\x00") == ["C"]
