"""plan_sync: the pure decision of what the templates/ folder sync does."""

from types import SimpleNamespace

from doc_intel.templates.library import SyncPlan, plan_sync


def row(id_, filename, sha, version=1):
    return SimpleNamespace(id=id_, filename=filename, sha256=sha, ir_builder_version=version)


def test_empty_everything_is_an_empty_plan():
    assert plan_sync([], [], 1) == SyncPlan([], [], [], [])


def test_new_file_is_added_in_filename_order():
    assert plan_sync([("a.pptx", "1"), ("b.pptx", "2")], [], 1).add == ["a.pptx", "b.pptx"]


def test_unchanged_file_is_kept():
    assert plan_sync([("a.pptx", "1")], [row("r1", "a.pptx", "1")], 1) == SyncPlan([], [], [], [])


def test_changed_hash_is_updated():
    plan = plan_sync([("a.pptx", "2")], [row("r1", "a.pptx", "1")], 1)
    assert plan == SyncPlan([], [("a.pptx", "r1")], [], [])


def test_older_ir_builder_version_is_updated():
    plan = plan_sync([("a.pptx", "1")], [row("r1", "a.pptx", "1", version=0)], 1)
    assert plan.update == [("a.pptx", "r1")]


def test_row_without_a_file_is_pruned():
    plan = plan_sync([("a.pptx", "1")], [row("r1", "a.pptx", "1"), row("r2", "gone.pptx", "9")], 1)
    assert plan == SyncPlan([], [], ["r2"], [])


def test_duplicate_bytes_under_two_names_keep_the_first():
    plan = plan_sync([("a.pptx", "1"), ("b.pptx", "1")], [], 1)
    assert (plan.add, plan.skipped) == (["a.pptx"], ["b.pptx"])


def test_file_with_the_hash_of_a_kept_row_of_another_name_is_skipped():
    rows = [row("r1", "z.pptx", "1")]
    plan = plan_sync([("a.pptx", "1"), ("z.pptx", "1")], rows, 1)
    assert (plan.add, plan.skipped, plan.update) == ([], ["a.pptx"], [])


def test_renamed_file_prunes_the_old_row_and_adds_the_new_name():
    plan = plan_sync([("b.pptx", "1")], [row("r1", "a.pptx", "1")], 1)
    assert (plan.add, plan.prune) == (["b.pptx"], ["r1"])


def test_second_row_with_the_same_filename_is_pruned():
    rows = [row("r1", "a.pptx", "1"), row("r2", "a.pptx", "5")]
    plan = plan_sync([("a.pptx", "1")], rows, 1)
    assert (plan.prune, plan.update, plan.add) == (["r2"], [], [])
