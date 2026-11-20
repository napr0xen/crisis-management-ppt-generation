"""Pipe flow with the template library: no uploaded template, pick one, adopt it, fill it."""

import io
import json
import re
import uuid
from pathlib import Path

import pytest
from docx import Document as DocxDocument
from pptx import Presentation
from sqlalchemy import delete, select, update

from doc_intel.api.v1 import pipe
from doc_intel.chat.router import Intent
from doc_intel.chat.summary import render_library
from doc_intel.core.tenancy import TenantContext
from doc_intel.db.models import Document, Job, LibraryTemplate, Template
from doc_intel.ingestion.schemas import ChunkPayload
from doc_intel.llm.prompts import load_prompt
from doc_intel.templates import library
from tests.fakes import FakeEmbedder
from tests.fixtures import templates
from tests.integration.test_generation_e2e import CHUNK, QUOTE, draft_reply
from tests.integration.test_library import FILES
from tests.integration.test_mapping_e2e import candidate, extraction
from tests.integration.test_pipe_api import (
    api_file,
    events,
    fast_poll,  # noqa: F401 (fixture)
    live_app,
    parse_sse,
    pipe_headers,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("empty_queue", "fast_poll", "one_ir_batch", "clean_library"),
]

CHUNK_ID = re.compile(r"\[([0-9a-f-]{36}) \|")


class ScriptedLLM:
    """Replies by system prompt. `route` is the router JSON; the extraction cites the first hit."""

    def __init__(self, route: dict) -> None:
        self.route = route
        self.extractions = 0
        self.ir_calls = 0

    async def complete(self, messages, json_schema=None) -> str:
        system, user = messages[0]["content"], messages[-1]["content"]
        if system == load_prompt("router"):
            reply = dict(self.route)
            if reply.get("template_document_id") == "UPLOADED":
                reply["template_document_id"] = next(
                    ln.split(" | ")[0] for ln in user.splitlines() if "update.pptx" in ln
                )
            return json.dumps(reply)
        if system == load_prompt("template_ir"):
            self.ir_calls += 1
            return draft_reply("pptx")
        assert system == load_prompt("field_extract")
        self.extractions += 1
        hit = CHUNK_ID.search(user).group(1)
        if self.extractions % 2:
            return extraction([candidate("Substation fire", hit, QUOTE)])
        return extraction([], not_found=True)


def unique(fmt: str, title: str) -> bytes:
    """The fixture bytes with a distinct title, so two library files never share a hash."""
    doc = (Presentation if fmt == "pptx" else DocxDocument)(io.BytesIO(FILES[fmt]))
    doc.core_properties.title = title
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


async def seed(app, sm, tmp_path: Path, name: str, fmt: str = "pptx") -> LibraryTemplate:
    path = tmp_path / f"{name}.{fmt}"
    path.write_bytes(unique(fmt, name))
    return await library.register(
        sm,
        app.state.storage,
        _IRLLM(draft_reply(fmt)),
        path,
        name=name,
        description=f"{name} layout",
        max_bytes=10**7,
    )


class _IRLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def complete(self, messages, json_schema=None) -> str:
        return self.reply


async def add_source(ac, sm, user, vector_store, app):
    """Upload a source, mark it indexed and index its one chunk (no real parsing)."""
    src = await api_file(ac, user, "src", "sitrep.txt", CHUNK.encode())
    project_id, source_id = uuid.UUID(src["project_id"]), uuid.UUID(src["document_id"])
    async with sm.begin() as s:
        await s.execute(delete(Job).where(Job.user_id == user))
        await s.execute(update(Document).where(Document.id == source_id).values(status="indexed"))
    payload = ChunkPayload(
        document_id=source_id, filename="sitrep.txt", page_number=None, section_heading=None,
        element_type="text", chunk_index=0, text=CHUNK,
    )  # fmt: skip
    await vector_store.upsert_chunks(
        TenantContext(user, project_id), [payload], await FakeEmbedder().embed_documents([CHUNK])
    )
    app.state.go.set()  # the held ingest job is gone; only later jobs run
    return project_id


