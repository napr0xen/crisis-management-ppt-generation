"""Chat edits through `POST /v1/pipe/chat` (Phase 11): a new run version per edit."""

import json
import uuid

import pytest
from sqlalchemy import func, select

from doc_intel.chat.router import Intent
from doc_intel.core.config import get_settings
from doc_intel.core.tenancy import TenantContext
from doc_intel.db import repo
from doc_intel.db.models import MappingRun
from doc_intel.storage.local import LocalStorage
from tests.fakes import FakeLLM
from tests.integration.test_edits_api import _conflict, _result
from tests.integration.test_mapping_runs_api import field, seed_document, seed_template_ir
from tests.integration.test_pipe_api import (  # noqa: F401 (fixtures)
    api_chat,
    events,
    fast_poll,
    live_app,
    set_intent,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("empty_queue", "fast_poll"),
]


@pytest.fixture(autouse=True)
def fake_render(monkeypatch):
    monkeypatch.setattr("doc_intel.writers.render", lambda *a, **k: (b"PK\x03\x04fake", []))


def edit_reply(*edits: dict) -> FakeLLM:
    return FakeLLM(json.dumps({"edits": list(edits)}))


async def seed_completed_run(sm, user, chat="chat-1") -> TenantContext:
    async with sm.begin() as s:
        project = await repo.get_or_create_chat_project(s, user, chat)
    ctx = TenantContext(user, project.id)
    async with sm() as s:
        ir_id = await seed_template_ir(s, ctx, [field("title"), field("owner")])
        ir = await repo.get_template_ir_by_id(s, ctx, ir_id)
        template = await repo.get_template(s, ctx, ir.template_id)
        doc = await repo.get_document(s, ctx, template.document_id)
        LocalStorage(get_settings().storage_root).put_bytes(doc.storage_key, b"TEMPLATE")
        await seed_document(s, ctx)
        summary = {"populated": 0, "partial": 0, "missing": 1, "conflict": 1, "warnings": []}
        await repo.create_mapping_run(
            s,
            ctx,
            template_ir_id=ir_id,
            document_ids=[uuid.uuid4()],
            status="completed",
            results={"title": _conflict("title"), "owner": _result("owner")},
            summary=summary,
        )
        await s.commit()
    return ctx


async def run_count(sm, ctx) -> int:
    async with sm() as s:
        return await s.scalar(
            select(func.count())
            .select_from(MappingRun)
            .where(MappingRun.project_id == ctx.project_id)
        )


async def test_chat_edit_creates_version_two_with_a_download_link(
    migrated_db, vector_store, user_a, monkeypatch
):
    set_intent(monkeypatch, Intent(intent="edit"))
    llm = edit_reply({"op": "set_value", "field_id": "owner", "value": "Ann"})
    async with live_app(migrated_db, vector_store, llm) as (ac, sm, _):
        ctx = await seed_completed_run(sm, user_a)
        frames = await api_chat(ac, user_a, [], content="set owner to Ann")
        kinds = events(frames)
        assert kinds[-1] == "done" and "error" not in kinds
        assert kinds == sorted(kinds, key=["status", "delta", "error", "done"].index)
        text = "\n".join(d["content"] for e, d in frames if e == "delta")
        assert "Applied 1 edit(s). This is version 2." in text
        assert "- Set **Owner** to Ann" in text
        assert "[Download the filled template](" in text
        done = frames[-1][1]
        async with sm() as s:
            run = await repo.get_mapping_run(s, ctx, uuid.UUID(done["mapping_run_id"]))
            artifact = await repo.get_artifact_by_run(s, ctx, run.id)
        assert run.version == 2 and run.trigger == "edit"
        assert done["artifact_id"] == str(artifact.id)


async def test_edit_without_a_generated_document_says_so(
    migrated_db, vector_store, user_a, monkeypatch
):
    set_intent(monkeypatch, Intent(intent="edit"))
    async with live_app(migrated_db, vector_store, FakeLLM()) as (ac, _, _):
        frames = await api_chat(ac, user_a, [], content="set owner to Ann")
    text = "\n".join(d["content"] for e, d in frames if e == "delta")
    assert "no generated document to edit" in text
    assert frames[-1][1]["mapping_run_id"] is None


async def test_fabricated_value_is_rejected_and_creates_no_run(
    migrated_db, vector_store, user_a, monkeypatch
):
    set_intent(monkeypatch, Intent(intent="edit"))
    llm = edit_reply({"op": "set_value", "field_id": "owner", "value": "Mallory"})
    async with live_app(migrated_db, vector_store, llm) as (ac, sm, _):
        ctx = await seed_completed_run(sm, user_a)
        frames = await api_chat(ac, user_a, [], content="change the owner")
        text = "\n".join(d["content"] for e, d in frames if e == "delta")
        assert "I could not apply that edit:" in text and "Mallory" not in text
        assert "error" not in events(frames) and frames[-1][1]["mapping_run_id"] is None
        assert await run_count(sm, ctx) == 1


async def test_edit_naming_no_field_asks_again(migrated_db, vector_store, user_a, monkeypatch):
    set_intent(monkeypatch, Intent(intent="edit"))
    async with live_app(migrated_db, vector_store, edit_reply()) as (ac, sm, _):
        ctx = await seed_completed_run(sm, user_a)
        frames = await api_chat(ac, user_a, [], content="change something")
        text = "\n".join(d["content"] for e, d in frames if e == "delta")
        assert "which field to change" in text and await run_count(sm, ctx) == 1
