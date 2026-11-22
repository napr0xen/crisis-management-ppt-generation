import asyncio
import hashlib
import threading
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from structlog.testing import capture_logs

from doc_intel.core.config import get_settings
from doc_intel.db import repo
from doc_intel.db.models import Artifact, Job, Project
from doc_intel.db.session import make_engine
from doc_intel.ingestion.upload import artifact_key
from doc_intel.jobs import handlers as handlers_mod
from doc_intel.jobs.handlers import _ARTIFACT_NS, make_failure_hooks
from doc_intel.jobs.worker import run_one
from doc_intel.mapping.schemas import MappingWarning
from doc_intel.storage.local import LocalStorage
from tests.integration.test_run_mapping_job import (
    handlers_for,
    missing,
    queue_run,
    state,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("empty_queue")]

DATA = b"PK\x03\x04fake"


@pytest.fixture
async def sm(migrated_db):
    engine = make_engine(migrated_db)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def fake(monkeypatch):
    """Records writers.render calls; tests may set `.warnings` and `.hook`."""

    class Fake:
        def __init__(self):
            self.warnings = [MappingWarning(field_id="f_one", code="table_overflow")]
            self.hook = None
            self.calls, self.threads = [], []

        def __call__(self, fmt, template, fields, results, *, missing_marker, conflict_marker):
            self.threads.append(threading.current_thread())
            self.calls.append((fmt, template, [f.field_id for f in fields], sorted(results)))
            if self.hook:
                self.hook()
            return DATA, self.warnings

    f = Fake()
    monkeypatch.setattr("doc_intel.writers.render", f)
    return f


async def prepared_run(sm, user, status="verifying", name=None):
    """A run whose mapping is committed; its queued run_mapping job is removed."""
    ctx, run_id, job_id, docs = await queue_run(sm, user)
    async with sm.begin() as s:
        run = await repo.get_mapping_run(s, ctx, run_id)
        ir = await repo.get_template_ir_by_id(s, ctx, run.template_ir_id)
        template = await repo.get_template(s, ctx, ir.template_id)
        if name:
            (await repo.get_document(s, ctx, template.document_id)).filename = name
        results = {fid: missing(_Field(fid), docs).model_dump(mode="json") for fid in FIELDS}
        summary = {"populated": 0, "partial": 0, "missing": 2, "conflict": 0}
        summary["warnings"] = [{"field_id": "f_two", "code": "formula_cell"}]
        await repo.update_mapping_run(
            s, ctx, run_id, status=status, results=results, summary=summary
        )
        await s.execute(delete(Job).where(Job.id == job_id))
    return ctx, run_id


class _Field:
    def __init__(self, field_id):
        self.field_id = field_id


FIELDS = ("f_one", "f_two")


async def run_render(sm, ctx, run_id):
    async with sm.begin() as s:
        job = await repo.create_job(s, ctx, "render_artifact", {"mapping_run_id": str(run_id)})
    handlers, hooks = handlers_for(sm), make_failure_hooks(sm)
    await run_one(sm, handlers, hooks)
    async with sm() as s:
        return await s.get(Job, job.id)


async def artifact_of(sm, ctx, run_id):
    async with sm() as s:
        return await repo.get_artifact_by_run(s, ctx, run_id)


def stored(key):
    with LocalStorage(get_settings().storage_root).open_read(key) as f:
        return f.read()


async def test_run_mapping_renders_artifact_with_merged_warnings(sm, user_a, fake, monkeypatch):
    ctx, run_id, job_id, _ = await queue_run(sm, user_a)

    async def fake_map(c, field, document_ids, retriever, llm, other_headings=()):
        return missing(field, document_ids)

    monkeypatch.setattr(handlers_mod, "map_field", fake_map)
    await run_one(sm, handlers_for(sm), make_failure_hooks(sm))

    run, job = await state(sm, ctx, run_id, job_id)
    art = await artifact_of(sm, ctx, run_id)
    assert run.status == "completed" and run.finished_at is not None
    assert job.result == {"mapping_run_id": str(run_id), "artifact_id": str(art.id)}
    assert run.summary["warnings"] == [{"field_id": "f_one", "code": "table_overflow"}]
    assert fake.calls == [("pptx", b"TEMPLATE", list(FIELDS), list(FIELDS))]


