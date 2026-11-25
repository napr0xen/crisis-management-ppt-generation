"""`POST /v1/projects/{project_id}/query` against real Postgres + Qdrant, with FakeLLM."""

import json
import uuid

import pytest

from doc_intel.chat.qa import NOT_FOUND
from doc_intel.core.tenancy import TenantContext
from doc_intel.ingestion.schemas import ChunkPayload
from doc_intel.mapping.retriever import Retriever
from doc_intel.vector.qdrant_store import chunk_id
from tests.conftest import headers
from tests.fakes import FakeEmbedder, FakeLLM

pytestmark = pytest.mark.integration

TEXT = "Approximately 12,000 customers lost power after the substation fire on Main Street."
QUOTE = "12,000 customers lost power"
CITATION_KEYS = {
    "marker", "document_id", "chunk_id", "filename", "page_number", "section_heading", "quote"
}  # fmt: skip


async def seed(store, ctx: TenantContext) -> uuid.UUID:
    doc = uuid.uuid4()
    chunk = ChunkPayload(
        document_id=doc,
        filename="ops_report.pdf",
        page_number=4,
        section_heading="Impact",
        element_type="text",
        chunk_index=0,
        text=TEXT,
    )
    await store.upsert_chunks(ctx, [chunk], await FakeEmbedder().embed_documents([TEXT]))
    return doc


def reply(cited: uuid.UUID, quote: str = QUOTE) -> str:
    citation = {"marker": 1, "chunk_id": str(cited), "quote": quote}
    return json.dumps({"answer_markdown": "About 12,000 [1].", "citations": [citation]})


@pytest.fixture
def llm(client) -> FakeLLM:
    client.app.state.retriever = Retriever(FakeEmbedder(), client.app.state.vector_store)
    client.app.state.llm = FakeLLM()
    return client.app.state.llm


def ask(client, user: str, project: str, **body):
    body = {"question": "How many customers lost power?", "document_ids": None, **body}
    return client.post(f"/v1/projects/{project}/query", json=body, headers=headers(user))


async def test_grounded_answer_with_verified_citation(client, vector_store, llm, user_a, project):
    doc = await seed(vector_store, TenantContext(user_a, uuid.UUID(project)))
    llm.replies.append(reply(chunk_id(doc, 0)))
    r = ask(client, user_a, project)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"answer_markdown", "citations", "found"}
    assert body["found"] is True
    assert body["answer_markdown"] == "About 12,000 [1]."
    [c] = body["citations"]
    assert set(c) == CITATION_KEYS
    assert c == {
        "marker": 1,
        "document_id": str(doc),
        "chunk_id": str(chunk_id(doc, 0)),
        "filename": "ops_report.pdf",
        "page_number": 4,
        "section_heading": "Impact",
        "quote": QUOTE,
    }
    [(messages, _)] = llm.calls
    assert str(chunk_id(doc, 0)) in messages[1]["content"]


async def test_fabricated_quote_is_not_found(client, vector_store, llm, user_a, project):
    doc = await seed(vector_store, TenantContext(user_a, uuid.UUID(project)))
    llm.replies.append(reply(chunk_id(doc, 0), "15,400 customers lost power"))
    body = ask(client, user_a, project).json()
    assert body == {"answer_markdown": NOT_FOUND, "citations": [], "found": False}


async def test_citing_another_tenants_chunk_is_dropped(
    client, vector_store, llm, user_a, user_b, project
):
    await seed(vector_store, TenantContext(user_a, uuid.UUID(project)))
    # Same text and project id value, but another user's tenant: never retrievable by user_a.
    foreign = await seed(vector_store, TenantContext(user_b, uuid.UUID(project)))
    llm.replies.append(reply(chunk_id(foreign, 0)))
    body = ask(client, user_a, project).json()
    assert body["found"] is False
    [(messages, _)] = llm.calls
    assert str(chunk_id(foreign, 0)) not in messages[1]["content"]


async def test_empty_project_answers_not_found_without_the_llm(client, llm, user_a, project):
    body = ask(client, user_a, project).json()
    assert body == {"answer_markdown": NOT_FOUND, "citations": [], "found": False}
    assert llm.calls == []


async def test_document_filter_limits_the_search(client, vector_store, llm, user_a, project):
    await seed(vector_store, TenantContext(user_a, uuid.UUID(project)))
    body = ask(client, user_a, project, document_ids=[str(uuid.uuid4())]).json()
    assert body["found"] is False
    assert llm.calls == []


def test_another_users_project_is_404(client, llm, user_b, project):
    r = ask(client, user_b, project)
    assert r.status_code == 404
    assert llm.calls == []


@pytest.mark.parametrize("question", ["", "x" * 2001])
def test_question_length_is_validated(client, llm, user_a, project, question):
    assert ask(client, user_a, project, question=question).status_code == 422


async def test_invalid_llm_output_is_503(client, vector_store, llm, user_a, project):
    await seed(vector_store, TenantContext(user_a, uuid.UUID(project)))
    llm.replies += ["not json", "still not json"]
    r = ask(client, user_a, project)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "upstream_unavailable"


async def test_no_db_connection_is_held_while_the_llm_answers(
    client, vector_store, llm, user_a, project
):
    # The LLM call can take minutes; holding a pooled connection through it exhausts the pool.
    doc = await seed(vector_store, TenantContext(user_a, uuid.UUID(project)))
    llm.replies.append(reply(chunk_id(doc, 0)))
    pool = client.app.state.engine.pool
    checked_out = []
    complete = llm.complete

    async def spy(messages, json_schema=None, think=False):
        checked_out.append(pool.checkedout())
        return await complete(messages, json_schema, think)

    llm.complete = spy
    assert ask(client, user_a, project).json()["found"] is True
    assert checked_out == [0]
