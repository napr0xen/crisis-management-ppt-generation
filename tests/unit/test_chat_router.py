import json
import uuid

import pytest

from doc_intel.chat.router import HISTORY_CHARS, HISTORY_MESSAGES, ChatMessage, Intent, route
from doc_intel.db.models import Document
from doc_intel.llm.gateway import LLMInvalidOutput
from tests.fakes import FakeLLM


def _doc(filename: str, key_ext: str, role: str = "source") -> Document:
    doc_id = uuid.uuid4()
    return Document(id=doc_id, filename=filename, role=role, storage_key=f"u/p/{doc_id}.{key_ext}")


def _reply(
    intent: str, template: object = None, candidates: list | None = None, fmt: str | None = None
) -> str:
    return json.dumps(
        {
            "intent": intent,
            "template_document_id": template and str(template),
            "candidates": [str(c) for c in candidates or []],
            "format": fmt,
        }
    )


def _msgs(*texts: str) -> list[ChatMessage]:
    return [ChatMessage(role="user", content=t) for t in texts]


async def test_single_call_and_prompt_shape() -> None:
    pdf, pptx = _doc("a.pdf", "pdf"), _doc("deck.pptx", "pptx", "template")
    llm = FakeLLM(_reply("qa"))
    await route(llm, _msgs("hello"), [pdf, pptx])
    assert len(llm.calls) == 1
    system, user = llm.calls[0][0]
    assert system["role"] == "system" and "map_template" in system["content"]
    assert f"{pdf.id} | a.pdf | source | pdf" in user["content"]
    assert f"{pptx.id} | deck.pptx | template | pptx" in user["content"]
    assert "user: hello" in user["content"]


async def test_history_cut_to_last_messages_and_chars() -> None:
    texts = [f"m{i:02d}" for i in range(12)]
    long = "HEAD" + "x" * HISTORY_CHARS + "TAIL"
    llm = FakeLLM(_reply("chitchat"))
    await route(llm, _msgs(*texts, long), [])
    prompt = llm.calls[0][0][1]["content"]
    assert "m00" not in prompt and "m02" not in prompt
    assert "m03" in prompt and "m11" in prompt
    assert "HEAD" not in prompt and "TAIL" in prompt
    assert HISTORY_MESSAGES == 10


async def test_exactly_one_template_sets_id() -> None:
    pptx = _doc("deck.pptx", "pptx")
    llm = FakeLLM(_reply("map_template", candidates=[pptx.id]))
    result = await route(llm, _msgs("fill it"), [pptx, _doc("a.pdf", "pdf")])
    assert result.intent == "map_template"
    assert result.template_document_id == pptx.id


async def test_template_document_id_counts_and_dedupes() -> None:
    docx = _doc("r.docx", "docx")
    llm = FakeLLM(_reply("map_template", template=docx.id, candidates=[docx.id, docx.id]))
    result = await route(llm, _msgs("fill"), [docx])
    assert result.template_document_id == docx.id


async def test_several_candidates_have_no_template_id() -> None:
    a, b = _doc("a.pptx", "pptx"), _doc("b.xlsx", "xlsx")
    llm = FakeLLM(_reply("map_template", candidates=[b.id, a.id]))
    result = await route(llm, _msgs("fill"), [a, b])
    assert result.template_document_id is None


async def test_zero_valid_candidates() -> None:
    llm = FakeLLM(_reply("map_template"))
    result = await route(llm, _msgs("fill"), [])
    assert result.template_document_id is None


async def test_invalid_ids_dropped() -> None:
    pptx, pdf = _doc("deck.pptx", "pptx"), _doc("a.pdf", "pdf")
    reply = json.dumps(
        {
            "intent": "map_template",
            "template_document_id": "not-a-uuid",
            "candidates": [str(uuid.uuid4()), str(pdf.id), "garbage", str(pptx.id)],
        }
    )
    result = await route(FakeLLM(reply), _msgs("fill"), [pptx, pdf])
    assert result.template_document_id == pptx.id


async def test_format_comes_from_storage_key_not_filename() -> None:
    fake = _doc("x.pptx", "pdf")
    real = _doc("notes.txt", "docx")
    llm = FakeLLM(_reply("map_template", candidates=[fake.id, real.id]))
    result = await route(llm, _msgs("fill"), [fake, real])
    assert result.template_document_id == real.id


@pytest.mark.parametrize("intent", ["qa", "chitchat", "edit"])
async def test_non_template_intents_return_no_ids(intent: str) -> None:
    pptx = _doc("deck.pptx", "pptx")
    result = await route(
        FakeLLM(_reply(intent, template=pptx.id, candidates=[pptx.id])), _msgs("hi"), [pptx]
    )
    assert result.intent == intent
    assert result.template_document_id is None


