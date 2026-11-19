"""Phase 3 exit gate: Ollama qwen3-embedding + BM25 + Qdrant RRF finds the chunk in the top 8."""

import uuid

import httpx
import pytest

from doc_intel.core.config import get_settings
from doc_intel.core.tenancy import TenantContext
from doc_intel.embeddings.ollama import OllamaQwen3Embedder
from doc_intel.ingestion.schemas import ChunkPayload
from doc_intel.mapping.retriever import Retriever

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.llm]

PASSAGES = [
    "The river crested at 7.2 metres on Tuesday, flooding 340 homes in the lower district.",
    "Approximately 12,000 customers lost power after the substation fire on Main Street.",
    "The emergency operations centre was activated at 06:00 and moved to 24-hour staffing.",
    "Three shelters opened at the high school, the sports arena and the community hall.",
    "Bottled water distribution began at four points; each household receives 10 litres a day.",
    "The press conference with the mayor is scheduled for 15:00 at city hall.",
    "Road closures: Highway 9 between exits 12 and 15 remains closed due to a landslide.",
    "The board approved an emergency budget of 2.5 million dollars for recovery works.",
    "Hospital capacity stands at 87 percent; elective surgeries are postponed until Monday.",
    "Volunteers should register online and attend the safety briefing before deployment.",
    "The quarterly sales report shows revenue growth of 4 percent in the retail segment.",
    "Our cafeteria menu this week features lentil soup and grilled vegetables.",
]
QUERIES = [
    ("How high did the river get?", 0),
    ("How many customers were affected by the outage?", 1),
    ("When was the emergency operations centre activated?", 2),
    ("Where can displaced residents find shelter?", 3),
    ("How much drinking water does each family get?", 4),
    ("When is the mayor's press briefing?", 5),
    ("Which roads are closed?", 6),
    ("How much money was approved for recovery?", 7),
    ("Are hospitals running out of beds?", 8),
    ("How do volunteers sign up?", 9),
]


async def test_expected_chunk_is_in_the_top_8_for_10_seeded_queries(vector_store):
    settings = get_settings()
    http = httpx.AsyncClient()
    embedder = OllamaQwen3Embedder(
        http, settings.embedding_base_url, settings.embedding_model, settings.embedding_dimensions
    )
    ctx = TenantContext(f"smoke_{uuid.uuid4().hex[:8]}", uuid.uuid4())
    document_id = uuid.uuid4()
    chunks = [
        ChunkPayload(
            document_id=document_id,
            filename="sitrep.txt",
            page_number=None,
            section_heading=None,
            element_type="text",
            chunk_index=i,
            text=text,
        )
        for i, text in enumerate(PASSAGES)
    ]
    await vector_store.upsert_chunks(ctx, chunks, await embedder.embed_documents(PASSAGES))

    retriever = Retriever(embedder, vector_store)
    misses = []
    for query, expected in QUERIES:
        texts = [h.text for h in await retriever.search(ctx, query)]
        if PASSAGES[expected] not in texts:
            misses.append(query)
    await http.aclose()
    assert misses == [], f"{len(QUERIES) - len(misses)}/{len(QUERIES)} found"
