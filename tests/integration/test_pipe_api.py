"""`/v1/pipe/files` and `/v1/pipe/chat` (api-specification § Open WebUI Pipe, system-design § 9)."""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from doc_intel.api.v1 import pipe
from doc_intel.chat.qa import Answer, Citation
from doc_intel.chat.router import Intent
from doc_intel.core.config import get_settings
from doc_intel.core.tenancy import TenantContext
from doc_intel.db.models import Document, Job, Project
from doc_intel.db.session import make_engine
from doc_intel.ingestion.schemas import ChunkPayload
from doc_intel.jobs.handlers import make_failure_hooks, make_handlers
from doc_intel.jobs.worker import run_one
from doc_intel.llm.gateway import LLMError
from doc_intel.mapping.retriever import Retriever
from doc_intel.storage.local import LocalStorage
from doc_intel.vector.qdrant_store import chunk_id
from tests.conftest import headers
from tests.fakes import FakeEmbedder, FakeLLM
from tests.fixtures import templates
from tests.integration.test_generation_e2e import CHUNK, QUOTE, draft_reply
from tests.integration.test_mapping_e2e import candidate, extraction

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("empty_queue")]

PDF = b"%PDF-1.7 quarterly report"
TXT = b"Ingest me: the depot reopens on Monday."


def pipe_headers(user: str, chat: str = "chat-1") -> dict[str, str]:
    return {**headers(user), "X-OWUI-Chat-Id": chat}


def send_file(client, user, chat="chat-1", file_id="f1", name="report.pdf", data=PDF):
    return client.post(
        "/v1/pipe/files",
        files={"file": (name, data, "application/octet-stream")},
        data={"openwebui_file_id": file_id},
        headers=pipe_headers(user, chat),
    )


def parse_sse(text: str) -> list[tuple[str, dict]]:
    frames = []
    for block in text.strip().split("\n\n"):
        if block.startswith(":"):  # SSE comment (keep-alive)
            continue
        event, data = block.split("\n")
        assert event.startswith("event: ") and data.startswith("data: ")
        frames.append((event[7:], json.loads(data[6:])))
    return frames


def chat(client, user, content="hi", files=(), chat_id="chat-1"):
    body = {
        "messages": [{"role": "user", "content": content}],
        "files": [{"openwebui_file_id": i, "filename": n} for i, n in files],
    }
    r = client.post("/v1/pipe/chat", json=body, headers=pipe_headers(user, chat_id))
    return r, parse_sse(r.text) if r.status_code == 200 else None


def events(frames) -> list[str]:
    return [e for e, _ in frames]


@pytest.fixture
def fast_poll(monkeypatch):
    monkeypatch.setattr(pipe, "POLL_INTERVAL_S", 0.02)


def set_intent(monkeypatch, intent: Intent | Exception):
    async def route(llm, messages, documents):
        if isinstance(intent, Exception):
            raise intent
        return intent

    monkeypatch.setattr(pipe, "route", route)


async def force_doc(session, user, doc_id, **values):
    await session.execute(update(Document).where(Document.id == doc_id).values(**values))
    await session.execute(delete(Job).where(Job.user_id == user))
    await session.commit()


# ---------------------------------------------------------------- POST /v1/pipe/files


def test_file_upload_creates_project_and_document(client, session, user_a):
    r = send_file(client, user_a)
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"project_id", "document_id", "job_id", "deduplicated"}
    assert body["deduplicated"] is False and body["job_id"]
    got = client.get(
        f"/v1/projects/{body['project_id']}/documents/{body['document_id']}",
        headers=headers(user_a),
    )
    assert got.status_code == 200


async def test_new_document_records_the_owui_file_id(client, session, user_a):
    doc_id = send_file(client, user_a, file_id="owui-9").json()["document_id"]
    doc = await session.get(Document, uuid.UUID(doc_id))
    assert doc.openwebui_file_ids == ["owui-9"]


