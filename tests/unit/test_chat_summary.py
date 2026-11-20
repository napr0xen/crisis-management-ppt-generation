import re
import uuid

from doc_intel.chat.qa import Answer, Citation
from doc_intel.chat.summary import HELP_TEXT, render_answer, render_candidates, render_summary
from doc_intel.db.models import Document
from doc_intel.mapping.schemas import (
    Candidate,
    ConflictDetails,
    Evidence,
    FieldMappingResult,
)
from doc_intel.templates.schemas import Constraints, IRField, TemplateIR

DOC = uuid.uuid4()


def _ev(filename: str = "a.pdf", page: int | None = 2, quote: str = "quoted text here") -> Evidence:
    return Evidence(
        document_id=DOC,
        chunk_id=uuid.uuid4(),
        filename=filename,
        page_number=page,
        section_heading=None,
        quote=quote,
    )


def _res(field_id: str, status: str, **kw) -> FieldMappingResult:
    base = {
        "field_id": field_id,
        "status": status,
        "value": None,
        "evidence": [],
        "confidence": 0.5,
        "missing_reason": None,
        "checked_documents": [],
        "conflict_details": None,
        "provenance": "rag",
    }
    return FieldMappingResult(**(base | kw))


def _ir(*ids: str) -> TemplateIR:
    fields = [
        IRField(
            field_id=i,
            anchor_id="a1",
            section_path=[],
            label=f"Label {i}",
            description="",
            expected_type="text",
            required=False,
            write_mode="replace_text",
            constraints=Constraints(),
        )
        for i in ids
    ]
    return TemplateIR(template_id=uuid.uuid4(), version=1, format="pptx", fields=fields)


def _doc(filename: str, ext: str) -> Document:
    return Document(id=uuid.uuid4(), filename=filename, storage_key=f"u/p/x.{ext}")


COUNTS = {"populated": 1, "partial": 1, "missing": 2, "conflict": 1, "warnings": []}


def _full() -> str:
    results = {
        "ok": _res("ok", "populated", value="fine", evidence=[_ev()]),
        "par": _res("par", "partial", value=["x", "y"], evidence=[_ev()]),
        "miss": _res("miss", "missing", missing_reason="no_relevant_chunks"),
        "unk": _res("unk", "missing", missing_reason="not_found_by_llm"),
        "con": _res(
            "con",
            "conflict",
            conflict_details=ConflictDetails(
                candidates=[
                    Candidate(value="10", evidence=[_ev("a.pdf", 1)]),
                    Candidate(value="20", evidence=[_ev("b.docx", None)]),
                ]
            ),
        ),
    }
    summary = COUNTS | {"warnings": [{"field_id": "ok", "code": "max_chars_exceeded"}]}
    return render_summary(results, summary, _ir(*results), "http://x/dl?sig=1")


def test_help_text_mentions_capabilities() -> None:
    for word in ("PDF", "PPTX", "fill the template", "verified"):
        assert word in HELP_TEXT


def test_summary_counts_and_sections() -> None:
    out = _full()
    assert "1 populated" in out and "1 partial" in out and "2 missing" in out
    assert "1 conflict" in out
    assert "Label miss" in out and "no relevant passages found" in out
    assert "Label unk" in out and "not stated in the documents" in out
    assert "Label con" in out and "never auto-resolved" in out
    assert "10" in out and "20" in out and "a.pdf p.1" in out and "b.docx" in out
    assert "b.docx p." not in out
    assert "Label par" in out and "x; y" in out
    assert "Label ok" in out and "character limit" in out
    assert "[Download the filled template](http://x/dl?sig=1)" in out
    assert "expires" in out


def test_summary_omits_empty_sections_and_no_link() -> None:
    results = {"ok": _res("ok", "populated", value="v", evidence=[_ev()])}
    summary = {"populated": 1, "partial": 0, "missing": 0, "conflict": 0, "warnings": []}
    out = render_summary(results, summary, _ir("ok"), None)
    for heading in ("Missing", "Conflicts", "Partial", "Warnings"):
        assert heading not in out
    assert "no file was produced" in out.lower()
    assert "Download" not in out


