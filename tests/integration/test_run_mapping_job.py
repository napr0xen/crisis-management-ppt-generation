import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from doc_intel.core.config import get_settings
from doc_intel.db import repo
from doc_intel.db.models import Job, MappingRun
from doc_intel.db.session import make_engine
from doc_intel.jobs import handlers as handlers_mod
from doc_intel.jobs.handlers import make_failure_hooks, make_handlers
from doc_intel.jobs.worker import MAX_JOB_ATTEMPTS, recover_running_jobs, run_one
from doc_intel.mapping.schemas import FieldMappingResult, MappingSummary
from doc_intel.storage.local import LocalStorage
from doc_intel.templates.schemas import TemplateIR
from tests.fakes import FakeEmbedder, FakeLLM
from tests.integration.test_mapping_repo import seed_ir

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("empty_queue")]


def ir_json(template_id, ids):
    fields = [
        {
            "field_id": fid,
            "anchor_id": f"a{i}",
            "section_path": ["s"],
            "label": "L",
            "description": "D",
            "expected_type": "text",
            "required": True,
            "write_mode": "replace_text",
            "constraints": {},
        }
        for i, fid in enumerate(ids)
    ]
    return TemplateIR(template_id=template_id, version=1, format="pptx", fields=fields).model_dump(
        mode="json", exclude_none=True
    )


def missing(field, docs):
    return FieldMappingResult(
        field_id=field.field_id,
        status="missing",
        value=None,
        evidence=[],
        confidence=0.0,
        missing_reason="not_found_by_llm",
        checked_documents=docs,
        conflict_details=None,
        provenance="rag",
    )


@pytest.fixture
async def sm(migrated_db):
    engine = make_engine(migrated_db)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def queue_run(sm, user, field_ids=("f_one", "f_two")):
    async with sm.begin() as s:
        ctx, ir = await seed_ir(s, user)
        ir.ir = ir_json(ir.template_id, field_ids)
        docs = [uuid.uuid4()]
        run = await repo.create_mapping_run(s, ctx, template_ir_id=ir.id, document_ids=docs)
        job = await repo.create_job(s, ctx, "run_mapping", {"mapping_run_id": str(run.id)})
        template = await repo.get_template(s, ctx, ir.template_id)
        doc = await repo.get_document(s, ctx, template.document_id)
        doc.storage_key = f"uploads/{doc.id}/original.pptx"
        LocalStorage(get_settings().storage_root).put_bytes(doc.storage_key, b"TEMPLATE")
    return ctx, run.id, job.id, docs


def handlers_for(sm):
    return make_handlers(
        sm, LocalStorage(get_settings().storage_root), get_settings(), FakeEmbedder(), None,
        FakeLLM(), None,
    )  # fmt: skip


async def state(sm, ctx, run_id, job_id):
    async with sm() as s:
        return await repo.get_mapping_run(s, ctx, run_id), await s.get(Job, job_id)


@pytest.fixture(autouse=True)
def fake_render(monkeypatch):
    monkeypatch.setattr("doc_intel.writers.render", lambda *a, **k: (b"PK\x03\x04fake", []))


async def test_run_mapping_lifecycle(sm, user_a, monkeypatch):
    ctx, run_id, job_id, docs = await queue_run(sm, user_a)
    calls = []

    async def fake_map(c, field, document_ids, retriever, llm):
        calls.append((field.field_id, document_ids))
        return missing(field, document_ids)

    monkeypatch.setattr(handlers_mod, "map_field", fake_map)
    await run_one(sm, handlers_for(sm), make_failure_hooks(sm))

    run, job = await state(sm, ctx, run_id, job_id)
    assert calls == [("f_one", docs), ("f_two", docs)]
    assert run.status == "completed" and run.finished_at is not None
    assert list(run.results) == ["f_one", "f_two"]
    assert run.results["f_one"]["status"] == "missing"
    assert run.results["f_one"]["checked_documents"] == [str(docs[0])]
    assert run.summary == {
        "populated": 0,
        "partial": 0,
        "missing": 2,
        "conflict": 0,
        "warnings": [],
    }
    MappingSummary.model_validate(run.summary)
    assert job.status == "succeeded"
    async with sm() as s:
        artifact = await repo.get_artifact_by_run(s, ctx, run_id)
    assert job.result == {"mapping_run_id": str(run_id), "artifact_id": str(artifact.id)}
    assert job.progress == {"stage": "mapping", "done": 2, "total": 2}