async def test_project_is_created_once_per_chat_and_reused(client, session, user_a):
    first = send_file(client, user_a, file_id="f1").json()
    second = send_file(client, user_a, file_id="f2", name="b.txt", data=TXT).json()
    other_chat = send_file(client, user_a, chat="chat-2", file_id="f3", data=b"%PDF-1.4 other")
    assert first["project_id"] == second["project_id"]
    assert other_chat.json()["project_id"] != first["project_id"]
    rows = (await session.scalars(select(Project).where(Project.user_id == user_a))).all()
    assert len(rows) == 2
    assert {p.openwebui_chat_id for p in rows} == {"chat-1", "chat-2"}


async def test_same_bytes_new_file_id_is_deduplicated_and_id_appended(client, session, user_a):
    first = send_file(client, user_a, file_id="f1").json()
    again = send_file(client, user_a, file_id="f2").json()
    resend = send_file(client, user_a, file_id="f2").json()
    assert again["deduplicated"] is True and again["job_id"] is None
    assert again["document_id"] == first["document_id"] == resend["document_id"]
    session.expire_all()
    doc = await session.get(Document, uuid.UUID(first["document_id"]))
    assert doc.openwebui_file_ids == ["f1", "f2"]  # no duplicates on re-send


async def test_resent_file_id_does_not_retry_a_failed_document(client, session, user_a):
    first = send_file(client, user_a, file_id="f1").json()
    doc_id = uuid.UUID(first["document_id"])
    await force_doc(session, user_a, doc_id, status="failed")
    again = send_file(client, user_a, file_id="f1")  # Open WebUI re-sends it on every turn
    assert again.status_code == 201, again.text
    assert again.json()["deduplicated"] is True and again.json()["job_id"] is None
    session.expire_all()
    assert (await session.get(Document, doc_id)).status == "failed"
    assert (await session.scalars(select(Job).where(Job.user_id == user_a))).all() == []
    retry = send_file(client, user_a, file_id="f2").json()  # re-attached: new id, real retry
    assert retry["deduplicated"] is True and retry["job_id"]
    session.expire_all()
    assert (await session.get(Document, doc_id)).status == "uploaded"


def test_unsupported_and_oversize_uploads(client, user_a, monkeypatch):
    r = send_file(client, user_a, name="fake.pdf", data=b"not a pdf")
    assert (r.status_code, r.json()["error"]["code"]) == (415, "unsupported_media_type")
    monkeypatch.setattr(client.app.state.settings, "max_upload_mb", 1)
    r = send_file(client, user_a, name="big.txt", data=b"x" * (1024 * 1024 + 1))
    assert (r.status_code, r.json()["error"]["code"]) == (413, "payload_too_large")


@pytest.mark.parametrize("chat_id", [None, "", "../x", "a b", "x" * 65])
def test_bad_chat_id_is_422(client, user_a, chat_id):
    hdrs = headers(user_a) | ({"X-OWUI-Chat-Id": chat_id} if chat_id is not None else {})
    r = client.post(
        "/v1/pipe/files",
        files={"file": ("a.txt", TXT)},
        data={"openwebui_file_id": "f1"},
        headers=hdrs,
    )
    assert r.status_code == 422
    assert r.json()["error"] == {
        "code": "validation_error",
        "message": "Invalid X-OWUI-Chat-Id.",
        "request_id": r.json()["error"]["request_id"],
    }


@pytest.mark.parametrize("file_id", ["", "x" * 129])
def test_bad_file_id_is_422(client, user_a, file_id):
    assert send_file(client, user_a, file_id=file_id).status_code == 422


def test_files_without_bearer_is_401(client, user_a):
    r = client.post(
        "/v1/pipe/files",
        files={"file": ("a.txt", TXT)},
        data={"openwebui_file_id": "f1"},
        headers={"X-OWUI-User-Id": user_a, "X-OWUI-Chat-Id": "c"},
    )
    assert r.status_code == 401


