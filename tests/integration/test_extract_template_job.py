import json
import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from doc_intel.core.config import get_settings
from doc_intel.core.tenancy import TenantContext
from doc_intel.db import repo
from doc_intel.db.models import Document, Job, Template, TemplateIRVersion
from doc_intel.db.session import make_engine
from doc_intel.jobs.handlers import make_failure_hooks, make_handlers
from doc_intel.jobs.worker import run_one
from doc_intel.mapping.retriever import Retriever
from doc_intel.storage.local import LocalStorage
from doc_intel.templates.anchors import build_anchor_map
from doc_intel.templates.schemas import TEXT_KINDS
from tests.conftest import headers
from tests.fakes import FakeEmbedder, FakeLLM
from tests.fixtures import templates

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("empty_queue")]

BUILDERS = {
    "pptx": templates.pptx_template,
    "docx": templates.docx_template,
    "xlsx": templates.xlsx_template,
}


class NoopStore:
    async def delete_document_points(self, ctx, document_id) -> None:
        return None


def draft_reply(fmt: str) -> str:
    """A valid IRDraft with one field on the first anchor of the fixture."""
    anchor = build_anchor_map(fmt, BUILDERS[fmt]()).anchors[0]
    text = anchor.kind in TEXT_KINDS
    field = {
        "field_id": "first_field",
        "anchor_id": anchor.anchor_id,
        "section_path": anchor.path,
        "label": "First field",
        "description": "The first fillable location.",
        "expected_type": "text" if text else "number",
        "required": True,
        "write_mode": "replace_text" if text else "fill_cell",
        "constraints": {},
    }
    return json.dumps({"fields": [field]})


async def queue_job(client, session, user, project, fmt, data=None):
    """Upload, mark indexed, POST /templates; returns (document_id, job_id)."""
    client.app.state.vector_store = NoopStore()
    r = client.post(
        f"/v1/projects/{project}/documents",
        files={"file": (f"t.{fmt}", data or BUILDERS[fmt]())},
        headers=headers(user),
    )
    assert r.status_code == 201, r.text
    doc_id = r.json()["document_id"]
    ctx = TenantContext(user, uuid.UUID(project))
    await repo.update_document(session, ctx, uuid.UUID(doc_id), status="indexed")
    await session.execute(delete(Job).where(Job.user_id == user))  # the upload's ingest job
    await session.commit()
    return doc_id, post_template(client, user, project, doc_id)


def post_template(client, user, project, doc_id) -> str:
    r = client.post(
        f"/v1/projects/{project}/templates", json={"document_id": doc_id}, headers=headers(user)
    )
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