def test_summary_label_fallback_and_all_reasons_and_warnings() -> None:
    reasons = {
        "evidence_failed_verification": "could not be verified",
        "insufficient_evidence": "not enough evidence",
        "llm_error": "model failed",
        "cleared_by_user": "cleared by user",
    }
    results = {f"f{i}": _res(f"f{i}", "missing", missing_reason=r) for i, r in enumerate(reasons)}
    summary = {
        "populated": 0,
        "partial": 0,
        "missing": 4,
        "conflict": 0,
        "warnings": [
            {"field_id": "zzz", "code": "formula_cell"},
            {"field_id": "f0", "code": "table_overflow"},
        ],
    }
    out = render_summary(results, summary, _ir("other"), "u")
    for text in reasons.values():
        assert text in out
    assert "zzz" in out and "f0" in out and "formula" in out and "rows" in out


def test_value_formatting() -> None:
    table = [["a", "b"], ["c", "d"]]
    results = {
        "t": _res("t", "partial", value=table, evidence=[_ev()]),
        "n": _res("n", "partial", value=3.5, evidence=[_ev()]),
        "s": _res("s", "partial", value="line1\nline2 " + "z" * 300, evidence=[_ev()]),
    }
    summary = {"populated": 0, "partial": 3, "missing": 0, "conflict": 0, "warnings": []}
    out = render_summary(results, summary, _ir("t", "n", "s"), None)
    assert "a \\| b; c \\| d" in out
    assert "3.5" in out
    assert "line1 line2" in out
    assert "z" * 300 not in out and "..." in out


def _cit(marker: int, page: int | None, quote: str) -> Citation:
    return Citation(
        marker=marker,
        document_id=DOC,
        chunk_id=uuid.uuid4(),
        filename="f.pdf",
        page_number=page,
        section_heading=None,
        quote=quote,
    )


def test_render_answer_sources_ordered_and_truncated() -> None:
    ans = Answer(
        answer_markdown="Answer [1] [2]",
        citations=[_cit(2, None, "second\n  quote"), _cit(1, 4, "q" * 400)],
        found=True,
    )
    out = render_answer(ans)
    assert out.startswith("Answer [1] [2]")
    assert "Sources" in out
    assert out.index("[1] f.pdf p.4") < out.index("[2] f.pdf:")
    assert '"second quote"' in out
    assert "q" * 300 + '..."' in out and "q" * 301 not in out


def test_render_answer_not_found() -> None:
    ans = Answer(answer_markdown="nothing", citations=[], found=False)
    assert render_answer(ans) == "nothing"


def test_render_candidates() -> None:
    out = render_candidates(
        [_doc("a.pdf", "pdf"), _doc("deck.pptx", "pptx"), _doc("r.docx", "docx")]
    )
    assert "1. deck.pptx" in out and "2. r.docx" in out and "a.pdf" not in out
    assert "number or the filename" in out


def test_render_candidates_none() -> None:
    out = render_candidates([_doc("a.pdf", "pdf"), _doc("x.pptx", "pdf")])
    assert "attach" in out.lower() and "PPTX, DOCX or XLSX" in out
    assert "1." not in out


# ------------------------------------------------ document-derived text is never live markdown

EVIL = "![x](https://evil/p.png) <img src=x> `c` *b* _i_ [l](http://e) #h | t"


def _no_live_markup(out: str) -> None:
    for live in ("![x](", "<img", "[l](http://e)", "`c`", "*b*"):
        assert not re.search(r"(?<!\\)" + re.escape(live), out), live


def test_citation_filename_and_quote_are_escaped() -> None:
    cit = _cit(1, 1, EVIL).model_copy(update={"filename": "a![x](https://evil/p.png).pdf"})
    out = render_answer(Answer(answer_markdown="Ans [1]", citations=[cit], found=True))
    _no_live_markup(out)
    assert "![x](https://evil" not in out
    assert "\\!\\[x\\]\\(https://evil/p.png\\)" in out
    assert "Ans [1]" in out and "[1] a" in out