async def turn(ac, user, *messages, files=(("src", "sitrep.txt"),)):
    body = {
        "messages": [{"role": r, "content": c} for r, c in messages],
        "files": [{"openwebui_file_id": i, "filename": n} for i, n in files],
    }
    r = await ac.post("/v1/pipe/chat", json=body, headers=pipe_headers(user))
    assert r.status_code == 200, r.text
    return parse_sse(r.text)


def deltas(frames) -> list[str]:
    return [d["content"] for e, d in frames if e == "delta"]


async def job_kinds(sm, user) -> list[str]:
    async with sm.begin() as s:
        return list(await s.scalars(select(Job.kind).where(Job.user_id == user)))


async def template_formats(sm, project_id) -> list[str]:
    async with sm.begin() as s:
        return list(
            await s.scalars(select(Template.format).where(Template.project_id == project_id))
        )


def assert_filled(frames) -> None:
    assert "error" not in events(frames), frames
    assert frames[-1][1]["mapping_run_id"] and frames[-1][1]["artifact_id"]
    assert "[Download the filled template](" in "\n".join(deltas(frames))


async def test_single_matching_library_template_runs_without_extraction(
    migrated_db, vector_store, user_a, tmp_path
):
    llm = ScriptedLLM({"intent": "map_template", "format": "pptx"})
    async with live_app(migrated_db, vector_store, llm, start=False) as (ac, sm, app):
        await seed(app, sm, tmp_path, "Status deck")
        project_id = await add_source(ac, sm, user_a, vector_store, app)
        frames = await turn(ac, user_a, ("user", "make a powerpoint out of this"))
        kinds = await job_kinds(sm, user_a)
        formats = await template_formats(sm, project_id)
    assert_filled(frames)
    assert "Using the library template **Status deck**." in deltas(frames)[0]
    assert "extract_template" not in kinds and "run_mapping" in kinds
    assert llm.ir_calls == 0 and formats == ["pptx"]


async def test_two_templates_list_then_a_number_reply_runs_that_one(
    migrated_db, vector_store, user_a, tmp_path
):
    llm = ScriptedLLM({"intent": "map_template"})
    async with live_app(migrated_db, vector_store, llm, start=False) as (ac, sm, app):
        a = await seed(app, sm, tmp_path, "Alpha deck")
        b = await seed(app, sm, tmp_path, "Beta memo", "docx")
        project_id = await add_source(ac, sm, user_a, vector_store, app)
        first = await turn(ac, user_a, ("user", "make me something"))
        listing = deltas(first)[0]
        assert listing == render_library([a, b]) and frames_done(first)["mapping_run_id"] is None
        second = await turn(
            ac, user_a, ("user", "make me something"), ("assistant", listing), ("user", "2")
        )
        formats = await template_formats(sm, project_id)
    assert_filled(second)
    assert "Using the library template **Beta memo**." in deltas(second)[0]
    assert formats == ["docx"]


def frames_done(frames) -> dict:
    return frames[-1][1]


async def test_format_narrows_the_list(migrated_db, vector_store, user_a, tmp_path):
    llm = ScriptedLLM({"intent": "map_template", "format": "docx"})
    async with live_app(migrated_db, vector_store, llm, start=False) as (ac, sm, app):
        await seed(app, sm, tmp_path, "Alpha deck")
        memo = await seed(app, sm, tmp_path, "Beta memo", "docx")
        other = await seed(app, sm, tmp_path, "Gamma memo", "docx")
        await add_source(ac, sm, user_a, vector_store, app)
        frames = await turn(ac, user_a, ("user", "make a word document"))
    assert deltas(frames) == [render_library([memo, other])]


async def test_explicit_library_name_runs_it(migrated_db, vector_store, user_a, tmp_path):
    llm = ScriptedLLM({"intent": "map_template"})
    async with live_app(migrated_db, vector_store, llm, start=False) as (ac, sm, app):
        await seed(app, sm, tmp_path, "Alpha deck")
        await seed(app, sm, tmp_path, "Beta memo", "docx")
        project_id = await add_source(ac, sm, user_a, vector_store, app)
        frames = await turn(ac, user_a, ("user", "please use the alpha DECK"))
        formats = await template_formats(sm, project_id)
    assert_filled(frames)
    assert formats == ["pptx"]


