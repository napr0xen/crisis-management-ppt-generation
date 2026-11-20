"""Phase 4 exit gate: grounded Q&A against the real models (testing-strategy § 5).

Hard: 0 fabricated citations (re-checked here independently of the verifier) and "not found"
for every unanswerable question. Merges its results into tests/eval/report.json["qa"] (git-ignored).
"""

import json
import statistics
import time
import uuid
from pathlib import Path

import httpx
import pytest

from doc_intel.chat import qa
from doc_intel.core.config import get_settings
from doc_intel.core.tenancy import TenantContext
from doc_intel.embeddings.ollama import OllamaQwen3Embedder
from doc_intel.ingestion.schemas import ChunkPayload
from doc_intel.llm.gateway import make_llm_client
from doc_intel.mapping.retriever import Retriever
from tests.integration.test_retrieval_smoke import PASSAGES

pytestmark = [pytest.mark.integration, pytest.mark.llm, pytest.mark.eval, pytest.mark.slow]

DATASET = Path(__file__).parents[1] / "fixtures" / "eval" / "qa.jsonl"
REPORT = Path(__file__).parent / "report.json"
MIN_ANSWERED = 0.8  # answerable questions that must come back with a verified citation


def _squash(s: str) -> str:
    return " ".join(s.split()).casefold()


async def test_qa_eval_grounded_answers_and_refusals(vector_store):
    settings = get_settings()
    cases = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
    ctx = TenantContext(f"eval_{uuid.uuid4().hex[:8]}", uuid.uuid4())
    chunks = [
        ChunkPayload(
            document_id=uuid.uuid4(),
            filename=f"sitrep_{i:02d}.txt",
            page_number=None,
            section_heading=None,
            element_type="text",
            chunk_index=0,
            text=text,
        )
        for i, text in enumerate(PASSAGES)
    ]
    async with httpx.AsyncClient() as http:
        embedder = OllamaQwen3Embedder(
            http,
            settings.embedding_base_url,
            settings.embedding_model,
            settings.embedding_dimensions,
        )
        await vector_store.upsert_chunks(ctx, chunks, await embedder.embed_documents(PASSAGES))
        texts = {c.document_id: c.text for c in chunks}
        retriever, llm = Retriever(embedder, vector_store), make_llm_client(settings, http)

        rows = []
        for case in cases:
            started = time.perf_counter()
            out = await qa.answer(ctx, case["question"], None, retriever, llm)
            latency = time.perf_counter() - started
            expected_files = {s["filename"] for s in case["expected_sources"]}
            top8 = await retriever.search(ctx, case["question"])
            fabricated = [
                c.marker
                for c in out.citations
                if _squash(c.quote) not in _squash(texts.get(c.document_id, ""))
            ]
            expected = case["expected_answer_contains"]
            rows.append(
                {
                    "question": case["question"],
                    "answerable": case["answerable"],
                    "found": out.found,
                    "citations": len(out.citations),
                    "fabricated_citations": len(fabricated),
                    "latency_s": round(latency, 2),
                    "retrieved_expected": any(h.filename in expected_files for h in top8),
                    "think_leaked": "<think>" in out.answer_markdown,
                    "contains_expected": bool(expected)
                    and _squash(expected) in _squash(out.answer_markdown),
                    "source_hit": any(c.filename in expected_files for c in out.citations),
                }
            )

    answerable = [r for r in rows if r["answerable"]]
    unanswerable = [r for r in rows if not r["answerable"]]
    fabricated = sum(r["fabricated_citations"] for r in rows)
    total_citations = sum(r["citations"] for r in rows)
    latencies = [r["latency_s"] for r in rows]
    summary = {
        "model": settings.llm_model,
        "fabricated_citations": fabricated,
        "citation_verification_rate": 1 - fabricated / total_citations if total_citations else 1.0,
        "source_recall_at_8": sum(r["retrieved_expected"] for r in answerable) / len(answerable),
        "latency_p50_s": statistics.median(latencies),
        "latency_max_s": max(latencies),
        "unanswerable_not_found": sum(not r["found"] for r in unanswerable) / len(unanswerable),
        "answered": sum(r["found"] for r in answerable) / len(answerable),
        "answer_contains_expected": sum(r["contains_expected"] for r in answerable)
        / len(answerable),
        "cited_expected_source": sum(r["source_hit"] for r in answerable) / len(answerable),
        "rows": rows,
    }
    report = json.loads(REPORT.read_text()) if REPORT.exists() else {}
    REPORT.write_text(json.dumps({**report, "qa": summary}, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2))

    assert summary["fabricated_citations"] == 0
    assert summary["citation_verification_rate"] == 1.0
    assert not any(r["think_leaked"] for r in rows)
    assert [r["question"] for r in unanswerable if r["found"]] == []
    assert summary["answered"] >= MIN_ANSWERED