async def test_invalid_json_twice_propagates() -> None:
    with pytest.raises(LLMInvalidOutput):
        await route(FakeLLM("nope", "still nope"), _msgs("hi"), [])


# ------------------------------------------- data-loss guard: a source is never silently a template


async def _pick(messages: list[str], docs: list[Document], named: Document) -> Intent:
    return await route(FakeLLM(_reply("map_template", template=named.id)), _msgs(*messages), docs)


async def test_unconfirmed_pick_among_several_template_formats_is_not_returned() -> None:
    a, b = _doc("a.pptx", "pptx"), _doc("b.docx", "docx")
    result = await _pick(["fill the template"], [a, b], b)
    assert result.intent == "map_template" and result.template_document_id is None


async def test_filename_in_last_user_message_confirms_case_insensitively() -> None:
    a, b = _doc("a.pptx", "pptx"), _doc("Report.docx", "docx")
    result = await _pick(["fill the template", "use REPORT.docx"], [a, b], b)
    assert result.template_document_id == b.id


async def test_filename_in_an_earlier_message_does_not_confirm() -> None:
    a, b = _doc("a.pptx", "pptx"), _doc("b.docx", "docx")
    result = await _pick(["use b.docx", "fill the template"], [a, b], b)
    assert result.template_document_id is None


async def test_list_number_confirms_the_nth_template_format_document() -> None:
    pdf, a, b = _doc("n.pdf", "pdf"), _doc("a.pptx", "pptx"), _doc("b.docx", "docx")
    result = await _pick(["2"], [pdf, a, b], b)  # the pdf is not counted
    assert result.template_document_id == b.id
    assert (await _pick(["1"], [pdf, a, b], b)).template_document_id is None  # names #2, said #1
    assert (await _pick(["file 12"], [pdf, a, b], b)).template_document_id is None


async def test_already_template_role_is_confirmed() -> None:
    a, b = _doc("a.pptx", "pptx"), _doc("b.docx", "docx", "template")
    result = await _pick(["fill the template"], [a, b], b)
    assert result.template_document_id == b.id


async def test_only_template_format_document_is_confirmed() -> None:
    pdf, a = _doc("n.pdf", "pdf"), _doc("a.pptx", "pptx")
    result = await _pick(["fill the template"], [pdf, a], a)
    assert result.template_document_id == a.id


# ------------------------------------------- output format (library templates, Phase 9)


@pytest.mark.parametrize(
    "raw, expected", [("pptx", "pptx"), ("PPTX", "pptx"), ("pdf", None), ("", None), (None, None)]
)
async def test_format_is_parsed_and_invalid_is_ignored(raw, expected) -> None:
    result = await route(FakeLLM(_reply("map_template", fmt=raw)), _msgs("make it"), [])
    assert result.format == expected


async def test_source_docx_is_not_auto_confirmed_for_a_pptx_request() -> None:
    docx = _doc("memo.docx", "docx")
    llm = FakeLLM(_reply("map_template", template=docx.id, candidates=[docx.id], fmt="pptx"))
    result = await route(llm, _msgs("make a powerpoint out of this"), [docx])
    assert result.intent == "map_template" and result.format == "pptx"
    assert result.template_document_id is None


@pytest.mark.parametrize("fmt", [None, "pptx"])
async def test_single_pptx_is_still_auto_confirmed(fmt) -> None:
    pdf, pptx = _doc("n.pdf", "pdf"), _doc("a.pptx", "pptx")
    llm = FakeLLM(_reply("map_template", template=pptx.id, fmt=fmt))
    result = await route(llm, _msgs("fill it"), [pdf, pptx])
    assert result.template_document_id == pptx.id


async def test_format_filter_leaves_list_numbers_on_the_unfiltered_order() -> None:
    a, b = _doc("a.pptx", "pptx"), _doc("b.pptx", "pptx")
    llm = FakeLLM(_reply("map_template", template=b.id, fmt="pptx"))
    assert (await route(llm, _msgs("1"), [a, b])).template_document_id is None
    llm = FakeLLM(_reply("map_template", template=b.id, fmt="pptx"))
    assert (await route(llm, _msgs("2"), [a, b])).template_document_id == b.id


async def test_format_filter_drops_other_format_candidates() -> None:
    docx, pptx = _doc("m.docx", "docx"), _doc("a.pptx", "pptx")
    llm = FakeLLM(_reply("map_template", candidates=[docx.id, pptx.id], fmt="pptx"))
    result = await route(llm, _msgs("make a deck"), [docx, pptx])
    assert result.template_document_id == pptx.id