async def test_render_artifact_completes_a_verifying_run(sm, user_a, fake):
    ctx, run_id = await prepared_run(sm, user_a, name="Crisis Plan.pptx")
    job = await run_render(sm, ctx, run_id)

    run, _ = await state(sm, ctx, run_id, job.id)
    art = await artifact_of(sm, ctx, run_id)
    assert job.status == "succeeded" and job.result == {"artifact_id": str(art.id)}
    assert run.status == "completed" and run.finished_at is not None
    assert run.summary["warnings"] == [
        {"field_id": "f_two", "code": "formula_cell"},
        {"field_id": "f_one", "code": "table_overflow"},
    ]
    assert art.format == "pptx" and art.filename == f"Crisis_Plan_v{run.version}.pptx"
    assert art.storage_key == artifact_key(ctx, art.id, "pptx")
    assert art.storage_key.startswith(f"users/{ctx.user_id}/projects/{ctx.project_id}/artifacts/")
    assert stored(art.storage_key) == DATA
    assert (art.sha256, art.size_bytes) == (hashlib.sha256(DATA).hexdigest(), len(DATA))


async def test_render_runs_off_the_event_loop(sm, user_a, fake):
    ctx, run_id = await prepared_run(sm, user_a)
    await run_render(sm, ctx, run_id)
    assert fake.threads and fake.threads[0] is not threading.main_thread()


async def test_retry_on_a_completed_run_returns_the_same_artifact(sm, user_a, fake):
    ctx, run_id = await prepared_run(sm, user_a)
    first = await run_render(sm, ctx, run_id)
    second = await run_render(sm, ctx, run_id)

    assert second.result == first.result and len(fake.calls) == 1
    async with sm() as s:
        assert (
            len((await s.scalars(select(Artifact).where(Artifact.user_id == ctx.user_id))).all())
            == 1
        )


async def test_retry_after_a_crash_reuses_the_file_on_disk(sm, user_a, fake):
    ctx, run_id = await prepared_run(sm, user_a, status="rendering")
    aid = uuid.uuid5(_ARTIFACT_NS, str(run_id))
    key = artifact_key(ctx, aid, "pptx")
    leftover = b"PK\x03\x04crashed-attempt"
    LocalStorage(get_settings().storage_root).put_bytes(key, leftover)

    job = await run_render(sm, ctx, run_id)

    art = await artifact_of(sm, ctx, run_id)
    assert job.status == "succeeded" and art.id == aid
    assert art.sha256 == hashlib.sha256(leftover).hexdigest() and art.size_bytes == len(leftover)
    assert stored(key) == leftover


async def test_render_artifact_on_a_queued_run_fails_without_retry_or_killing_it(sm, user_a, fake):
    ctx, run_id = await prepared_run(sm, user_a, status="queued")
    job = await run_render(sm, ctx, run_id)

    run, _ = await state(sm, ctx, run_id, job.id)
    assert (job.status, job.attempts, job.error_code) == ("failed", 1, "conflict")
    assert run.status == "queued" and run.finished_at is None  # R2: the mapping run lives on
    assert fake.calls == [] and await artifact_of(sm, ctx, run_id) is None


async def test_project_deleted_mid_render_leaves_no_artifact_or_file(sm, migrated_db, user_a, fake):
    ctx, run_id = await prepared_run(sm, user_a)

    def drop_project():
        # runs in the writer thread: a private loop needs its own engine
        async def go():
            engine = make_engine(migrated_db)
            async with engine.begin() as conn:
                await conn.execute(delete(Project).where(Project.id == ctx.project_id))
            await engine.dispose()

        asyncio.run(go())

    fake.hook = drop_project
    # called directly: the cascade also deletes the job row the worker would update
    job = Job(
        user_id=ctx.user_id,
        project_id=ctx.project_id,
        kind="render_artifact",
        payload={"mapping_run_id": str(run_id)},
    )
    assert await handlers_for(sm)["render_artifact"](job, ctx) == {"artifact_id": None}

    async with sm() as s:
        assert (
            await s.scalars(select(Artifact).where(Artifact.user_id == ctx.user_id))
        ).all() == []
    key = artifact_key(ctx, uuid.uuid5(_ARTIFACT_NS, str(run_id)), "pptx")
    with pytest.raises(FileNotFoundError):
        stored(key)