async def test_uploaded_template_keeps_the_existing_path(
    migrated_db, vector_store, user_a, tmp_path
):
    llm = ScriptedLLM({"intent": "map_template", "template_document_id": "UPLOADED"})
    async with live_app(migrated_db, vector_store, llm, start=False) as (ac, sm, app):
        await seed(app, sm, tmp_path, "Status deck")
        tpl = await api_file(ac, user_a, "tpl", "update.pptx", templates.pptx_template())
        project_id = await add_source(ac, sm, user_a, vector_store, app)  # also drops tpl's job
        async with sm.begin() as s:
            await s.execute(
                update(Document)
                .where(Document.id == uuid.UUID(tpl["document_id"]))
                .values(status="indexed")
            )
        frames = await turn(
            ac, user_a, ("user", "fill the template"),
            files=(("src", "sitrep.txt"), ("tpl", "update.pptx")),
        )  # fmt: skip
        kinds = await job_kinds(sm, user_a)
        formats = await template_formats(sm, project_id)
    assert_filled(frames)
    assert "extract_template" in kinds and llm.ir_calls == 1
    assert "library template" not in "\n".join(deltas(frames))
    assert formats == ["pptx"]


async def test_confirmed_upload_beats_a_library_name_in_the_message(
    migrated_db, vector_store, user_a, tmp_path
):
    llm = ScriptedLLM({"intent": "map_template", "template_document_id": "UPLOADED"})
    async with live_app(migrated_db, vector_store, llm, start=False) as (ac, sm, app):
        await seed(app, sm, tmp_path, "update")  # same word as the uploaded filename
        tpl = await api_file(ac, user_a, "tpl", "update.pptx", templates.pptx_template())
        await add_source(ac, sm, user_a, vector_store, app)
        async with sm.begin() as s:
            await s.execute(
                update(Document)
                .where(Document.id == uuid.UUID(tpl["document_id"]))
                .values(status="indexed")
            )
        frames = await turn(
            ac, user_a, ("user", "fill update.pptx"),
            files=(("src", "sitrep.txt"), ("tpl", "update.pptx")),
        )  # fmt: skip
        kinds = await job_kinds(sm, user_a)
    assert_filled(frames)
    assert "extract_template" in kinds
    assert "library template" not in "\n".join(deltas(frames))


async def test_library_copy_is_hidden_from_the_router_next_turn(
    migrated_db, vector_store, user_a, tmp_path, monkeypatch
):
    llm = ScriptedLLM({"intent": "map_template", "format": "pptx"})
    seen = []
    real_route = pipe.route

    async def spy(llm_, messages, documents):
        seen.append([d.filename for d in documents])
        return await real_route(llm_, messages, documents)

    monkeypatch.setattr(pipe, "route", spy)
    async with live_app(migrated_db, vector_store, llm, start=False) as (ac, sm, app):
        await seed(app, sm, tmp_path, "Status deck")
        await add_source(ac, sm, user_a, vector_store, app)
        first = await turn(ac, user_a, ("user", "make a powerpoint out of this"))
        second = await turn(ac, user_a, ("user", "make a powerpoint out of this"))
    assert_filled(first)
    assert seen == [["sitrep.txt"], ["sitrep.txt"]]  # the adopted "Status deck.pptx" is not listed
    assert_filled(second)  # a second request reuses the copy


async def test_unchosen_uploaded_template_lists_candidates_not_the_library(
    migrated_db, vector_store, user_a, tmp_path, monkeypatch
):
    async def route(llm, messages, documents):
        return Intent(intent="map_template")

    monkeypatch.setattr(pipe, "route", route)
    monkeypatch.setattr(pipe, "render_candidates", lambda docs: "PICK")
    async with live_app(migrated_db, vector_store, ScriptedLLM({}), start=False) as (ac, sm, app):
        await seed(app, sm, tmp_path, "Status deck")
        await api_file(ac, user_a, "tpl", "update.pptx", templates.pptx_template())
        async with sm.begin() as s:
            await s.execute(delete(Job).where(Job.user_id == user_a))
            await s.execute(
                update(Document).where(Document.user_id == user_a).values(status="indexed")
            )
        app.state.go.set()
        frames = await turn(ac, user_a, ("user", "fill it"), files=(("tpl", "update.pptx"),))
    assert deltas(frames) == ["PICK"]
