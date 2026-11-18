import asyncio

import pytest
from alembic import command
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import alembic_config

pytestmark = pytest.mark.integration

TABLES = {"projects", "documents", "jobs", "templates", "template_irs", "mapping_runs", "artifacts"}


async def _tables(url: str) -> dict[str, set[str]]:
    engine = create_async_engine(url)
    async with engine.connect() as conn:

        def read(sync_conn):
            insp = inspect(sync_conn)
            return {t: {c["name"] for c in insp.get_columns(t)} for t in insp.get_table_names()}

        tables = await conn.run_sync(read)
    await engine.dispose()
    return tables


def test_downgrade_and_upgrade_round_trip(migrated_db):
    cfg = alembic_config()
    command.downgrade(cfg, "base")
    assert set(asyncio.run(_tables(migrated_db))) <= {"alembic_version"}
    command.upgrade(cfg, "head")
    tables = asyncio.run(_tables(migrated_db))
    assert TABLES <= set(tables)


def test_columns_match_data_model(migrated_db):
    tables = asyncio.run(_tables(migrated_db))
    assert "max_attempts" not in tables["jobs"]
    assert "openwebui_file_ids" in tables["documents"]
    assert "openwebui_file_id" not in tables["documents"]
    tenant = {"user_id", "project_id"}
    for t in TABLES - {"projects"}:
        assert tenant <= tables[t], t
