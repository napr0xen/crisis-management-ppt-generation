import uuid

from doc_intel.core.tenancy import TenantContext
from doc_intel.embeddings.schemas import Hit
from doc_intel.mapping.retriever import TOP_N, Retriever, format_hit
from doc_intel.vector.qdrant_store import chunk_id
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


class NeighborStore:
    """Holds every chunk of some documents, keyed by deterministic id; records `retrieve` calls."""

    def __init__(self, docs: dict[uuid.UUID, int]):
        self.by_id = {}
        for doc, n in docs.items():
            for i in range(n):
                self.by_id[chunk_id(doc, i)] = _hit_at(doc, i)
        self.retrieved = []

    async def retrieve(self, ctx, ids):
        self.retrieved.append((ctx, list(ids)))
        return [self.by_id[i] for i in ids if i in self.by_id]


def _hit_at(doc, index, score=0.5):
    return Hit(
        chunk_id=chunk_id(doc, index),
        document_id=doc,
        filename="f.txt",
        page_number=None,
        section_heading=None,
        element_type="text",
        text=f"d{str(doc)[:4]}c{index}",
        score=score,
        chunk_index=index,
    )


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


async def test_search_top_n_defaults_to_eight_and_can_be_raised():
    hits = _hits(20)
    r = Retriever(FakeEmbedder(), FakeStore(hits))
    assert await r.search(CTX, "q") == hits[:8]
    assert await r.search(CTX, "q", top_n=12) == hits[:12]
    reranked = Retriever(FakeEmbedder(), FakeStore(hits), FakeReranker())
    assert len(await reranked.search(CTX, "q", top_n=12)) == 12


async def test_with_neighbors_orders_documents_by_rank_and_chunks_by_index():
    a, b = uuid.uuid4(), uuid.uuid4()
    store = NeighborStore({a: 10, b: 10})
    hits = [_hit_at(b, 5), _hit_at(a, 2), _hit_at(b, 4)]
    got = await Retriever(FakeEmbedder(), store).with_neighbors(CTX, hits, 7)
    assert [(h.document_id, h.chunk_index) for h in got] == [
        (b, 3), (b, 4), (b, 5), (b, 6), (a, 1), (a, 2), (a, 3)
    ]  # fmt: skip
    assert store.retrieved == [
        (CTX, [chunk_id(a, 1), chunk_id(b, 3), chunk_id(b, 6), chunk_id(a, 3)])
    ]


async def test_with_neighbors_previous_chunks_win_the_budget_over_next_chunks():
    a, b = uuid.uuid4(), uuid.uuid4()
    store = NeighborStore({a: 10, b: 10})
    hits = [_hit_at(a, 5), _hit_at(b, 5)]
    got = await Retriever(FakeEmbedder(), store).with_neighbors(CTX, hits, 4)
    # the rank-2 hit's previous chunk (its heading) beats the rank-1 hit's next chunk
    assert [(h.document_id, h.chunk_index) for h in got] == [(a, 4), (a, 5), (b, 4), (b, 5)]
    assert (a, 6) not in [(h.document_id, h.chunk_index) for h in got]


async def test_with_neighbors_skips_negative_index_and_present_chunks():
    a = uuid.uuid4()
    store = NeighborStore({a: 5})
    hits = [_hit_at(a, 0), _hit_at(a, 1)]
    got = await Retriever(FakeEmbedder(), store).with_neighbors(CTX, hits, 10)
    assert [h.chunk_index for h in got] == [0, 1, 2]
    assert store.retrieved == [(CTX, [chunk_id(a, 2)])]


async def test_with_neighbors_calls_nothing_when_budget_is_spent_or_nothing_to_fetch():
    a = uuid.uuid4()
    store = NeighborStore({a: 5})
    r = Retriever(FakeEmbedder(), store)
    hits = [_hit_at(a, 1), _hit_at(a, 2)]
    assert await r.with_neighbors(CTX, hits, 2) == hits
    assert await r.with_neighbors(CTX, hits * 3, 1) == hits[:1]  # cut to budget
    assert await r.with_neighbors(CTX, [], 5) == []
    assert store.retrieved == []
    legacy = _hits(2)
    assert await r.with_neighbors(CTX, legacy, 5) == legacy  # no chunk_index: no ids
    assert store.retrieved == []


async def test_with_neighbors_keeps_hits_without_chunk_index_after_the_indexed_ones():
    a = uuid.uuid4()
    store = NeighborStore({a: 5})
    legacy = _hits(1)[0]  # a point written before chunk_index was exposed
    hits = [legacy, _hit_at(a, 2)]
    got = await Retriever(FakeEmbedder(), store).with_neighbors(CTX, hits, 5)
    assert got[-1] == legacy and [h.chunk_index for h in got[:-1]] == [1, 2, 3]
