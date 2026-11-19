import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from structlog.testing import capture_logs

from doc_intel.core.tenancy import TenantContext
from doc_intel.db import repo
from doc_intel.db.models import Job
from doc_intel.db.session import make_engine
from doc_intel.ingestion.pipeline import ParseError
from doc_intel.jobs import worker
from doc_intel.jobs.worker import (
    MAX_JOB_ATTEMPTS,
    claim_next_job,
    recover_running_jobs,
    run_one,
    set_progress,
    start_worker,
)

pytestmark = pytest.mark.integration

SENTINEL = "SENTINEL_confidential_board_minutes"


class ParseFailed(Exception):
    code = "parse_error"


@pytest.fixture
async def sm(migrated_db):
    engine = make_engine(migrated_db)
    async with engine.begin() as conn:
        # The worker claims any tenant's job, so each test starts from an empty queue.
        await conn.execute(delete(Job))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def enqueue(sm, user_id: str, **values) -> Job:
    async with sm.begin() as s:
        project = await repo.create_project(s, user_id, "P")
        ctx = TenantContext(user_id, project.id)
        job = await repo.create_job(s, ctx, "ingest_document", {"document_id": str(uuid.uuid4())})
        for key, value in values.items():
            setattr(job, key, value)
    return job


async def load(sm, job_id) -> Job:
    async with sm() as s:
        return await s.get(Job, job_id)


async def test_success_stores_result_and_passes_the_jobs_tenant(sm, user_a):
    job = await enqueue(sm, user_a)
    seen = []

    async def handler(claimed: Job, ctx: TenantContext) -> dict:
        seen.append((claimed.id, claimed.status, ctx))
        return {"chunk_count": 3, "page_count": 1}

    assert await run_one(sm, {"ingest_document": handler}) is True
    assert seen == [(job.id, "running", TenantContext(user_a, job.project_id))]
    row = await load(sm, job.id)
    assert row.status == "succeeded"
    assert row.result == {"chunk_count": 3, "page_count": 1}
    assert row.attempts == 1
    assert row.started_at is not None and row.finished_at is not None
    assert row.locked_at is None
    assert await run_one(sm, {"ingest_document": handler}) is False


async def test_failure_retries_then_fails_without_exception_text(sm, user_a):
    job = await enqueue(sm, user_a)

    async def handler(_job, _ctx):
        raise ParseFailed(SENTINEL)

    statuses = []
    with capture_logs() as logs:
        for _ in range(MAX_JOB_ATTEMPTS):
            assert await run_one(sm, {"ingest_document": handler}) is True
            row = await load(sm, job.id)
            statuses.append((row.status, row.attempts))
    assert statuses == [("queued", 1), ("queued", 2), ("failed", 3)]
    assert row.error_code == "parse_error"
    assert row.error_message and SENTINEL not in row.error_message
    assert row.finished_at is not None and row.locked_at is None
    assert await run_one(sm, {"ingest_document": handler}) is False
    assert SENTINEL not in str(logs)
    assert [e["error_type"] for e in logs if "error_type" in e] == ["ParseFailed"] * 3


async def test_exception_without_code_fails_as_internal(sm, user_a):
    job = await enqueue(sm, user_a, attempts=MAX_JOB_ATTEMPTS - 1)

    async def handler(_job, _ctx):
        raise RuntimeError(SENTINEL)

    await run_one(sm, {"ingest_document": handler})
    row = await load(sm, job.id)
    assert (row.status, row.error_code) == ("failed", "internal")
    assert SENTINEL not in row.error_message


