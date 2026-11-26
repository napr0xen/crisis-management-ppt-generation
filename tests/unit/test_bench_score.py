import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from doc_intel.templates.schemas import IRField
from tests.eval.bench.score import rates, score_case, source_rows

BENCH = Path(__file__).parents[1] / "eval" / "bench"
CASES = {p.stem: json.loads(p.read_text()) for p in sorted((BENCH / "cases").glob("*.json"))}
GOLD = json.loads((BENCH / "gold.json").read_text())

CASE = {
    "sources": [
        {
            "text": "Header\n\n| Name | Qty |\n| --- | --- |\n| apple | 3 |\n| pear | 5 |",
        }
    ]
}


def res(status, value=None):
    return SimpleNamespace(status=status, value=value)


def gold(fields, row_groups=()):
    return {"fields": fields, "row_groups": [list(g) for g in row_groups]}


def test_a_correct_case_scores_one():
    g = gold({"a": {"status": "populated", "value": "apple"}, "b": {"status": "missing"}})
    r = rates(score_case(CASE, g, {"a": res("populated", "apple"), "b": res("missing")}))
    assert r["field_accuracy"] == 1.0
    assert r["populated_but_wrong_rate"] == 0.0


def test_net_movement_populated_with_the_closing_balance_is_populated_but_wrong():
    g = gold(
        {
            "net": {"status": "populated", "value": 1219.5},
            "close": {"status": "populated", "value": 13669.5},
        }
    )
    results = {"net": res("populated", 13669.5), "close": res("populated", 13669.5)}
    r = rates(score_case(CASE, g, results))
    assert r["populated_but_wrong_rate"] == 0.5
    assert r["field_accuracy"] == 0.5


def test_populated_with_a_matching_value_where_gold_is_partial_is_not_wrong():
    g = gold({"a": {"status": "partial", "value": 1200}})
    c = score_case(CASE, g, {"a": res("populated", 1200)})
    assert c["populated_wrong"] == 0
    assert c["fields_correct"] == 0


def test_populated_where_gold_is_missing_counts_as_wrong():
    r = rates(score_case(CASE, gold({"a": {"status": "missing"}}), {"a": res("populated", "x")}))
    assert r["populated_but_wrong_rate"] == 1.0
    assert r["field_accuracy"] == 0.0


def test_a_populated_field_absent_from_gold_counts_as_wrong():
    assert (
        rates(score_case(CASE, gold({}), {"zz": res("populated", "x")}))["populated_but_wrong_rate"]
        == 1.0
    )


def test_table_rows_from_one_source_row_each_are_aligned():
    table = res("populated", [["apple", "3"], ["pear", "5"]])
    r = rates(score_case(CASE, gold({"t": {"status": "populated"}}), {"t": table}))
    assert r["row_alignment_accuracy"] == 1.0
    assert r["rows"] == 2


def test_a_table_row_mixing_two_source_rows_is_misaligned():
    table = res("populated", [["apple", "3"], ["apple", "5"]])
    r = rates(score_case(CASE, gold({"t": {"status": "populated"}}), {"t": table}))
    assert (r["rows"], r["rows_aligned"]) == (2, 1)
    assert r["row_alignment_accuracy"] == 0.5


def test_empty_cells_are_ignored_and_numbers_compare_as_numbers():
    table = res("populated", [["apple", "", 3]])
    assert rates(score_case(CASE, gold({}), {"t": table}))["row_alignment_accuracy"] == 1.0


def test_a_row_group_mixing_rows_is_misaligned():
    g = gold({}, [["n1", "q1"], ["n2", "q2"]])
    results = {
        "n1": res("populated", "apple"),
        "q1": res("populated", 3),
        "n2": res("partial", "pear"),
        "q2": res("populated", 3),
    }
    r = rates(score_case(CASE, g, results))
    assert (r["rows"], r["rows_aligned"]) == (2, 1)


def test_a_row_group_with_a_missing_field_is_not_a_row():
    g = gold({}, [["n1", "q1"]])
    r = rates(score_case(CASE, g, {"n1": res("populated", "apple"), "q1": res("missing")}))
    assert r["rows"] == 0


def test_zero_denominators():
    r = rates(score_case(CASE, gold({}), {}))
    assert r["field_accuracy"] == 0.0
    assert r["populated_but_wrong_rate"] == 0.0
    assert r["row_alignment_accuracy"] == 1.0  # no populated rows: n=0, nothing misaligned
    assert r["rows"] == 0


def test_source_rows_skip_separators():
    assert source_rows(CASE) == ["Name | Qty", "apple | 3", "pear | 5"]


def test_bench_has_the_three_cases():
    assert set(CASES) == {"finance_statement", "procurement_review", "multilingual"} == set(GOLD)


@pytest.mark.parametrize("name", sorted(CASES))
def test_case_and_gold_are_consistent_and_gold_scores_one(name):
    case, g = CASES[name], GOLD[name]
    ids = {IRField.model_validate(f).field_id for f in case["template"]["fields"]}
    assert set(g["fields"]) <= ids
    assert all(f in ids for group in g["row_groups"] for f in group)
    results = {f: res(v["status"], v.get("value")) for f, v in g["fields"].items()}
    r = rates(score_case(case, g, results))
    assert r["field_accuracy"] == 1.0
    assert r["populated_but_wrong_rate"] == 0.0
    assert r["row_alignment_accuracy"] == 1.0
    assert r["rows"] > 0
