"""`POST /v1/pipe/chat` with `library_template_id` / `template_document_id` skips routing."""

import uuid

import pytest

from doc_intel.api.v1 import pipe
from doc_intel.chat.router import Intent
from doc_intel.db import repo
from tests.integration.test_pipe_api import (
    TXT,
    events,
    force_doc,
    parse_sse,
    pipe_headers,
    send_file,
    set_intent,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("empty_queue", "clean_library")]


def chat(client, user, **extra):
    body = {"messages": [{"role": "user", "content": "go"}], **extra}
    r = client.post("/v1/pipe/chat", json=body, headers=pipe_headers(user))
    return r, parse_sse(r.text) if r.status_code == 200 else None


@pytest.fixture
def no_router(monkeypatch):
    """Fails the turn with a distinctive code if the router LLM is called."""
    calls = []

    async def route(llm, messages, documents):
        calls.append(1)
        raise AssertionError("the router must be skipped")

    monkeypatch.setattr(pipe, "route", route)
    return calls


def fake_flow(monkeypatch, name, seen):
    async def flow(request, ctx, target, deltas, out):
        seen.append(target)
        deltas.append("mapped")
        return
        yield  # an async generator, like the real flows

    monkeypatch.setattr(pipe, name, flow)


def test_both_ids_are_422(client, user_a):
    r, _ = chat(
        client,
        user_a,
        library_template_id=str(uuid.uuid4()),
        template_document_id=str(uuid.uuid4()),
    )
    assert r.status_code == 422


@pytest.mark.parametrize("field", ["library_template_id", "template_document_id"])
def test_not_a_uuid_is_422(client, user_a, field):
    assert chat(client, user_a, **{field: "nope"})[0].status_code == 422


@pytest.mark.parametrize("field", ["library_template_id", "template_document_id"])
def test_unknown_id_is_a_not_found_error_event(client, user_a, no_router, field):
    _, frames = chat(client, user_a, **{field: str(uuid.uuid4())})
    assert events(frames)[-2:] == ["error", "done"]
    assert frames[-2][1]["code"] == "not_found"
    assert no_router == []


async def test_library_id_runs_that_template_without_routing(
    client, session, user_a, no_router, monkeypatch
):
    lib = await repo.create_library_template(
        session,
        name="L",
        description="",
        category=None,
        format="pptx",
        filename="l.pptx",
        sha256="c" * 64,
        size_bytes=1,
        storage_key="library/z/original.pptx",
        anchor_map={"anchors": []},
        anchor_count=0,
        ir={"fields": []},
    )
    await session.commit()
    seen = []
    fake_flow(monkeypatch, "_map_library", seen)
    _, frames = chat(client, user_a, library_template_id=str(lib.id))
    assert [t.id for t in seen] == [lib.id] and no_router == []
    assert ("delta", {"content": "mapped"}) in frames and events(frames)[-1] == "done"


async def test_template_document_id_runs_that_document_without_routing(
    client, session, user_a, no_router, monkeypatch
):
    doc_id = uuid.UUID(send_file(client, user_a, name="notes.txt", data=TXT).json()["document_id"])
    await force_doc(session, user_a, doc_id, status="indexed")
    seen = []
    fake_flow(monkeypatch, "_map_template", seen)
    _, frames = chat(client, user_a, template_document_id=str(doc_id))
    assert seen == [doc_id] and no_router == []
    assert events(frames)[-1] == "done"


async def test_another_tenants_document_is_not_found(client, session, user_a, user_b, no_router):
    doc_id = uuid.UUID(send_file(client, user_b, name="notes.txt", data=TXT).json()["document_id"])
    await force_doc(session, user_b, doc_id, status="indexed")
    _, frames = chat(client, user_a, template_document_id=str(doc_id))
    assert frames[-2][1]["code"] == "not_found" and no_router == []


def test_without_the_ids_the_router_still_runs(client, user_a, monkeypatch):
    set_intent(monkeypatch, Intent(intent="chitchat"))
    _, frames = chat(client, user_a)
    assert events(frames)[-1] == "done"
