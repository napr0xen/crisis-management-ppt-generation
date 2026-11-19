"""Postgres-backed job queue, one in-process worker (ADR-010).

Each state transition commits in its own transaction; handlers run outside them. The
sessionmaker must use `expire_on_commit=False` (as `main.py` does) so a claimed `Job` stays
readable after its claim transaction closes.
"""

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_intel.core.tenancy import TenantContext
from doc_intel.db import repo
from doc_intel.db.models import Job

MAX_JOB_ATTEMPTS = 3
POLL_INTERVAL_S = 1.0
# Wait RETRY_DELAY_S x attempts before the next try, so a short Ollama outage doesn't use up
# every attempt at once. The single worker idles meanwhile; every other job needs Ollama too.
RETRY_DELAY_S = 30.0

Handler = Callable[[Job, TenantContext], Awaitable[dict[str, Any]]]
# Called once when a job is failed for good (last attempt, or interrupted at startup), so the kind
# can mark what it owns (e.g. the document) failed too: (job, tenant, error_code).
OnFailed = Callable[[Job, TenantContext, str], Awaitable[None]]
Sessionmaker = async_sessionmaker[AsyncSession]

log = structlog.get_logger()


async def recover_running_jobs(
    sessionmaker: Sessionmaker, on_failed: dict[str, OnFailed] | None = None
) -> None:
    """At startup nothing can still be running (the only worker lives in this process), so every
    `running` job is re-queued, or failed once it has used all its attempts."""
    # Queue exception to the tenant rule: recovery spans all tenants and touches only queue state.
    async with sessionmaker.begin() as session:
        failed = await session.scalars(
            update(Job)
            .where(Job.status == "running", Job.attempts >= MAX_JOB_ATTEMPTS)
            .values(
                status="failed",
                error_code="interrupted",
                error_message=f"Job was interrupted {MAX_JOB_ATTEMPTS} times.",
                locked_at=None,
                finished_at=func.now(),
            )
            .returning(Job)
        )
        failed = list(failed)
        await session.execute(
            update(Job).where(Job.status == "running").values(status="queued", locked_at=None)
        )
    for job in failed:
        await _notify_failed(on_failed, job, "interrupted")


async def _notify_failed(on_failed: dict[str, OnFailed] | None, job: Job, code: str) -> None:
    if hook := (on_failed or {}).get(job.kind):
        await hook(job, TenantContext(job.user_id, job.project_id), code)


async def claim_next_job(sessionmaker: Sessionmaker) -> Job | None:
    """Claim the oldest queued job of any tenant and commit the claim."""
    # Queue exception to the tenant rule: the claim spans all tenants; after it, everything
    # uses the job's own tenant.
    oldest_queued = (
        select(Job.id)
        .where(Job.status == "queued")
        .order_by(Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    async with sessionmaker.begin() as session:
        return await session.scalar(
            update(Job)
            .where(Job.id == oldest_queued)
            .values(
                status="running",
                attempts=Job.attempts + 1,
                locked_at=func.now(),
                started_at=func.now(),
            )
            .returning(Job)
        )


async def set_progress(
    sessionmaker: Sessionmaker, ctx: TenantContext, job_id: uuid.UUID, progress: dict[str, Any]
) -> None:
    """For handlers: commit `jobs.progress` now, so pollers see it while the job runs."""
    async with sessionmaker.begin() as session:
        await repo.update_job(session, ctx, job_id, progress=progress)


async def run_one(
    sessionmaker: Sessionmaker,
    handlers: dict[str, Handler],
    on_failed: dict[str, OnFailed] | None = None,
    stop: asyncio.Event | None = None,
) -> bool:
    """Claim and run one job. Returns False when the queue is empty.

    With `stop` (the worker loop), a failed attempt waits before its retry; `stop` cuts it short.
    """
    job = await claim_next_job(sessionmaker)
    if job is None:
        return False
    ctx = TenantContext(job.user_id, job.project_id)
    bound = log.bind(job_id=str(job.id), kind=job.kind, attempt=job.attempts)
    bound.info("job_started")
    try:
        result = await handlers[job.kind](job, ctx)
    except Exception as exc:
        # Only the type: exception text may carry document content.
        bound.error("job_attempt_failed", error_type=type(exc).__name__)
        code = getattr(exc, "code", None)
        code = code if isinstance(code, str) else "internal"
        final = job.attempts >= MAX_JOB_ATTEMPTS or getattr(exc, "retry", True) is False
        async with sessionmaker.begin() as session:
            if final:
                await repo.update_job(
                    session,
                    ctx,
                    job.id,
                    status="failed",
                    error_code=code,
                    error_message=f"Job failed after {job.attempts} attempt(s) ({code}).",
                    locked_at=None,
                    finished_at=func.now(),
                )
            else:
                await repo.update_job(session, ctx, job.id, status="queued", locked_at=None)
        if final:
            await _notify_failed(on_failed, job, code)
        elif stop is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), RETRY_DELAY_S * job.attempts)
        return True
    async with sessionmaker.begin() as session:
        await repo.update_job(
            session,
            ctx,
            job.id,
            status="succeeded",
            result=result,
            locked_at=None,
            finished_at=func.now(),
        )
    bound.info("job_succeeded")
    return True


async def run_loop(
    sessionmaker: Sessionmaker,
    handlers: dict[str, Handler],
    stop: asyncio.Event,
    on_failed: dict[str, OnFailed] | None = None,
) -> None:
    """Run jobs one at a time (JOB_CONCURRENCY=1) until `stop` is set; poll when idle."""
    while not stop.is_set():
        try:
            worked = await run_one(sessionmaker, handlers, on_failed, stop)
        except Exception as exc:
            # e.g. Postgres down mid-transition: the job stays `running` until the next startup.
            log.error("worker_error", error_type=type(exc).__name__)
            worked = False
        if not worked:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), POLL_INTERVAL_S)


async def start_worker(
    sessionmaker: Sessionmaker,
    handlers: dict[str, Handler],
    on_failed: dict[str, OnFailed] | None = None,
) -> Callable[[], Awaitable[None]]:
    """Recover interrupted jobs, start the loop, and return the coroutine function that stops it."""
    await recover_running_jobs(sessionmaker, on_failed)
    stop = asyncio.Event()
    task = asyncio.create_task(run_loop(sessionmaker, handlers, stop, on_failed))

    async def stop_worker() -> None:
        # shortcut: waits for the running job to finish, cancel it instead if shutdowns get slow.
        stop.set()
        await task

    return stop_worker
