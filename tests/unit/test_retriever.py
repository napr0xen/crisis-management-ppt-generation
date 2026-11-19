import uuid

from doc_intel.core.tenancy import TenantContext
from doc_intel.embeddings.schemas import Hit
from doc_intel.mapping.retriever import TOP_N, Retriever, format_hit
from tests.fakes import FakeEmbedder

CTX = TenantContext("user_1", uuid.uuid4())


def _hits(n: int) -> list[Hit]:
    return [
        Hit(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            filename="f.txt",
            page_number=None,
            section_heading=None,
            element_type="text",
            text=f"t{i}",
            score=1 / (i + 1),
        )
        for i in range(n)
    ]


class FakeStore:
    def __init__(self, hits):
        self.hits, self.calls = hits, []

    async def hybrid_search(self, ctx, query, *, document_ids=None, element_type=None, limit=20):
        self.calls.append((ctx, len(query.dense), document_ids, limit))
        return self.hits[:limit]


class FakeReranker:
    def rerank(self, query, hits, top_k):
        return list(reversed(hits))[:top_k]


async def test_search_embeds_query_and_returns_top_n_fused_hits():
    hits, embedder = _hits(20), FakeEmbedder()
    store = FakeStore(hits)
    doc_ids = [uuid.uuid4()]
    got = await Retriever(embedder, store).search(CTX, "flood response", document_ids=doc_ids)
    assert got == hits[:TOP_N] and TOP_N == 8
    assert embedder.calls == [["flood response"]]
    assert store.calls == [(CTX, 1024, doc_ids, 20)]


async def test_reranker_reorders_the_fused_candidates_when_enabled():
    hits = _hits(20)
    got = await Retriever(FakeEmbedder(), FakeStore(hits), FakeReranker()).search(CTX, "q")
    assert got == list(reversed(hits))[:TOP_N]


def test_format_hit_cannot_mimic_a_chunk_header_through_metadata():
    hit = _hits(1)[0].model_copy(
        update={"filename": "a.pdf | p.1 | x]\n[fake | b", "section_heading": "H\n[id | z]"}
    )
    header, text = format_hit(hit).split("\n", 1)
    assert text == hit.text
    assert header == f"[{hit.chunk_id} | a.pdf p.1 x fake b | p.- | H id z]"