async def test_startup_recovery_requeues_every_running_job(sm, user_a):
    now = datetime.now(UTC)
    stale = await enqueue(
        sm, user_a, status="running", attempts=1, locked_at=now - timedelta(hours=2)
    )
    fresh = await enqueue(sm, user_a, status="running", attempts=2, locked_at=now)
    spent = await enqueue(sm, user_a, status="running", attempts=MAX_JOB_ATTEMPTS, locked_at=now)
    queued = await enqueue(sm, user_a)
    done = await enqueue(sm, user_a, status="succeeded", attempts=1)

    await recover_running_jobs(sm)

    for job in (stale, fresh):
        row = await load(sm, job.id)
        assert (row.status, row.locked_at, row.attempts) == ("queued", None, job.attempts)
    row = await load(sm, spent.id)
    assert row.status == "failed"
    assert row.error_code and row.error_message and row.finished_at is not None
    assert (await load(sm, queued.id)).status == "queued"
    assert (await load(sm, done.id)).status == "succeeded"


async def test_claim_skips_locked_rows(sm, user_a):
    first = await enqueue(sm, user_a)
    second = await enqueue(sm, user_a)
    async with sm.begin() as other:
        await other.execute(select(Job).where(Job.id == first.id).with_for_update())
        # Without SKIP LOCKED this would block until `other` commits.
        claimed = await asyncio.wait_for(claim_next_job(sm), timeout=5)
    assert claimed.id == second.id
    assert (claimed.status, claimed.attempts) == ("running", 1)
    assert claimed.locked_at is not None and claimed.started_at is not None


async def test_concurrent_claimers_never_share_a_job(sm, user_a):
    jobs = [await enqueue(sm, user_a) for _ in range(3)]
    claimed = await asyncio.gather(*(claim_next_job(sm) for _ in range(5)))
    ids = [j.id for j in claimed if j is not None]
    assert sorted(ids) == sorted(j.id for j in jobs)


async def test_progress_is_committed_while_the_handler_runs(sm, user_a):
    await enqueue(sm, user_a)
    seen = []

    async def handler(claimed: Job, ctx: TenantContext) -> dict:
        await set_progress(sm, ctx, claimed.id, {"stage": "parsing", "done": 1, "total": 2})
        seen.append((await load(sm, claimed.id)).progress)
        return {}

    await run_one(sm, {"ingest_document": handler})
    assert seen == [{"stage": "parsing", "done": 1, "total": 2}]


async def test_started_worker_recovers_runs_and_stops(sm, user_a, monkeypatch):
    monkeypatch.setattr(worker, "POLL_INTERVAL_S", 0.05)
    interrupted = await enqueue(sm, user_a, status="running", attempts=1)

    async def handler(_job, _ctx):
        return {"ok": True}

    stop = await start_worker(sm, {"ingest_document": handler})
    for _ in range(100):
        if (await load(sm, interrupted.id)).status == "succeeded":
            break
        await asyncio.sleep(0.05)
    await asyncio.wait_for(stop(), timeout=5)
    row = await load(sm, interrupted.id)
    assert (row.status, row.attempts, row.result) == ("succeeded", 2, {"ok": True})


async def test_parse_error_fails_at_once_without_retry(sm, user_a):
    # The same bytes fail the same way: retrying only keeps the single worker busy.
    job = await enqueue(sm, user_a)
    failed = []

    async def handler(_job, _ctx):
        raise ParseError()

    async def on_failed(j, _ctx, code):
        failed.append((j.id, code))

    await run_one(sm, {"ingest_document": handler}, {"ingest_document": on_failed})
    row = await load(sm, job.id)
    assert (row.status, row.attempts, row.error_code) == ("failed", 1, "parse_error")
    assert failed == [(job.id, "parse_error")]
    assert await run_one(sm, {"ingest_document": handler}) is False


async def test_retry_waits_and_stop_cuts_the_wait_short(sm, user_a, monkeypatch):
    # A short Ollama outage must not burn every attempt back to back.
    monkeypatch.setattr(worker, "RETRY_DELAY_S", 60.0)
    job = await enqueue(sm, user_a)
    calls = []

    async def handler(_job, _ctx):
        calls.append(1)
        raise RuntimeError("ollama down")

    stop = asyncio.Event()
    task = asyncio.create_task(worker.run_loop(sm, {"ingest_document": handler}, stop))
    for _ in range(100):
        if calls:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.5)
    assert len(calls) == 1
    row = await load(sm, job.id)
    assert (row.status, row.attempts) == ("queued", 1)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