async def test_same_chat_id_for_another_user_is_another_project(client, session, user_a, user_b):
    a = send_file(client, user_a).json()
    b = send_file(client, user_b).json()
    assert a["project_id"] != b["project_id"]
    assert b["deduplicated"] is False  # dedup never crosses tenants
    # user_b cannot read user_a's document through the project routes
    r = client.get(
        f"/v1/projects/{a['project_id']}/documents/{a['document_id']}", headers=headers(user_b)
    )
    assert r.status_code == 404


# ---------------------------------------------------------------- POST /v1/pipe/chat


def test_chat_validates_before_streaming(client, user_a):
    r = client.post("/v1/pipe/chat", json={"messages": []}, headers=pipe_headers(user_a))
    assert r.status_code == 422
    r = client.post("/v1/pipe/chat", json={"messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 401
    r = client.post(
        "/v1/pipe/chat",
        json={"messages": [{"role": "user", "content": "x"}]},
        headers=headers(user_a),
    )
    assert (r.status_code, r.json()["error"]["message"]) == (422, "Invalid X-OWUI-Chat-Id.")


def test_chat_response_is_an_event_stream(client, user_a, monkeypatch):
    set_intent(monkeypatch, Intent(intent="chitchat"))
    r, _ = chat(client, user_a)
    assert r.headers["content-type"].startswith("text/event-stream")


def test_chitchat_replies_with_help_text(client, user_a, monkeypatch):
    set_intent(monkeypatch, Intent(intent="chitchat"))
    monkeypatch.setattr(pipe, "HELP_TEXT", "HELP ME")
    _, frames = chat(client, user_a, "hello")
    assert [(e, d) for e, d in frames if e == "delta"] == [("delta", {"content": "HELP ME"})]
    event, done = frames[-1]
    assert event == "done"
    assert done["mapping_run_id"] is None and done["artifact_id"] is None
    uuid.UUID(done["project_id"])


async def test_router_gets_messages_and_project_documents(client, session, user_a, monkeypatch):
    doc_id = send_file(client, user_a).json()["document_id"]
    await force_doc(session, user_a, uuid.UUID(doc_id), status="indexed")
    seen = {}

    async def route(llm, messages, documents):
        seen["llm"], seen["messages"], seen["docs"] = llm, messages, documents
        return Intent(intent="chitchat")

    monkeypatch.setattr(pipe, "route", route)
    chat(client, user_a, "hello")
    assert seen["llm"] is client.app.state.llm
    assert [m.content for m in seen["messages"]] == ["hello"]
    assert [str(d.id) for d in seen["docs"]] == [doc_id]


def test_qa_streams_status_then_delta_then_done(client, user_a, monkeypatch):
    set_intent(monkeypatch, Intent(intent="qa"))
    cite = Citation(
        marker=1, document_id=uuid.uuid4(), chunk_id=uuid.uuid4(), filename="r.pdf",
        page_number=2, section_heading=None, quote="the quote",
    )  # fmt: skip
    ans = Answer(answer_markdown="It rained [1]", citations=[cite], found=True)
    asked = {}

    async def answer(ctx, question, document_ids, retriever, llm):
        asked.update(ctx=ctx, question=question, ids=document_ids)
        return ans

    monkeypatch.setattr(pipe, "answer", answer)
    monkeypatch.setattr(pipe, "render_answer", lambda a: f"RENDERED:{a.answer_markdown}")
    body = {
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "What fell?"},
        ]
    }
    r = client.post("/v1/pipe/chat", json=body, headers=pipe_headers(user_a))
    frames = parse_sse(r.text)
    assert asked["question"] == "What fell?" and asked["ids"] is None
    assert asked["ctx"].user_id == user_a
    kinds = events(frames)
    assert kinds == sorted(kinds, key=["status", "delta", "error", "done"].index)
    assert kinds[-1] == "done" and kinds.count("done") == 1
    assert [d for e, d in frames if e == "delta"] == [{"content": "RENDERED:It rained [1]"}]
    assert frames[-1][1]["project_id"] == str(asked["ctx"].project_id)


