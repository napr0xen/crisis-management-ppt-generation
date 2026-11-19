"""`GET /projects/{p}/artifacts` and the mapping run `artifact` field, against real Postgres."""

import hashlib
import uuid
from urllib.parse import urlsplit

import pytest
from sqlalchemy import update

from doc_intel.core.config import get_settings
from doc_intel.db import repo
from doc_intel.db.models import Artifact
from doc_intel.ingestion.upload import artifact_key
from doc_intel.storage.local import LocalStorage
from tests.conftest import headers
from tests.integration.test_mapping_runs_api import ctx_of, seed_template_ir

pytestmark = pytest.mark.integration

PPTX_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


async def seed_run(session, ctx) -> uuid.UUID:
    ir_id = await seed_template_ir(session, ctx)
    run = await repo.create_mapping_run(
        session, ctx, template_ir_id=ir_id, document_ids=[uuid.uuid4()]
    )
    await session.commit()
    return run.id


async def seed_artifact(
    session, ctx, run_id=None, data=b"PK-artifact-bytes", fmt="pptx", filename="out.pptx", key=None
):
    """An artifacts row plus its file in storage (unless `key` points elsewhere)."""
    run_id = run_id or await seed_run(session, ctx)
    artifact_id = uuid.uuid4()
    key = key or artifact_key(ctx, artifact_id, fmt)
    LocalStorage(get_settings().storage_root).put_bytes(key, data)
    row = await repo.create_artifact(
        session,
        ctx,
        artifact_id=artifact_id,
        mapping_run_id=run_id,
        format=fmt,
        filename=filename,
        storage_key=key,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )
    await session.commit()
    return row


def path_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}"


async def test_list_is_newest_first_with_signed_working_urls(client, session, user_a, project):
    ctx = ctx_of(user_a, project)
    first = await seed_artifact(session, ctx, data=b"first")
    second = await seed_artifact(session, ctx, data=b"second")

    r = client.get(f"/v1/projects/{project}/artifacts", headers=headers(user_a))
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["id"] for i in items] == [str(second.id), str(first.id)]
    assert set(items[0]) == {
        "id",
        "mapping_run_id",
        "format",
        "filename",
        "size_bytes",
        "download_url",
        "created_at",
    }
    assert items[0]["size_bytes"] == len(b"second")
    for item, body in zip(items, [b"second", b"first"], strict=True):
        assert item["download_url"].startswith(
            f"{get_settings().public_base_url}/v1/artifacts/{item['id']}/download?exp="
        )
        got = client.get(path_of(item["download_url"]))  # no bearer
        assert got.status_code == 200
        assert got.content == body
        assert got.headers["content-type"] == PPTX_TYPE


@pytest.mark.parametrize("limit, status", [(0, 422), (201, 422), (1, 200), (200, 200)])
def test_limit_bounds(client, user_a, project, limit, status):
    r = client.get(f"/v1/projects/{project}/artifacts?limit={limit}", headers=headers(user_a))
    assert r.status_code == status


async def test_limit_caps_the_list(client, session, user_a, project):
    ctx = ctx_of(user_a, project)
    await seed_artifact(session, ctx)
    newest = await seed_artifact(session, ctx)
    r = client.get(f"/v1/projects/{project}/artifacts?limit=1", headers=headers(user_a))
    assert [i["id"] for i in r.json()["items"]] == [str(newest.id)]


async def test_other_users_project_is_404(client, session, user_a, user_b, project):
    await seed_artifact(session, ctx_of(user_a, project))
    r = client.get(f"/v1/projects/{project}/artifacts", headers=headers(user_b))
    unknown = client.get(f"/v1/projects/{uuid.uuid4()}/artifacts", headers=headers(user_b))
    assert r.status_code == unknown.status_code == 404
    assert r.json()["error"]["code"] == unknown.json()["error"]["code"] == "not_found"


async def test_artifact_of_another_project_is_not_listed(client, session, user_a, project):
    other = client.post("/v1/projects", json={"name": "O"}, headers=headers(user_a)).json()["id"]
    await seed_artifact(session, ctx_of(user_a, other))
    mine = await seed_artifact(session, ctx_of(user_a, project))

    r = client.get(f"/v1/projects/{project}/artifacts", headers=headers(user_a))
    assert [i["id"] for i in r.json()["items"]] == [str(mine.id)]


async def test_mapping_run_shows_artifact_once_rendered(client, session, user_a, project):
    ctx = ctx_of(user_a, project)
    run_id = await seed_run(session, ctx)
    url = f"/v1/projects/{project}/mapping-runs/{run_id}"
    assert client.get(url, headers=headers(user_a)).json()["artifact"] is None

    artifact = await seed_artifact(session, ctx, run_id=run_id)
    shown = client.get(url, headers=headers(user_a)).json()["artifact"]
    assert shown["id"] == str(artifact.id)
    assert shown["mapping_run_id"] == str(run_id)
    assert "sha256" not in shown and "storage_key" not in shown
    assert client.get(path_of(shown["download_url"])).status_code == 200


async def test_newest_first_breaks_created_at_ties_by_id(client, session, user_a, project):
    ctx = ctx_of(user_a, project)
    rows = [await seed_artifact(session, ctx) for _ in range(3)]
    await session.execute(update(Artifact).values(created_at=rows[0].created_at))
    await session.commit()
    r = client.get(f"/v1/projects/{project}/artifacts", headers=headers(user_a))
    assert [i["id"] for i in r.json()["items"]] == sorted((str(a.id) for a in rows), reverse=True)


def test_trailing_slash_in_public_base_url_gives_no_double_slash(monkeypatch):
    from datetime import UTC, datetime

    from doc_intel.api.v1.artifacts import artifact_out
    from doc_intel.db.models import Artifact

    settings = get_settings().model_copy(update={"public_base_url": "http://host:8000/"})
    row = Artifact(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        mapping_run_id=uuid.uuid4(),
        format="pptx",
        filename="a.pptx",
        size_bytes=1,
        created_at=datetime.now(UTC),
    )
    url = artifact_out(row, settings).download_url
    assert url.startswith(f"http://host:8000/v1/artifacts/{row.id}/download?")


async def test_download_of_a_row_without_a_file_is_404(client, session, user_a, project):
    ctx = ctx_of(user_a, project)
    row = await seed_artifact(session, ctx, key=artifact_key(ctx, uuid.uuid4(), "pptx"))
    LocalStorage(get_settings().storage_root).delete(row.storage_key)
    r = client.get(f"/v1/projects/{project}/artifacts", headers=headers(user_a))
    assert client.get(path_of(r.json()["items"][0]["download_url"])).status_code == 404


async def test_download_with_an_invalid_storage_key_is_404(client, session, user_a, project):
    ctx = ctx_of(user_a, project)
    row = await seed_artifact(session, ctx)
    row.storage_key = "../../etc/passwd"
    await session.commit()
    r = client.get(f"/v1/projects/{project}/artifacts", headers=headers(user_a))
    assert client.get(path_of(r.json()["items"][0]["download_url"])).status_code == 404