async def run_jobs(migrated_db, llm, rounds=1):
    engine = make_engine(migrated_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    settings = get_settings()
    handlers = make_handlers(
        sm,
        LocalStorage(settings.storage_root),
        settings,
        FakeEmbedder(),
        NoopStore(),
        llm,
        Retriever(FakeEmbedder(), NoopStore()),
    )
    try:
        for _ in range(rounds):
            await run_one(sm, handlers, make_failure_hooks(sm))
    finally:
        await engine.dispose()


async def job_of(session, job_id) -> Job:
    session.expire_all()
    return await session.get(Job, uuid.UUID(job_id))


@pytest.mark.parametrize("fmt", ["pptx", "docx", "xlsx"])
async def test_extracts_template_and_ir(client, session, migrated_db, user_a, project, fmt):
    doc_id, job_id = await queue_job(client, session, user_a, project, fmt)
    await run_jobs(migrated_db, FakeLLM(draft_reply(fmt)))

    job = await job_of(session, job_id)
    assert job.status == "succeeded", job.error_code
    assert set(job.result) == {"template_id", "template_ir_id", "ir_version"}
    assert job.result["ir_version"] == 1
    template = await session.get(Template, uuid.UUID(job.result["template_id"]))
    ir = await session.get(TemplateIRVersion, uuid.UUID(job.result["template_ir_id"]))
    assert (template.user_id, template.project_id) == (user_a, uuid.UUID(project))
    assert (ir.user_id, ir.project_id) == (user_a, uuid.UUID(project))
    assert str(template.document_id) == doc_id
    assert template.format == fmt
    assert template.anchor_count == len(template.anchor_map["anchors"]) > 0
    assert ir.template_id == template.id
    assert ir.ir["template_id"] == str(template.id)
    assert ir.ir["format"] == fmt
    assert ir.ir["fields"][0]["field_id"] == "first_field"
    assert "max_chars" not in ir.ir["fields"][0]["constraints"]  # exclude_none

    # the same tenant keys serve the API
    got = client.get(
        f"/v1/projects/{project}/templates/{template.id}/irs/1", headers=headers(user_a)
    )
    assert got.status_code == 200
    assert got.json()["ir"] == ir.ir
    doc = await session.get(Document, uuid.UUID(doc_id))
    assert doc.role == "template"


async def test_second_run_reuses_template_and_ir(client, session, migrated_db, user_a, project):
    doc_id, first_job = await queue_job(client, session, user_a, project, "pptx")
    await run_jobs(migrated_db, FakeLLM(draft_reply("pptx")))
    first = (await job_of(session, first_job)).result

    second_job = post_template(client, user_a, project, doc_id)
    llm = FakeLLM()  # a call would pop from an empty list and fail the job
    await run_jobs(migrated_db, llm)

    assert llm.calls == []
    assert (await job_of(session, second_job)).result == first
    pid = uuid.UUID(project)
    for model in (Template, TemplateIRVersion):
        count = await session.scalar(
            select(func.count()).where(model.user_id == user_a, model.project_id == pid)
        )
        assert count == 1


async def test_invalid_ir_twice_fails_without_retry_or_rows(
    client, session, migrated_db, user_a, project
):
    doc_id, job_id = await queue_job(client, session, user_a, project, "pptx")
    bad = json.loads(draft_reply("pptx"))
    bad["fields"][0]["anchor_id"] = "no_such_anchor"
    await run_jobs(migrated_db, FakeLLM(json.dumps(bad), json.dumps(bad)))

    job = await job_of(session, job_id)
    assert job.status == "failed"
    assert job.error_code == "ir_invalid"
    assert job.attempts == 1
    assert job.error_message == "Job failed after 1 attempt(s) (ir_invalid)."
    assert not any(p in job.error_message for p in ("pptx:", "docx:", "xlsx:", "no_such_anchor"))
    count = await session.scalar(select(func.count()).where(Template.user_id == user_a))
    assert count == 0
    doc = await session.get(Document, uuid.UUID(doc_id))
    assert (doc.role, doc.status) == ("template", "indexed")


async def test_corrupt_file_fails_with_parse_error(client, session, migrated_db, user_a, project):
    doc_id, job_id = await queue_job(client, session, user_a, project, "pptx")
    doc = await session.get(Document, uuid.UUID(doc_id))
    client.app.state.storage.put_bytes(doc.storage_key, b"PK\x03\x04 not an office file", True)
    llm = FakeLLM()
    await run_jobs(migrated_db, llm)

    job = await job_of(session, job_id)
    assert (job.status, job.error_code) == ("failed", "parse_error")
    assert llm.calls == []
    await session.refresh(doc)
    assert (doc.role, doc.status) == ("template", "indexed")
    count = await session.scalar(select(func.count()).where(Template.user_id == user_a))
    assert count == 0


async def test_deleted_document_fails_after_one_attempt(
    client, session, migrated_db, user_a, project
):
    doc_id, job_id = await queue_job(client, session, user_a, project, "pptx")
    await session.execute(delete(Document).where(Document.id == uuid.UUID(doc_id)))
    await session.commit()
    await run_jobs(migrated_db, FakeLLM(draft_reply("pptx")))
    job = await job_of(session, job_id)
    assert (job.status, job.attempts, job.error_code) == ("failed", 1, "not_found")
