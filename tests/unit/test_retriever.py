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


def _text_hit(text: str) -> Hit:
    return _hits(1)[0].model_copy(update={"text": text})


def test_curate_drops_near_duplicates_keeping_the_first():
    from doc_intel.mapping.retriever import curate

    a = _text_hit("the quick brown fox jumps over the lazy dog today again")
    dup = _text_hit("The quick brown fox jumps over the lazy dog today again!")
    other = _text_hit("completely different words about power outages")
    assert curate([a, dup, other], 10_000, lambda t: 1) == ([a, other], 2)


def test_curate_stops_at_the_token_budget_but_keeps_the_first():
    from doc_intel.mapping.retriever import curate

    hits = [_text_hit(f"distinct text number {i} alpha{i}") for i in range(5)]
    assert curate(hits, 2 * 10, lambda t: 10) == (hits[:2], 20)
    assert curate(hits, 1, lambda t: 10) == (hits[:1], 10)
    assert curate([], 100, len) == ([], 0)


def test_curate_drops_zero_score_chunks_when_any_chunk_scored():
    from doc_intel.mapping.retriever import curate

    neighbor = _text_hit("heading chunk above the table").model_copy(update={"score": 0.0})
    a, b = _text_hit("first scored hit alpha"), _text_hit("second scored hit beta")
    assert curate([neighbor, a, b], 20, lambda t: 10) == ([a, b], 20)
    assert curate([neighbor, a, b], 1_000, lambda t: 10) == ([a, b], 20)  # budget left over
    # a reranker's negative score is a score, not an unscored neighbor
    low = _text_hit("reranked below zero gamma").model_copy(update={"score": -1.5})
    assert curate([neighbor, low], 1_000, lambda t: 10) == ([low], 10)


def test_curate_keeps_zero_score_chunks_when_none_scored():
    from doc_intel.mapping.retriever import curate

    zeros = [
        _text_hit(f"unscored chunk {i} delta{i}").model_copy(update={"score": 0.0})
        for i in range(3)
    ]
    assert curate(zeros, 20, lambda t: 10) == (zeros[:2], 20)  # never an empty context
    assert curate(zeros[:1], 5, lambda t: 10) == (zeros[:1], 10)  # the first is always kept


def test_curate_pinned_opening_chunks_claim_the_budget_first():
    from doc_intel.mapping.retriever import curate

    opening = _text_hit("cover page with the account holder").model_copy(update={"score": 0.0})
    a, b = _text_hit("first scored hit alpha"), _text_hit("second scored hit beta")
    assert curate([opening, a, b], 20, lambda t: 10, pinned=1) == ([opening, a], 20)
    assert curate([opening, a, b], 1_000, lambda t: 10, pinned=1) == ([opening, a, b], 30)


def test_select_context_logs_only_ids_scores_and_counts(monkeypatch):
    import structlog

    from doc_intel.mapping import retriever as mod

    monkeypatch.setattr(mod, "count_tokens", lambda text, model: 5)
    hits = [_text_hit("secret quote alpha"), _text_hit("secret quote beta gamma")]
    with structlog.testing.capture_logs() as logs:
        kept = mod.select_context(hits)
    assert kept == hits
    [event] = logs
    assert event["event"] == "retrieval" and event["context_tokens"] == 10 and event["hits"] == 2
    assert event["chunk_ids"] == [str(h.chunk_id) for h in hits]
    assert "secret" not in str(event)


async def test_opening_returns_the_first_chunks_of_each_document_in_document_order():
    d1, d2, d3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    store = NeighborStore({d1: 5, d2: 1})
    got = await Retriever(FakeEmbedder(), store).opening(CTX, [d2, d1, d3])
    assert [(h.document_id, h.chunk_index) for h in got] == [(d2, 0), (d1, 0), (d1, 1)]
    ctx, ids = store.retrieved[0]
    assert ctx == CTX and ids == [chunk_id(d, i) for d in (d2, d1, d3) for i in (0, 1)]
    assert await Retriever(FakeEmbedder(), NeighborStore({})).opening(CTX, []) == []


def test_select_context_cuts_by_an_explicit_token_budget_and_keeps_the_first():
    from doc_intel.mapping.retriever import select_context

    hits = _hits(5)
    assert select_context(hits, 1) == hits[:1]
    assert select_context(hits, 100_000) == hits


def _table_hit(text: str, rows) -> Hit:
    h = _hits(1)[0]
    return h.model_copy(update={"element_type": "table", "text": text, "rows": rows})


def test_format_hit_prefixes_row_ids_after_the_header_lines():
    from doc_intel.ingestion.schemas import TableRow

    text = "| k |\n| --- |\n| a |\n| b |"
    rows = [TableRow(id="P1-T2-R01", cells={"k": "a"}), TableRow(id="P1-T2-R02", cells={"k": "b"})]
    body = format_hit(_table_hit(text, rows)).split("\n", 1)[1]
    assert body == "| k |\n| --- |\n[P1-T2-R01] | a |\n[P1-T2-R02] | b |"


def test_format_hit_leaves_a_mismatched_or_text_hit_unchanged():
    from doc_intel.ingestion.schemas import TableRow

    text = "| k |\n| --- |\n| a |\n| b |"
    one = [TableRow(id="T1-R01", cells={"k": "a"})]
    assert format_hit(_table_hit(text, one)).split("\n", 1)[1] == text
    assert format_hit(_table_hit(text, None)).split("\n", 1)[1] == text
    assert format_hit(_hits(1)[0].model_copy(update={"block_ids": ["B01"]})).endswith("\nt0")
