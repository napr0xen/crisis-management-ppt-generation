"""Grounded Q&A (system-design § 9.5): retrieve -> one JSON LLM call -> verify -> answer."""

import json
import uuid

import pytest

from doc_intel.chat.qa import NOT_FOUND, answer
from doc_intel.core.errors import UpstreamUnavailable
from doc_intel.core.tenancy import TenantContext
from doc_intel.embeddings.schemas import Hit
from doc_intel.llm.gateway import LLMError
from tests.fakes import FakeLLM

CTX = TenantContext("user_1", uuid.uuid4())


def _hit(text: str, page: int | None = 4, section: str | None = "Impact") -> Hit:
    return Hit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="ops_report.pdf",
        page_number=page,
        section_heading=section,
        element_type="text",
        text=text,
        score=0.5,
    )


OUTAGE = _hit("Approximately 12,000 customers were affected by the outage.")
SHELTER = _hit("Three shelters opened at the high school.", page=None, section=None)


class FakeRetriever:
    def __init__(self, hits: list[Hit]) -> None:
        self.hits, self.calls = hits, []

    async def search(self, ctx, query, document_ids=None, k=20, top_n=8):
        self.calls.append((ctx, query, document_ids))
        return self.hits


def _reply(answer_markdown: str, *citations: tuple[int, Hit | str, str]) -> str:
    return json.dumps(
        {
            "answer_markdown": answer_markdown,
            "citations": [
                {"marker": m, "chunk_id": str(h.chunk_id if isinstance(h, Hit) else h), "quote": q}
                for m, h, q in citations
            ],
        }
    )


async def test_no_hits_answers_not_found_without_calling_the_llm():
    llm = FakeLLM()
    retriever = FakeRetriever([])
    out = await answer(CTX, "How many?", None, retriever, llm)
    assert (out.found, out.answer_markdown, out.citations) == (False, NOT_FOUND, [])
    assert llm.calls == []


async def test_search_is_tenant_scoped_and_filtered_by_document_ids():
    docs = [uuid.uuid4()]
    retriever = FakeRetriever([])
    await answer(CTX, "How many?", docs, retriever, FakeLLM())
    assert retriever.calls == [(CTX, "How many?", docs)]


async def test_verified_answer_keeps_markers_and_copies_metadata_from_the_hit():
    llm = FakeLLM(
        _reply(
            "About 12,000 customers were affected [1].",
            (1, OUTAGE, "12,000 customers were affected"),
        )
    )
    out = await answer(CTX, "How many customers?", None, FakeRetriever([OUTAGE, SHELTER]), llm)
    assert out.found
    assert out.answer_markdown == "About 12,000 customers were affected [1]."
    [c] = out.citations
    assert (c.marker, c.chunk_id, c.document_id) == (1, OUTAGE.chunk_id, OUTAGE.document_id)
    assert (c.filename, c.page_number, c.section_heading) == ("ops_report.pdf", 4, "Impact")
    assert c.quote == "12,000 customers were affected"


async def test_prompt_labels_every_hit_and_carries_the_question():
    llm = FakeLLM(_reply("x", (1, OUTAGE, "12,000 customers were affected")))
    await answer(CTX, "How many customers?", None, FakeRetriever([OUTAGE, SHELTER]), llm)
    [(messages, schema)] = llm.calls
    assert messages[0]["role"] == "system"
    assert "verbatim" in messages[0]["content"]
    user = messages[1]["content"]
    assert f"[{OUTAGE.chunk_id} | ops_report.pdf | p.4 | Impact]" in user
    assert f"[{SHELTER.chunk_id} | ops_report.pdf | p.- | -]" in user
    assert SHELTER.text in user
    assert user.rstrip().endswith("How many customers?")
    assert set(schema["properties"]) == {"answer_markdown", "citations"}


async def test_unverified_citations_and_their_markers_are_removed():
    llm = FakeLLM(
        _reply(
            "12,000 customers [1] and 15,400 homes [2] and a fake chunk [3].",
            (1, OUTAGE, "12,000 customers were affected"),
            (2, OUTAGE, "15,400 homes were flooded"),  # quote not in the chunk
            (3, uuid.uuid4(), "Approximately 12,000 customers"),  # chunk never shown
        )
    )
    out = await answer(CTX, "q", None, FakeRetriever([OUTAGE]), llm)
    assert out.found
    assert [c.marker for c in out.citations] == [1]
    assert out.answer_markdown == "12,000 customers [1] and 15,400 homes and a fake chunk."


async def test_no_verified_citation_means_not_found():
    llm = FakeLLM(_reply("Made up [1].", (1, OUTAGE, "something the chunk never says")))
    out = await answer(CTX, "q", None, FakeRetriever([OUTAGE]), llm)
    assert (out.found, out.answer_markdown, out.citations) == (False, NOT_FOUND, [])


async def test_llm_declining_with_no_citations_means_not_found():
    llm = FakeLLM(_reply("The documents do not say."))
    out = await answer(CTX, "q", None, FakeRetriever([OUTAGE]), llm)
    assert (out.found, out.answer_markdown) == (False, NOT_FOUND)


async def test_search_failure_is_upstream_unavailable():
    class DownRetriever:
        async def search(self, ctx, query, document_ids=None, k=20):
            raise ConnectionError("qdrant down")

    with pytest.raises(UpstreamUnavailable):
        await answer(CTX, "q", None, DownRetriever(), FakeLLM())


async def test_llm_failure_propagates():
    llm = FakeLLM("not json", "still not json")
    with pytest.raises(LLMError):
        await answer(CTX, "q", None, FakeRetriever([OUTAGE]), llm)