def test_summary_values_labels_and_filenames_are_escaped() -> None:
    ir = _ir("p", "c")
    ir.fields[0].label = EVIL
    ir.fields[1].label = "![y](https://evil/l.png)"
    results = {
        "p": _res("p", "partial", value=EVIL, evidence=[_ev(filename=EVIL)]),
        "c": _res(
            "c",
            "conflict",
            conflict_details=ConflictDetails(
                candidates=[Candidate(value=EVIL, evidence=[_ev(filename=EVIL)])] * 2
            ),
        ),
    }
    summary = {"populated": 0, "partial": 1, "missing": 0, "conflict": 1, "warnings": []}
    out = render_summary(results, summary, ir, "http://x/dl?sig=1")
    _no_live_markup(out)
    assert "![y](" not in out
    assert "[Download the filled template](http://x/dl?sig=1)" in out  # real link stays a link


def test_candidate_list_filenames_are_escaped() -> None:
    out = render_candidates([_doc("a![x](https://evil/p.png).pptx", "pptx")])
    assert "![x](" not in out and "1. a\\!\\[x\\]" in out


def test_answer_markdown_strips_images_and_links_keeps_markers() -> None:
    md = "See ![x](https://evil/p.png) and [click](https://evil/l) now [1] <b>hi</b>"
    out = render_answer(Answer(answer_markdown=md, citations=[], found=False))
    assert "![" not in out and "evil" not in out and "(http" not in out
    assert "click" in out and "[1]" in out
    assert "\\<b\\>" in out and "<b>" not in out


def _live_brackets(out: str) -> list[str]:
    """Unescaped `[`/`]` other than plain `[n]` markers: anything that could form a link."""
    plain = re.sub(r"(?<!\\)\[\d+\](?![(\[:])", "", out)
    return re.findall(r"(?<!\\)[\[\]]", plain)


def test_answer_reference_style_image_and_definition_are_inert() -> None:
    md = "Spend rose [1].\n\n![x][1]\n[y][evil]\n\n[1]: https://evil/p.png?d=secret\n[evil]: https://e"
    out = render_answer(Answer(answer_markdown=md, citations=[], found=True))
    assert _live_brackets(out) == []
    assert not re.search(r"(?m)^\s{0,3}\[[^\]]+\]:", out)  # no live reference definition
    assert "Spend rose [1]." in out


def test_answer_nested_brackets_and_escape_tricks_are_inert() -> None:
    for md in ("[a [b]](https://evil/l)", "![a [b]](https://evil/p.png)", "\\[x](https://e)"):
        out = render_answer(Answer(answer_markdown=md, citations=[], found=True))
        assert _live_brackets(out) == [], md


def test_answer_marker_never_becomes_a_link() -> None:
    md = "Claim [1](https://evil/l) and [2][3] then [4]"
    out = render_answer(Answer(answer_markdown=md, citations=[], found=True))
    assert "[1](" not in out and "[4]" in out
    assert _live_brackets(out) == []


def test_answer_keeps_markdown_emphasis_and_lists() -> None:
    md = "**Total** is *high*\n\n- one [1]\n- two [2]\n\n| a | b |"
    out = render_answer(Answer(answer_markdown=md, citations=[], found=True))
    assert out == md


def test_render_library_lists_format_and_clips_description() -> None:
    from doc_intel.chat.summary import render_library
    from doc_intel.db.models import LibraryTemplate

    a = LibraryTemplate(name="Deck", format="pptx", description="d" * 300)
    b = LibraryTemplate(name="Memo", format="docx", description="")
    out = render_library([a, b])
    assert out.startswith("No template is attached to this chat. Which library template")
    assert "1. Deck (pptx): " + "d" * 200 + "..." in out and "d" * 201 not in out
    assert "\n2. Memo (docx)\n" in out
    assert out.endswith("Reply with the number or the name of the template.")


def test_render_library_escapes_name_and_description() -> None:
    from doc_intel.chat.summary import render_library
    from doc_intel.db.models import LibraryTemplate

    evil = "![x](http://evil/p.png)"
    out = render_library([LibraryTemplate(name=evil, format="pptx", description=evil)])
    assert evil not in out and "\\!\\[x\\]\\(http://evil/p.png\\)" in out


def test_help_text_mentions_the_library() -> None:
    assert "make a powerpoint out of this" in HELP_TEXT
