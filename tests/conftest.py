"""Shared fixtures.

Unit tests need no services. Integration/security tests that touch the DB use a throwaway
`doc_intel_test` database on the Compose Postgres, migrated with Alembic, so dev data is untouched.
"""

import atexit
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest
import structlog

ROOT = Path(__file__).resolve().parents[1]

# Settings must be importable in every test, so give the required secrets test values.
# Secrets need >= 32 characters (core/config.py).
PIPE_SECRET = "test-pipe-secret-0123456789abcdef"
os.environ.setdefault("PIPE_SHARED_SECRET", PIPE_SECRET)
os.environ.setdefault("SIGNING_SECRET", "test-signing-secret-0123456789abcdef")


@pytest.fixture(autouse=True)
def _reset_templates_dir():
    yield
    # an upload test creates the folder: drop it so the next test starts without one
    shutil.rmtree(os.environ["TEMPLATES_DIR"], ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_structlog():
    yield
    # create_app/configure_logging bind the test's captured stdout, closed once the test ends.
    structlog.reset_defaults()


def _dev_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


DEV_ENV = _dev_env()
DEV_DATABASE_URL = os.environ.get("DATABASE_URL") or DEV_ENV.get(
    "DATABASE_URL", "postgresql+asyncpg://doc_intel:doc_intel@127.0.0.1:5432/doc_intel"
)
# TEST_RUN_ID gives parallel pytest runs their own DB and collection (default: shared names).
_RUN_SUFFIX = f"_{os.environ['TEST_RUN_ID']}" if os.environ.get("TEST_RUN_ID") else ""
TEST_DB_NAME = f"doc_intel_test{_RUN_SUFFIX}"
TEST_DATABASE_URL = DEV_DATABASE_URL.rsplit("/", 1)[0] + f"/{TEST_DB_NAME}"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("QDRANT_URL", DEV_ENV.get("QDRANT_URL", "http://127.0.0.1:6333"))
# Tests never write into ./data.
# Tests drive jobs with `run_one`; a background worker would race them for queued jobs.
os.environ["JOB_CONCURRENCY"] = "0"
# Like the throwaway test DB: tests never touch the dev `doc_chunks` collection.
os.environ["QDRANT_COLLECTION"] = f"doc_chunks_test{_RUN_SUFFIX}"
os.environ["STORAGE_ROOT"] = tempfile.mkdtemp(prefix="doc_intel_test_storage_")
atexit.register(shutil.rmtree, os.environ["STORAGE_ROOT"], ignore_errors=True)

# A folder that never exists: app startups skip the templates/ sync. Tests of the sync, or that
# save uploads, point the setting at their own tmp dir.
os.environ["TEMPLATES_DIR"] = str(Path(tempfile.mkdtemp(prefix="doc_intel_test_tpl_")) / "absent")
atexit.register(shutil.rmtree, Path(os.environ["TEMPLATES_DIR"]).parent, ignore_errors=True)

AUTH = {"Authorization": f"Bearer {PIPE_SECRET}"}


def qdrant_api_key() -> str | None:
    from doc_intel.core.config import get_settings

    key = get_settings().qdrant_api_key
    return (key.get_secret_value() or None) if key else None


def headers(user_id: str) -> dict[str, str]:
    return {**AUTH, "X-OWUI-User-Id": user_id}


@pytest.fixture
def one_ir_batch(monkeypatch):
    """One IR batch per template: one scripted FakeLLM reply then covers a multi-slide fixture."""
    monkeypatch.setattr(
        "doc_intel.templates.ir_builder._batches", lambda amap: [(None, list(amap.anchors))]
    )


@pytest.fixture
def user_a() -> str:
    return f"user_a_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def user_b() -> str:
    return f"user_b_{uuid.uuid4().hex[:8]}"


async def _recreate_test_db() -> None:
    import asyncpg

    admin_dsn = DEV_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        await conn.close()


def alembic_config():
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


@pytest.fixture(scope="session")
def migrated_db() -> str:
    """Fresh test database at Alembic head. Session-scoped: created once per run."""
    import asyncio

    from alembic import command

    asyncio.run(_recreate_test_db())
    command.upgrade(alembic_config(), "head")
    return TEST_DATABASE_URL


@pytest.fixture
async def session(migrated_db):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(migrated_db)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def client(migrated_db):
    from fastapi.testclient import TestClient

    from doc_intel.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def project(client, user_a) -> str:
    """A project owned by `user_a`; returns its id."""
    return client.post("/v1/projects", json={"name": "P"}, headers=headers(user_a)).json()["id"]


@pytest.fixture
async def empty_queue(migrated_db):
    """The worker claims any tenant's oldest job: worker-driven tests start from an empty queue."""
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import create_async_engine

    from doc_intel.db.models import Job

    engine = create_async_engine(migrated_db)
    async with engine.begin() as conn:
        await conn.execute(delete(Job))
    await engine.dispose()


@pytest.fixture
async def clean_library(migrated_db):
    """The library catalog is global: empty it before and after, so no test sees another's."""
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import create_async_engine

    from doc_intel.db.models import LibraryTemplate

    engine = create_async_engine(migrated_db)
    for phase in ("before", "after"):
        async with engine.begin() as conn:
            await conn.execute(delete(LibraryTemplate))
        if phase == "before":
            yield
    await engine.dispose()


@pytest.fixture(scope="session")
def qdrant_collection() -> str:
    """Fresh `doc_chunks_test` collection, recreated once per run. Never the dev collection."""
    import asyncio

    from qdrant_client import AsyncQdrantClient

    from doc_intel.core.config import get_settings
    from doc_intel.vector.qdrant_store import QdrantStore

    settings = get_settings()
    assert settings.qdrant_collection == f"doc_chunks_test{_RUN_SUFFIX}"

    async def recreate() -> None:
        client = AsyncQdrantClient(url=settings.qdrant_url, api_key=qdrant_api_key())
        try:
            await client.delete_collection(settings.qdrant_collection)
            await QdrantStore(client, settings.qdrant_collection).ensure_collection()
        finally:
            await client.close()

    asyncio.run(recreate())
    return settings.qdrant_collection


@pytest.fixture
async def vector_store(qdrant_collection):
    """A `QdrantStore` on the test collection; tests isolate themselves with random tenants."""
    from qdrant_client import AsyncQdrantClient

    from doc_intel.core.config import get_settings
    from doc_intel.vector.qdrant_store import QdrantStore

    client = AsyncQdrantClient(url=get_settings().qdrant_url, api_key=qdrant_api_key())
    yield QdrantStore(client, qdrant_collection)
    await client.close()
