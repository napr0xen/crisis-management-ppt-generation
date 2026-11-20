"""Phase 12: NFR-3 performance and memory on the real stack (requirements NFR-3, architecture § 6).

Targets are tracked, not gated: the test only asserts the run completed and records pass/fail
booleans in tests/eval/report.json["perf"] (git-ignored).
Run: `pytest -m perf -s tests/eval/test_perf.py`.
"""

import asyncio
import json
import resource
import statistics
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

from doc_intel.chat import qa
from doc_intel.core.config import get_settings
from doc_intel.core.tenancy import TenantContext
from doc_intel.embeddings.ollama import OllamaQwen3Embedder
from doc_intel.ingestion.pipeline import parse_and_chunk
from doc_intel.ingestion.schemas import ChunkPayload
from doc_intel.llm.gateway import make_llm_client
from doc_intel.mapping.engine import map_field
from doc_intel.mapping.retriever import Retriever
from doc_intel.templates.schemas import IRField
from tests.eval.test_qa_eval import REPORT
from tests.fixtures.sources import _lines, _pdf
from tests.integration.test_retrieval_smoke import PASSAGES

pytestmark = [pytest.mark.integration, pytest.mark.llm, pytest.mark.perf, pytest.mark.slow]

MAPPING = Path(__file__).parents[1] / "fixtures" / "eval" / "mapping.jsonl"
TARGET_INGEST_S, TARGET_QA_S, TARGET_MAP_S = 180, 20, 600
QA_QUESTIONS = [
    "How high did the river get?",
    "How many customers were affected by the outage?",
    "When was the emergency operations centre activated?",
    "Where can displaced residents find shelter?",
    "How much money did the board approve for recovery?",
]


def _pdf_50_pages() -> bytes:
    """50 text pages of about 400 words each, with distinct sentences so chunks differ."""
    pages = []
    for p in range(50):
        lines = [
            (
                12,
                740 - 14 * i,
                f"Page {p + 1} line {i + 1}: crews logged shelter, road, water and power "
                f"status for district {p % 7} during shift {i}.",
            )
            for i in range(45)
        ]
        pages.append(_lines(lines))
    return _pdf(pages)


def _cmd(*args: str) -> str:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no user input
            args, capture_output=True, text=True, timeout=30, check=False
        )
        return out.stdout.strip() or out.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable: {type(exc).__name__}"


def _ram() -> dict:
    return {
        "ollama_ps": _cmd("ollama", "ps"),
        "docker_stats": _cmd(
            "docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}"
        ),
    }


def _peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2  # bytes on macOS


async def _index(vector_store, embedder, ctx, filename: str, texts: list[str]):
    doc_id = uuid.uuid4()
    chunks = [
        ChunkPayload(
            document_id=doc_id,
            filename=filename,
            page_number=None,
            section_heading=None,
            element_type="text",
            chunk_index=i,
            text=t,
        )
        for i, t in enumerate(texts)
    ]
    await vector_store.upsert_chunks(ctx, chunks, await embedder.embed_documents(texts))
    return doc_id


async def test_nfr3_performance(vector_store):
    settings = get_settings()
    ram_before = _ram()
    perf: dict = {"model": settings.llm_model}
    async with httpx.AsyncClient() as http:
        embedder = OllamaQwen3Embedder(
            http,
            settings.embedding_base_url,
            settings.embedding_model,
            settings.embedding_dimensions,
        )
        retriever, llm = Retriever(embedder, vector_store), make_llm_client(settings, http)

        # (a) 50-page text PDF through parse_and_chunk + embed + upsert (the ingest job's path)
        ctx = TenantContext(f"perf_{uuid.uuid4().hex[:8]}", uuid.uuid4())
        data = _pdf_50_pages()
        started = time.perf_counter()
        parsed, chunks = await asyncio.to_thread(
            parse_and_chunk,
            data,
            "pdf",
            document_id=uuid.uuid4(),
            filename="perf_50p.pdf",
            max_tokens=settings.chunk_max_tokens,
            tokenizer_model=settings.chunk_tokenizer,
        )
        for start in range(0, len(chunks), 256):
            batch = chunks[start : start + 256]
            await vector_store.upsert_chunks(
                ctx, batch, await embedder.embed_documents([c.text for c in batch])
            )
        perf["ingest_50p_s"] = round(time.perf_counter() - started, 1)
        perf["ingest_50p_pages"] = parsed.page_count
        perf["ingest_50p_chunks"] = len(chunks)

        # (b) Q&A latency
        qa_ctx = TenantContext(f"perf_{uuid.uuid4().hex[:8]}", uuid.uuid4())
        await _index(vector_store, embedder, qa_ctx, "sitrep.txt", PASSAGES)
        latencies = []
        for question in QA_QUESTIONS:
            started = time.perf_counter()
            await qa.answer(qa_ctx, question, None, retriever, llm)
            latencies.append(time.perf_counter() - started)
        perf["qa_latencies_s"] = [round(x, 1) for x in latencies]
        perf["qa_p50_s"] = round(statistics.median(latencies), 1)
        perf["qa_max_s"] = round(max(latencies), 1)

        # (c) the first 30 eval-template fields mapped over the first 5 eval sources, pooled in
        # one project (map_field retrieves across all of them).
        cases = [json.loads(line) for line in MAPPING.read_text().splitlines() if line.strip()]
        fields = [IRField.model_validate(f) for c in cases for f in c["template"]["fields"]][:30]
        map_ctx = TenantContext(f"perf_{uuid.uuid4().hex[:8]}", uuid.uuid4())
        doc_ids = []
        for source in [s for c in cases for s in c["sources"]][:5]:
            paragraphs = [p for p in source["text"].split("\n\n") if p.strip()]
            doc_ids.append(
                await _index(vector_store, embedder, map_ctx, source["filename"], paragraphs)
            )
        started = time.perf_counter()
        statuses = []
        for field in fields:
            statuses.append((await map_field(map_ctx, field, doc_ids, retriever, llm)).status)
        perf["map_30x5_s"] = round(time.perf_counter() - started, 1)
        perf["map_fields"] = len(statuses)
        perf["map_sources"] = len(doc_ids)

    perf["peak_rss_mb"] = round(_peak_rss_mb(), 1)
    perf["ram_before"], perf["ram_after"] = ram_before, _ram()
    perf["targets_met"] = {
        "ingest_50p": perf["ingest_50p_s"] <= TARGET_INGEST_S,
        "qa_max": perf["qa_max_s"] <= TARGET_QA_S,
        "map_30x5": perf["map_30x5_s"] <= TARGET_MAP_S,
    }
    report = json.loads(REPORT.read_text()) if REPORT.exists() else {}
    REPORT.write_text(json.dumps({**report, "perf": perf}, indent=2))
    print(json.dumps(perf, indent=2))

    assert perf["ingest_50p_pages"] == 50
    assert len(statuses) == 30
