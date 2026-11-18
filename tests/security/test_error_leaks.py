"""Crashes must never put document data into logs or responses (CLAUDE.md guardrail #6)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from doc_intel.core.config import Settings
from doc_intel.db import repo
from doc_intel.db.models import Document
from doc_intel.db.session import make_engine
from doc_intel.main import create_app

SENTINEL = "SENTINEL_quarterly_losses_2026.pdf"


def test_unhandled_error_returns_envelope_and_is_not_reraised(capsys):
    app = create_app(
        Settings(_env_file=None, database_url="postgresql+asyncpg://x:x@127.0.0.1:1/x")
    )

    async def boom():
        raise RuntimeError(SENTINEL)

    app.add_api_route("/v1/boom", boom)
    # raise_server_exceptions=True: if the error escaped to the server, this call would raise.
    with TestClient(app) as client:
        r = client.get("/v1/boom", headers={"X-Request-Id": "req-boom"})
    assert r.status_code == 500
    assert r.headers["X-Request-Id"] == "req-boom"
    assert r.json() == {
        "error": {"code": "internal", "message": "Internal error.", "request_id": "req-boom"}
    }
    out = capsys.readouterr()
    assert SENTINEL not in out.out + out.err
    assert "RuntimeError" in out.out  # the error type is still logged


@pytest.mark.integration
async def test_db_errors_hide_bound_parameters(migrated_db, user_a, user_b):
    engine = make_engine(migrated_db)
    async with async_sessionmaker(engine)() as session:
        project = await repo.create_project(session, user_a, "A")
        session.add(
            Document(
                project_id=project.id,
                user_id=user_b,  # tenant mismatch -> FK violation
                filename=SENTINEL,
                mime_type="application/pdf",
                size_bytes=1,
                sha256="0" * 64,
                storage_key="k",
                status="uploaded",
            )
        )
        with pytest.raises(IntegrityError) as exc:
            await session.flush()
    await engine.dispose()
    assert SENTINEL not in str(exc.value)