async def test_unknown_file_id_warns_by_filename_and_continues(client, user_a, monkeypatch):
    set_intent(monkeypatch, Intent(intent="chitchat"))
    monkeypatch.setattr(pipe, "HELP_TEXT", "HELP")
    _, frames = chat(client, user_a, files=[("ghost-id", "ghost.pdf")])
    deltas = [d["content"] for e, d in frames if e == "delta"]
    assert any("ghost.pdf" in c for c in deltas) and "HELP" in deltas
    assert events(frames)[-1] == "done"


async def test_skip_message_escapes_the_filename(client, user_a, monkeypatch):
    set_intent(monkeypatch, Intent(intent="chitchat"))
    _, frames = chat(
        client, user_a, files=[("ghost-id", "![t](https://tracker.example/p.png).pdf")]
    )
    skip = next(d["content"] for e, d in frames if e == "delta" and "skipped" in d["content"])
    assert "![t](" not in skip and "\\!\\[t\\]\\(https:\\/\\/tracker.example/p.png\\).pdf" in skip


async def test_map_template_without_a_choice_lists_candidates(client, session, user_a, monkeypatch):
    doc_id = send_file(client, user_a).json()["document_id"]
    await force_doc(session, user_a, uuid.UUID(doc_id), status="indexed")
    set_intent(monkeypatch, Intent(intent="map_template"))
    seen = {}

    def render_candidates(documents):
        seen["names"] = [d.filename for d in documents]
        return "PICK ONE"

    monkeypatch.setattr(pipe, "render_candidates", render_candidates)
    _, frames = chat(client, user_a, "fill the template")
    assert seen["names"] == ["report.pdf"]
    assert [d["content"] for e, d in frames if e == "delta"] == ["PICK ONE"]
    assert frames[-1][1]["mapping_run_id"] is None


@pytest.mark.parametrize(
    "exc, code",
    [
        (LLMError(), "upstream_unavailable"),
        (RuntimeError("SENTINEL document text"), "internal"),
    ],
)
def test_failures_emit_error_then_done_without_leaking(client, user_a, monkeypatch, exc, code):
    set_intent(monkeypatch, exc)
    _, frames = chat(client, user_a)
    assert events(frames)[-2:] == ["error", "done"]
    error = frames[-2][1]
    assert error["code"] == code
    assert "SENTINEL" not in json.dumps(frames)
    assert set(frames[-1][1]) == {"project_id", "mapping_run_id", "artifact_id"}


async def test_app_error_in_the_flow_is_an_error_event(client, session, user_a, monkeypatch):
    doc_id = send_file(client, user_a, name="notes.txt", data=TXT).json()["document_id"]
    await force_doc(session, user_a, uuid.UUID(doc_id), status="indexed")
    set_intent(monkeypatch, Intent(intent="map_template", template_document_id=uuid.UUID(doc_id)))
    _, frames = chat(client, user_a, files=[("f1", "notes.txt")])
    assert events(frames)[-2:] == ["error", "done"]
    assert frames[-2][1]["code"] == "unsupported_media_type"  # a .txt is not a template


async def test_failed_ingest_warns_and_continues(client, session, user_a, monkeypatch, fast_poll):
    doc_id = send_file(client, user_a, name="bad.txt", data=TXT).json()["document_id"]
    await session.execute(update(Job).where(Job.user_id == user_a).values(status="failed"))
    await session.commit()
    set_intent(monkeypatch, Intent(intent="chitchat"))
    monkeypatch.setattr(pipe, "HELP_TEXT", "HELP")
    _, frames = chat(client, user_a, files=[("f1", "bad.txt")])
    deltas = [d["content"] for e, d in frames if e == "delta"]
    assert any("bad.txt" in c for c in deltas) and "HELP" in deltas
    assert doc_id  # the stream ended instead of waiting forever


