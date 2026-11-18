"""One transaction per request: repo functions never commit on their own."""

import pytest
from sqlalchemy import select

from doc_intel.core.errors import NotFound
from doc_intel.core.security import UserId
from doc_intel.db import repo
from doc_intel.db.models import Project
from doc_intel.db.session import Session
from tests.conftest import headers

pytestmark = pytest.mark.integration


async def test_repo_does_not_commit(session, user_a):
    await repo.create_project(session, user_a, "rolled back")
    await session.rollback()
    rows = await session.execute(select(Project).where(Project.user_id == user_a))
    assert rows.all() == []


def test_request_that_fails_after_a_write_persists_nothing(client, user_a):
    async def write_then_fail(user_id: UserId, session: Session):
        await repo.create_project(session, user_id, "half-done")
        raise NotFound()

    client.app.add_api_route("/v1/write-then-fail", write_then_fail, methods=["POST"])
    assert client.post("/v1/write-then-fail", headers=headers(user_a)).status_code == 404
    assert client.get("/v1/projects", headers=headers(user_a)).json()["items"] == []


def test_successful_request_commits(client, user_a):
    pid = client.post("/v1/projects", json={"name": "kept"}, headers=headers(user_a)).json()["id"]
    assert client.get(f"/v1/projects/{pid}", headers=headers(user_a)).status_code == 200
