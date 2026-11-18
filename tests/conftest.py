"""Shared fixtures.

Unit tests need no services. Integration/security tests that touch the DB use a throwaway
`doc_intel_test` database on the Compose Postgres, migrated with Alembic, so dev data is untouched.
"""

import os
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Settings must be importable in every test, so give the required secrets test values.
os.environ.setdefault("PIPE_SHARED_SECRET", "test-pipe-secret")
os.environ.setdefault("SIGNING_SECRET", "test-signing-secret")


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
TEST_DB_NAME = "doc_intel_test"
TEST_DATABASE_URL = DEV_DATABASE_URL.rsplit("/", 1)[0] + f"/{TEST_DB_NAME}"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("QDRANT_URL", DEV_ENV.get("QDRANT_URL", "http://127.0.0.1:6333"))

AUTH = {"Authorization": "Bearer test-pipe-secret"}


def headers(user_id: str) -> dict[str, str]:
    return {**AUTH, "X-OWUI-User-Id": user_id}


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