# ------------------------------------------- flows that wait on jobs (worker runs concurrently)


@asynccontextmanager
async def live_app(migrated_db, vector_store, llm, *, start=True):
    """The app with its lifespan, plus a loop running queued jobs like the worker would.

    With start=False the loop holds every job until the test sets `app.state.go`, so tests that
    rewrite job/document rows never race a worker that already claimed one.
    """
    from doc_intel.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.llm = llm
        app.state.vector_store = vector_store
        app.state.retriever = Retriever(FakeEmbedder(), vector_store)
        engine = make_engine(migrated_db)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        settings = get_settings()
        handlers = make_handlers(
            sm,
            LocalStorage(settings.storage_root),
            settings,
            FakeEmbedder(),
            vector_store,
            llm,
            app.state.retriever,
        )
        hooks = make_failure_hooks(sm)
        stop = asyncio.Event()
        app.state.go = go = asyncio.Event()
        if start:
            go.set()
        errors: list[Exception] = []

        async def worker() -> None:
            await go.wait()
            while not stop.is_set():
                try:
                    worked = await run_one(sm, handlers, hooks)
                except Exception as exc:  # keep polling like run_loop, but never hide it
                    errors.append(exc)
                    worked = False
                if not worked:
                    await asyncio.sleep(0.02)

        task = asyncio.create_task(worker())
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
                yield ac, sm, app
        finally:
            stop.set()
            go.set()
            await task
            await engine.dispose()
        assert not errors, errors


async def api_chat(ac, user, files, content="go", chat_id="chat-1"):
    body = {
        "messages": [{"role": "user", "content": content}],
        "files": [{"openwebui_file_id": i, "filename": n} for i, n in files],
    }
    r = await ac.post("/v1/pipe/chat", json=body, headers=pipe_headers(user, chat_id))
    assert r.status_code == 200, r.text
    return parse_sse(r.text)


