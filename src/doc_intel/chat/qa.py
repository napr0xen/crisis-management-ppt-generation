"""Grounded Q&A (system-design § 9.5): retrieve -> one JSON LLM call -> verify -> answer."""

import re
import uuid

from pydantic import BaseModel

from doc_intel.core.errors import UpstreamUnavailable
from doc_intel.core.tenancy import TenantContext
from doc_intel.llm.gateway import LLMClient, complete_json
from doc_intel.llm.prompts import load_prompt
from doc_intel.mapping.retriever import Retriever, format_hit
from doc_intel.mapping.verifier import verify_quote

NOT_FOUND = "I could not find this in the uploaded documents."
_MARKER = re.compile(r"\s?\[(\d+)\]")


class _LLMCitation(BaseModel):
    marker: int
    chunk_id: str
    quote: str


class _LLMAnswer(BaseModel):
    answer_markdown: str
    citations: list[_LLMCitation]


class Citation(BaseModel):
    marker: int
    document_id: uuid.UUID
    chunk_id: uuid.UUID
    filename: str
    page_number: int | None
    section_heading: str | None
    quote: str


class Answer(BaseModel):
    answer_markdown: str
    citations: list[Citation]
    found: bool


async def answer(
    ctx: TenantContext,
    question: str,
    document_ids: list[uuid.UUID] | None,
    retriever: Retriever,
    llm: LLMClient,
) -> Answer:
    not_found = Answer(answer_markdown=NOT_FOUND, citations=[], found=False)
    try:
        hits = await retriever.search(ctx, question, document_ids)
    except Exception as exc:  # Ollama embedder or Qdrant down
        raise UpstreamUnavailable() from exc
    if not hits:
        return not_found
    # shortcut: no token budget, the context is bounded by TOP_N hits x CHUNK_MAX_TOKENS within
    # LLM_NUM_CTX; add one if either grows.
    sources = "\n\n".join(format_hit(h) for h in hits)
    messages = [
        {"role": "system", "content": load_prompt("qa")},
        {"role": "user", "content": f"Chunks:\n\n{sources}\n\nQuestion: {question}"},
    ]
    reply = await complete_json(llm, messages, _LLMAnswer)
    citations = []
    for c in reply.citations:
        hit = verify_quote(hits, c.chunk_id, c.quote)
        if hit is not None:  # metadata from the hit payload, never from the LLM
            citations.append(
                Citation(
                    marker=c.marker,
                    document_id=hit.document_id,
                    chunk_id=hit.chunk_id,
                    filename=hit.filename,
                    page_number=hit.page_number,
                    section_heading=hit.section_heading,
                    quote=c.quote,
                )
            )
    if not citations:
        return not_found
    kept = {c.marker for c in citations}
    # shortcut: a sentence whose only marker failed verification stays in the answer uncited,
    # drop such sentences if the eval shows unsupported claims surviving.
    text = _MARKER.sub(lambda m: m[0] if int(m[1]) in kept else "", reply.answer_markdown)
    return Answer(answer_markdown=text, citations=citations, found=True)
