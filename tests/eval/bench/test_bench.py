"""Regression bench (testing-strategy § 5): field accuracy, populated-but-wrong rate and row
alignment per case and overall, on synthetic real-shaped cases. Merges into report.json["bench"]."""

import json
import uuid
from collections import Counter
from pathlib import Path

import httpx
import pytest

from doc_intel.core.config import get_settings
from doc_intel.core.tenancy import TenantContext
from doc_intel.embeddings.ollama import OllamaQwen3Embedder
from doc_intel.llm.gateway import make_llm_client
from doc_intel.mapping.engine import map_fields
from doc_intel.mapping.retriever import Retriever
from doc_intel.mapping.verifier import normalize
from doc_intel.templates.schemas import IRField
from tests.eval.bench.score import rates, score_case
from tests.eval.test_mapping_eval import _quotes, _write_report, ingest

pytestmark = [pytest.mark.integration, pytest.mark.llm, pytest.mark.eval, pytest.mark.slow]

BENCH = Path(__file__).parent
GOLD = json.loads((BENCH / "gold.json").read_text())


async def test_bench_records_accuracy_wrong_rate_and_row_alignment(vector_store):
    settings, report, total, fabricated = get_settings(), {}, Counter(), 0
    async with httpx.AsyncClient() as http:
        embedder = OllamaQwen3Embedder(
            http,
            settings.embedding_base_url,
            settings.embedding_model,
            settings.embedding_dimensions,
        )
        retriever, llm = Retriever(embedder, vector_store), make_llm_client(settings, http)
        for path in sorted((BENCH / "cases").glob("*.json")):
            case = json.loads(path.read_text())
            ctx = TenantContext(f"bench_{uuid.uuid4().hex[:8]}", uuid.uuid4())
            doc_ids, texts = await ingest(ctx, case["sources"], embedder, vector_store)
            fields = [IRField.model_validate(raw) for raw in case["template"]["fields"]]
            results = await map_fields(ctx, fields, doc_ids, retriever, llm)
            fabricated += sum(
                normalize(e.quote) not in normalize(texts.get(e.document_id, ""))
                for r in results.values()
                for e in _quotes(r)
            )
            counts = score_case(case, GOLD[case["name"]], results)
            total += counts
            report[case["name"]] = rates(counts)

    summary = {"overall": rates(total), "cases": report, "fabricated_citations": fabricated}
    _write_report("bench", summary)
    print(json.dumps(summary, indent=2))

    # guardrail 2 is the only gate; the three bench metrics are recorded, not gated
    assert fabricated == 0