async def api_file(ac, user, file_id, name, data):
    r = await ac.post(
        "/v1/pipe/files",
        files={"file": (name, data)},
        data={"openwebui_file_id": file_id},
        headers=pipe_headers(user),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_gated_worker_holds_jobs_until_released(migrated_db, vector_store, user_a):
    async with live_app(migrated_db, vector_store, FakeLLM(), start=False) as (ac, sm, app):
        meta = await api_file(ac, user_a, "f1", "depot.txt", TXT)
        job_id = uuid.UUID(meta["job_id"])
        await asyncio.sleep(0.2)
        async with sm.begin() as s:
            assert (await s.get(Job, job_id)).status == "queued"
        app.state.go.set()
        for _ in range(250):
            async with sm.begin() as s:
                status = (await s.get(Job, job_id)).status
            if status == "succeeded":
                break
            await asyncio.sleep(0.02)
    assert status == "succeeded"


async def test_waits_for_ingestion_and_reports_status(
    migrated_db, vector_store, user_a, monkeypatch, fast_poll
):
    set_intent(monkeypatch, Intent(intent="chitchat"))
    monkeypatch.setattr(pipe, "HELP_TEXT", "HELP")
    async with live_app(migrated_db, vector_store, FakeLLM(), start=False) as (ac, sm, app):
        meta = await api_file(ac, user_a, "f1", "depot.txt", TXT)
        app.state.go.set()  # the worker starts now; the stream still waits for it
        frames = await api_chat(ac, user_a, [("f1", "depot.txt")])
    kinds = events(frames)
    assert kinds[0] == "status" and kinds[-1] == "done"
    assert kinds == sorted(kinds, key=["status", "delta", "error", "done"].index)
    descriptions = [d["description"] for e, d in frames if e == "status"]
    assert all(d["done"] is False for e, d in frames if e == "status")
    assert descriptions == list(dict.fromkeys(descriptions))  # only changes are emitted
    assert "Parsing depot.txt" in descriptions or any("Embedding" in x for x in descriptions)
    assert [d["content"] for e, d in frames if e == "delta"] == ["HELP"]
    async with sm.begin() as s:
        doc = await s.get(Document, uuid.UUID(meta["document_id"]))
    assert doc.status == "indexed"


async def test_map_template_end_to_end(
    migrated_db, vector_store, user_a, monkeypatch, fast_poll, one_ir_batch
):
    llm = FakeLLM()
    summary_args = {}

    def render_summary(results, summary, ir, download_url):
        summary_args.update(results=results, summary=summary, ir=ir, url=download_url)
        return f"SUMMARY {download_url}"

    monkeypatch.setattr(pipe, "render_summary", render_summary)
    async with live_app(migrated_db, vector_store, llm, start=False) as (ac, sm, app):
        tpl = await api_file(ac, user_a, "tpl", "update.pptx", templates.pptx_template())
        src = await api_file(ac, user_a, "src", "sitrep.txt", CHUNK.encode())
        project_id = uuid.UUID(tpl["project_id"])
        ctx = TenantContext(user_a, project_id)
        source_id = uuid.UUID(src["document_id"])
        # Skip real parsing: mark both indexed and index the one source chunk directly.
        async with sm.begin() as s:
            await s.execute(delete(Job).where(Job.user_id == user_a))
            await s.execute(
                update(Document).where(Document.project_id == project_id).values(status="indexed")
            )
        payload = ChunkPayload(
            document_id=source_id, filename="sitrep.txt", page_number=None, section_heading=None,
            element_type="text", chunk_index=0, text=CHUNK,
        )  # fmt: skip
        await vector_store.upsert_chunks(
            ctx, [payload], await FakeEmbedder().embed_documents([CHUNK])
        )
        llm.replies += [
            draft_reply("pptx"),
            extraction([candidate("Substation fire", chunk_id(source_id, 0), QUOTE)]),
            extraction([], not_found=True),
        ]
        set_intent(
            monkeypatch,
            Intent(intent="map_template", template_document_id=uuid.UUID(tpl["document_id"])),
        )
        app.state.go.set()
        frames = await api_chat(ac, user_a, [("tpl", "update.pptx"), ("src", "sitrep.txt")])

    kinds = events(frames)
    assert kinds == sorted(kinds, key=["status", "delta", "error", "done"].index), frames
    assert "error" not in kinds, frames
    descriptions = [d["description"] for e, d in frames if e == "status"]
    assert descriptions == list(dict.fromkeys(descriptions))
    done = frames[-1][1]
    assert done["project_id"] == str(project_id)
    assert done["mapping_run_id"] and done["artifact_id"]
    assert "/v1/artifacts/" in summary_args["url"] and "sig=" in summary_args["url"]
    delta = next(d["content"] for e, d in frames if e == "delta")
    assert delta == f"SUMMARY {summary_args['url']}"
    assert summary_args["results"]["found"].status == "populated"
    assert summary_args["results"]["lost"].status == "missing"
    assert summary_args["ir"].fields[0].field_id == "found"
    assert done["artifact_id"] in summary_args["url"]


async def test_failed_template_job_is_an_error_event(
    migrated_db, vector_store, user_a, monkeypatch, fast_poll
):
    bad = json.loads(draft_reply("pptx"))
    bad["fields"][0]["anchor_id"] = "pptx:nope"
    llm = FakeLLM(json.dumps(bad), json.dumps(bad))  # IR validation fails twice -> ir_invalid
    async with live_app(migrated_db, vector_store, llm, start=False) as (ac, sm, app):
        tpl = await api_file(ac, user_a, "tpl", "update.pptx", templates.pptx_template())
        async with sm.begin() as s:
            await s.execute(delete(Job).where(Job.user_id == user_a))
            await s.execute(
                update(Document)
                .where(Document.id == uuid.UUID(tpl["document_id"]))
                .values(status="indexed")
            )
        set_intent(
            monkeypatch,
            Intent(intent="map_template", template_document_id=uuid.UUID(tpl["document_id"])),
        )
        app.state.go.set()
        frames = await api_chat(ac, user_a, [("tpl", "update.pptx")])
    assert events(frames)[-2:] == ["error", "done"]
    assert frames[-2][1]["code"] == "ir_invalid"
    assert frames[-1][1]["mapping_run_id"] is None


async def test_waits_for_pending_documents_not_listed_in_files(
    migrated_db, vector_store, user_a, monkeypatch, fast_poll
):
    seen = {}

    async def route(llm, messages, documents):
        seen["statuses"] = [d.status for d in documents]
        return Intent(intent="chitchat")

    monkeypatch.setattr(pipe, "route", route)
    async with live_app(migrated_db, vector_store, FakeLLM(), start=False) as (ac, _, app):
        await api_file(ac, user_a, "f1", "depot.txt", TXT)  # held: the doc is pending at the start

        async def release() -> None:
            await asyncio.sleep(0.2)
            app.state.go.set()

        task = asyncio.create_task(release())
        frames = await api_chat(ac, user_a, [])  # the document is not referenced in `files`
        await task
    assert seen["statuses"] == ["indexed"]
    assert events(frames)[-1] == "done"


async def test_watch_sends_keep_alive_comment_when_description_is_unchanged():
    jobs = iter([Job(status="running"), Job(status="running"), Job(status="succeeded")])

    class Begin:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    async def disconnected():
        return False

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(sessionmaker=SimpleNamespace(begin=Begin))),
        is_disconnected=disconnected,
    )

    async def load(session):
        return next(jobs)

    old = pipe.POLL_INTERVAL_S
    pipe.POLL_INTERVAL_S = 0.0
    try:
        frames = [f async for f in pipe._watch(request, load, lambda j: "same", [])]
    finally:
        pipe.POLL_INTERVAL_S = old
    assert frames == [
        pipe.sse("status", {"description": "same", "done": False}),
        ": keep-alive\n\n",
    ]