@pytest.mark.parametrize("status", ["rendering", "verifying"])
async def test_failure_hook_fails_an_unfinished_run(sm, user_a, status):
    ctx, run_id = await prepared_run(sm, user_a, status=status)
    async with sm.begin() as s:
        job = await repo.create_job(s, ctx, "render_artifact", {"mapping_run_id": str(run_id)})
    await make_failure_hooks(sm)["render_artifact"](job, ctx, "internal")
    run, _ = await state(sm, ctx, run_id, job.id)
    assert run.status == "failed" and run.finished_at is not None


@pytest.mark.parametrize("status", ["queued", "extracting"])
async def test_render_failure_hook_leaves_a_still_mapping_run(sm, user_a, status):
    ctx, run_id = await prepared_run(sm, user_a, status=status)
    async with sm.begin() as s:
        job = await repo.create_job(s, ctx, "render_artifact", {"mapping_run_id": str(run_id)})
    await make_failure_hooks(sm)["render_artifact"](job, ctx, "internal")
    run, _ = await state(sm, ctx, run_id, job.id)
    assert run.status == status and run.finished_at is None


@pytest.mark.parametrize("exc", [KeyError("secret text"), TypeError("x"), ValueError("x")])
async def test_deterministic_writer_failure_is_not_retried(sm, user_a, fake, exc):
    ctx, run_id = await prepared_run(sm, user_a)

    def boom():
        raise exc

    fake.hook = boom
    job = await run_render(sm, ctx, run_id)
    assert (job.status, job.attempts) == ("failed", 1)
    assert "secret text" not in (job.error_message or "")
    run, _ = await state(sm, ctx, run_id, job.id)
    assert run.status == "failed"


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (KeyError("anchor_unresolved"), "anchor_unresolved"),
        (KeyError("secret text"), None),
        (ValueError("secret text"), None),
    ],
)
async def test_render_failure_logs_only_type_and_allowlisted_code(sm, user_a, fake, exc, code):
    ctx, run_id = await prepared_run(sm, user_a)

    def boom():
        raise exc

    fake.hook = boom
    with capture_logs() as logs:
        await run_render(sm, ctx, run_id)
    (event,) = [e for e in logs if e["event"] == "render_failed"]
    assert event["error_type"] == type(exc).__name__ and event["code"] == code
    assert event["mapping_run_id"] == str(run_id) and event["format"] == "pptx"
    assert "secret text" not in str(logs)


async def test_failure_hook_leaves_a_completed_run(sm, user_a):
    ctx, run_id = await prepared_run(sm, user_a, status="completed")
    async with sm.begin() as s:
        job = await repo.create_job(s, ctx, "render_artifact", {"mapping_run_id": str(run_id)})
    await make_failure_hooks(sm)["render_artifact"](job, ctx, "internal")
    run, _ = await state(sm, ctx, run_id, job.id)
    assert run.status == "completed" and run.finished_at is None


@pytest.mark.parametrize(
    ("name", "stem"),
    [
        ("../evil\r\nname.pptx", "evil__name"),
        ("Ünïcode Plán.pptx", "n_code_Pl_n"),
        ("\u00e9\u00e9.pptx", "artifact"),
        (".hidden.pptx", "hidden"),
        ("a" * 300 + ".pptx", "a" * 100),
    ],
)
async def test_filename_is_sanitized(sm, user_a, fake, name, stem):
    ctx, run_id = await prepared_run(sm, user_a, name=name)
    await run_render(sm, ctx, run_id)
    art = await artifact_of(sm, ctx, run_id)
    assert art.filename == f"{stem}_v1.pptx"