async def test_run_mapping_publishes_the_current_field_before_mapping_it(sm, user_a, monkeypatch):
    _, _, job_id, _ = await queue_run(sm, user_a)
    seen = []

    async def fake_map(c, field, document_ids, retriever, llm):
        async with sm() as s:
            seen.append((await s.get(Job, job_id)).progress)
        return missing(field, document_ids)

    monkeypatch.setattr(handlers_mod, "map_field", fake_map)
    await run_one(sm, handlers_for(sm), make_failure_hooks(sm))
    assert seen == [
        {"stage": "mapping", "total": 2, "done": 0, "field": "f_one"},
        {"stage": "mapping", "total": 2, "done": 1, "field": "f_two"},
    ]


async def test_run_mapping_skips_mapping_and_renders_when_already_verifying(
    sm, user_a, monkeypatch
):
    ctx, run_id, job_id, _ = await queue_run(sm, user_a)
    async with sm.begin() as s:
        await repo.update_mapping_run(s, ctx, run_id, status="verifying", summary={"x": 1})

    async def boom(*a):
        raise AssertionError("map_field must not run")

    monkeypatch.setattr(handlers_mod, "map_field", boom)
    await run_one(sm, handlers_for(sm), make_failure_hooks(sm))

    run, job = await state(sm, ctx, run_id, job_id)
    assert job.status == "succeeded" and run.status == "completed"
    assert job.result["artifact_id"] is not None
    assert run.summary == {"x": 1, "warnings": []}


@pytest.mark.parametrize("status", ["failed", "completed"])
async def test_run_mapping_leaves_a_terminal_run_untouched(sm, user_a, monkeypatch, status):
    ctx, run_id, job_id, _ = await queue_run(sm, user_a)
    async with sm.begin() as s:
        await repo.update_mapping_run(s, ctx, run_id, status=status, summary={"x": 1})

    async def boom(*a):
        raise AssertionError("map_field must not run")

    monkeypatch.setattr(handlers_mod, "map_field", boom)
    await run_one(sm, handlers_for(sm), make_failure_hooks(sm))

    run, job = await state(sm, ctx, run_id, job_id)
    assert (run.status, run.summary, job.status) == (status, {"x": 1}, "succeeded")


async def test_run_mapping_failure_fails_job_and_run(sm, user_a, monkeypatch):
    ctx, run_id, job_id, _ = await queue_run(sm, user_a)

    async def broken(*a):
        raise RuntimeError("SECRET_document_text")

    monkeypatch.setattr(handlers_mod, "map_field", broken)
    handlers, hooks = handlers_for(sm), make_failure_hooks(sm)
    for _ in range(MAX_JOB_ATTEMPTS):
        await run_one(sm, handlers, hooks)

    run, job = await state(sm, ctx, run_id, job_id)
    assert (job.status, job.attempts) == ("failed", MAX_JOB_ATTEMPTS)
    assert run.status == "failed" and run.finished_at is not None
    assert run.results == {}


async def test_recovery_runs_the_hook_and_missing_run_is_ignored(sm, user_a):
    ctx, run_id, job_id, _ = await queue_run(sm, user_a)
    async with sm.begin() as s:
        await repo.update_job(s, ctx, job_id, status="running", attempts=MAX_JOB_ATTEMPTS)
    await recover_running_jobs(sm, make_failure_hooks(sm))
    run, job = await state(sm, ctx, run_id, job_id)
    assert (job.status, run.status) == ("failed", "failed")

    # a hook for a deleted run must not raise
    ghost = Job(user_id=ctx.user_id, project_id=ctx.project_id, kind="run_mapping",
                payload={"mapping_run_id": str(uuid.uuid4())})  # fmt: skip
    await make_failure_hooks(sm)["run_mapping"](ghost, ctx, "internal")
    async with sm() as s:
        assert await s.get(MappingRun, run_id) is not None


@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_failure_hook_leaves_a_finished_run_untouched(sm, user_a, status):
    ctx, run_id, job_id, _ = await queue_run(sm, user_a)
    async with sm.begin() as s:
        await repo.update_mapping_run(s, ctx, run_id, status=status)
    async with sm() as s:
        job = await s.get(Job, job_id)
    await make_failure_hooks(sm)["run_mapping"](job, ctx, "interrupted")
    run, _ = await state(sm, ctx, run_id, job_id)
    assert run.status == status and run.finished_at is None