def test_progress_text_tolerates_missing_counters():
    assert pipe._ingest_text("a.pdf")(Job(progress={"stage": "embedding"})) == "Parsing a.pdf"
    assert pipe._ingest_text("a.pdf")(
        Job(progress={"stage": "embedding", "done": 1, "total": 2})
    ) == ("Embedding a.pdf (1/2)")
    assert pipe._mapping_text(Job(progress={"total": 3})) == "Mapping fields"


def test_extract_and_mapping_text_show_batch_and_field_progress():
    ir = {"stage": "ir", "done": 1, "total": 10}
    assert pipe._extract_text(Job(progress={**ir, "part": "slide 2"})) == (
        "Analyzing template fields: slide 2 (2/10)"
    )
    assert pipe._extract_text(Job(progress=ir)) == "Analyzing template fields (2/10)"
    assert pipe._extract_text(Job(progress={"stage": "ir"})) == "Analyzing template fields"
    assert pipe._extract_text(Job(progress={"stage": "anchors"})) == "Reading template structure"
    mapping = {"total": 3, "done": 1, "field": "budget"}
    assert pipe._mapping_text(Job(progress=mapping)) == "Mapping field 2/3: budget"
    assert pipe._mapping_text(Job(progress={**mapping, "done": 3})) == "Rendering document"
    assert pipe._mapping_text(Job(progress={"total": 3, "done": 1})) == "Mapping fields (1/3)"


def test_chat_rejects_more_than_50_messages(client, user_a, monkeypatch):
    set_intent(monkeypatch, Intent(intent="chitchat"))
    msgs = [{"role": "user", "content": "x"}] * 51
    r = client.post("/v1/pipe/chat", json={"messages": msgs}, headers=pipe_headers(user_a))
    assert r.status_code == 422
    r = client.post("/v1/pipe/chat", json={"messages": msgs[:50]}, headers=pipe_headers(user_a))
    assert r.status_code == 200
