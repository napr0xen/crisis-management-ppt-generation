import uuid

from doc_intel.chat.library import library_choice
from doc_intel.chat.router import ChatMessage
from doc_intel.chat.summary import render_library
from doc_intel.db.models import LibraryTemplate


def _t(name: str, fmt: str = "pptx") -> LibraryTemplate:
    return LibraryTemplate(id=uuid.uuid4(), name=name, format=fmt, description="")


def _msgs(*pairs: tuple[str, str]) -> list[ChatMessage]:
    return [ChatMessage(role=r, content=c) for r, c in pairs]


def test_name_match_is_case_insensitive_and_word_bounded() -> None:
    a, b = _t("Status Update"), _t("Status")
    assert library_choice([a, b], _msgs(("user", "use the STATUS UPDATE please"))) is a
    assert library_choice([_t("Plan")], _msgs(("user", "make a planner"))) is None


def test_longest_matching_name_wins_and_tie_is_none() -> None:
    short, long = _t("Report"), _t("Report Weekly")
    assert library_choice([short, long], _msgs(("user", "fill Report Weekly"))) is long
    twin_a, twin_b = _t("Same", "pptx"), _t("Same", "docx")
    assert library_choice([twin_a, twin_b], _msgs(("user", "Same"))) is None


def test_name_match_uses_the_last_user_message_only() -> None:
    a = _t("Alpha")
    assert (
        library_choice([a], _msgs(("user", "Alpha"), ("assistant", "ok"), ("user", "hmm"))) is None
    )


def test_number_reply_resolves_through_the_rendered_line() -> None:
    a, b = _t("Alpha"), _t("Beta", "docx")
    listing = render_library([a, b])
    msgs = _msgs(("user", "make a deck"), ("assistant", listing), ("user", "2"))
    assert library_choice([a, b], msgs) is b
    # a filtered/reordered library cannot shift it: resolution is by the rendered line
    assert library_choice([b, a], msgs) is b


def test_number_without_a_library_list_or_out_of_range_is_none() -> None:
    a = _t("Alpha")
    assert library_choice([a], _msgs(("assistant", "hello"), ("user", "1"))) is None
    listing = render_library([a])
    assert library_choice([a], _msgs(("assistant", listing), ("user", "5"))) is None
    assert library_choice([a], _msgs(("assistant", listing), ("user", "no digits"))) is None
    assert library_choice([], _msgs(("user", "1"))) is None


def test_escaped_names_still_resolve() -> None:
    a = _t("A_b [x]")
    msgs = _msgs(("assistant", render_library([a])), ("user", "1"))
    assert library_choice([a], msgs) is a


def test_by_name_false_keeps_only_the_number_reply() -> None:
    a, b = _t("Alpha"), _t("Beta", "docx")
    assert library_choice([a, b], _msgs(("user", "use Alpha")), by_name=False) is None
    msgs = _msgs(("assistant", render_library([a, b])), ("user", "2"))
    assert library_choice([a, b], msgs, by_name=False) is b
