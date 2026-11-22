"""Phase 6 exit gate: field mapping against the real models (testing-strategy § 5).

Hard: 0 fabricated evidence quotes (re-checked here against the full source text, independently of
the verifier's chunk check), every seeded conflict detected, absent fields returned `missing`.
Field accuracy is tracked, not gated. Merges its results into tests/eval/report.json["mapping"].
"""

import json
import statistics
import time
import uuid
from pathlib import Path

import httpx
import pytest

from doc_intel.core.config import get_settings
from doc_intel.core.tenancy import TenantContext
from doc_intel.embeddings.ollama import OllamaQwen3Embedder
from doc_intel.ingestion.schemas import ChunkPayload
from doc_intel.llm.gateway import make_llm_client
from doc_intel.mapping.engine import map_fields
from doc_intel.mapping.retriever import Retriever
from doc_intel.mapping.schemas import FieldMappingResult
from doc_intel.mapping.verifier import normalize
from doc_intel.templates.schemas import IRField

pytestmark = [pytest.mark.integration, pytest.mark.llm, pytest.mark.eval, pytest.mark.slow]

DATASET = Path(__file__).parents[1] / "fixtures" / "eval" / "mapping.jsonl"
REPORT = Path(__file__).parent / "report.json"
MIN_ABSENT_MISSING = 0.95


def _write_report(key: str, section: dict) -> None:
    report = json.loads(REPORT.read_text()) if REPORT.exists() else {}
    REPORT.write_text(json.dumps({**report, key: section}, indent=2))


def _quotes(result: FieldMappingResult) -> list:
    candidates = result.conflict_details.candidates if result.conflict_details else []
    return [*result.evidence, *(e for c in candidates for e in c.evidence)]


def _fits(field: IRField, result: FieldMappingResult) -> bool | None:
    """Whether a populated/partial list value fits the field's max_items / max_chars."""
    c, value = field.constraints, result.value
    if result.status not in ("populated", "partial") or not isinstance(value, list):
        return None
    if c.max_items is None and c.max_chars is None:
        return None
    too_many = c.max_items is not None and len(value) > c.max_items
    too_long = c.max_chars is not None and len("\n".join(map(str, value))) > c.max_chars
    return not (too_many or too_long)


def _row_matches(actual: list, expected: list) -> bool:
    return len(actual) == len(expected) and all(
        normalize(str(e)) in normalize(str(a)) for a, e in zip(actual, expected, strict=True)
    )


def _matches(actual, expected) -> bool:
    if isinstance(expected, list) and expected and isinstance(expected[0], list):
        # a table: same row count, each expected row matches its own actual row (row order
        # across documents is not defined)
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        left = [r for r in actual if isinstance(r, list)]
        for row in expected:
            hit = next((a for a in left if _row_matches(a, row)), None)
            if hit is None:
                return False
            left.remove(hit)
        return True
    if isinstance(expected, list):
        joined = normalize(" ".join(actual)) if isinstance(actual, list) else ""
        return all(normalize(item) in joined for item in expected)
    if isinstance(expected, str):
        return isinstance(actual, str) and normalize(expected) in normalize(actual)
    return isinstance(actual, int | float) and float(actual) == float(expected)


async def test_mapping_eval_no_fabrication_conflicts_and_absent_fields(vector_store):
    settings = get_settings()
    cases = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
    rows = []
    async with httpx.AsyncClient() as http:
        embedder = OllamaQwen3Embedder(
            http,
            settings.embedding_base_url,
            settings.embedding_model,
            settings.embedding_dimensions,
        )
        retriever, llm = Retriever(embedder, vector_store), make_llm_client(settings, http)
        for case in cases:
            ctx = TenantContext(f"eval_{uuid.uuid4().hex[:8]}", uuid.uuid4())
            doc_ids, texts = [], {}
            for source in case["sources"]:
                doc_id = uuid.uuid4()
                doc_ids.append(doc_id)
                texts[doc_id] = source["text"]
                paragraphs = [p for p in source["text"].split("\n\n") if p.strip()]
                chunks = [
                    ChunkPayload(
                        document_id=doc_id,
                        filename=source["filename"],
                        page_number=None,
                        section_heading=None,
                        element_type="text",
                        chunk_index=i,
                        text=p,
                    )
                    for i, p in enumerate(paragraphs)
                ]
                await vector_store.upsert_chunks(
                    ctx, chunks, await embedder.embed_documents(paragraphs)
                )

            fields = [IRField.model_validate(raw) for raw in case["template"]["fields"]]
            started = time.perf_counter()
            results = await map_fields(ctx, fields, doc_ids, retriever, llm)
            latency = (time.perf_counter() - started) / len(fields)
            for field in fields:
                result = results[field.field_id]
                expected = case["expected"][field.field_id]
                fabricated = [
                    e.quote
                    for e in _quotes(result)
                    if normalize(e.quote) not in normalize(texts.get(e.document_id, ""))
                ]
                rows.append(
                    {
                        "template": case["name"],
                        "field_id": field.field_id,
                        "expected_status": expected["status"],
                        "status": result.status,
                        "confidence": result.confidence,
                        "latency_s": round(latency, 2),
                        "missing_reason": result.missing_reason,
                        "evidence_count": len(_quotes(result)),
                        "synthesized": any(e.item is not None for e in _quotes(result)),
                        "fits": _fits(field, result),
                        "fabricated_citations": len(fabricated),
                        "value_correct": (
                            _matches(result.value, expected["value"])
                            if expected["status"] == "populated"
                            and result.status in ("populated", "partial")
                            else None
                        ),
                    }
                )

    absent = [r for r in rows if r["expected_status"] == "missing"]
    conflicts = [r for r in rows if r["expected_status"] == "conflict"]
    present = [r for r in rows if r["expected_status"] == "populated"]
    total_evidence = sum(r["evidence_count"] for r in rows)
    fabricated = sum(r["fabricated_citations"] for r in rows)
    answered = [r for r in rows if r["status"] in ("populated", "partial")]
    calibration = {}
    for tier in sorted({r["confidence"] for r in answered}, reverse=True):
        tier_rows = [r for r in answered if r["confidence"] == tier]
        calibration[str(tier)] = {
            "count": len(tier_rows),
            "fraction_correct": sum(bool(r["value_correct"]) for r in tier_rows) / len(tier_rows),
        }
    latencies = [r["latency_s"] for r in rows]
    summary = {
        "model": settings.llm_model,
        "fabricated_citations": fabricated,
        "citation_verification_rate": 1 - fabricated / total_evidence if total_evidence else 1.0,
        "seeded_conflicts_detected": sum(r["status"] == "conflict" for r in conflicts)
        / len(conflicts),
        "absent_missing": sum(r["status"] == "missing" for r in absent) / len(absent),
        "field_accuracy": sum(
            bool(r["value_correct"]) and r["status"] == "populated" for r in present
        )
        / len(present),
        "synthesized": sum(r["synthesized"] for r in rows),
        "length_violations": sum(r["fits"] is False for r in rows),
        "calibration": calibration,
        "latency_p50_s": statistics.median(latencies),
        "latency_max_s": max(latencies),
        "rows": rows,
    }
    _write_report("mapping", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2))

    assert summary["fabricated_citations"] == 0
    assert summary["seeded_conflicts_detected"] == 1.0
    assert summary["absent_missing"] >= MIN_ABSENT_MISSING
