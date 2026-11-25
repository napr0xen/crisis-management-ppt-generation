"""`GET .../conflicts` and `POST .../resolve-conflicts` against real Postgres (Phase 19)."""

import uuid

import pytest
from sqlalchemy import func, select

from doc_intel.api.v1 import mapping_runs
from doc_intel.core.config import get_settings
from doc_intel.db import repo
from doc_intel.db.models import MappingRun
from doc_intel.main import create_app
from doc_intel.mapping.schemas import Candidate, ConflictDetails, Evidence, FieldMappingResult
from doc_intel.storage.local import LocalStorage
from tests.conftest import headers
from tests.integration.test_mapping_runs_api import (
    ctx_of,
    field,
    seed_document,
    seed_template_ir,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("empty_queue")]

DOC = uuid.uuid4()


def _evidence(filename, page, quote) -> Evidence:
    return Evidence(
        document_id=DOC,
        chunk_id=uuid.uuid4(),
        filename=filename,
        page_number=page,
        section_heading=None,
        quote=quote,
    )


def _result(fid, status="missing", **kw) -> dict:
    base = {
        "field_id": fid,
        "status": status,
        "value": None,
        "evidence": [],
        "confidence": 0.0,
        "missing_reason": "not_found_by_llm",
        "checked_documents": [DOC],
        "conflict_details": None,
        "provenance": "rag",
    }
    return FieldMappingResult(**(base | kw)).model_dump(mode="json")


def _conflict(fid, values=("first", "second")) -> dict:
    cands = [
        Candidate(value=v, evidence=[_evidence(f"{v}.pdf", n + 3, f"the {v} passage")])
        for n, v in enumerate(values)
    ]
    return _result(
        fid, "conflict", missing_reason=None, conflict_details=ConflictDetails(candidates=cands)
    )


def _fields() -> list[dict]:
    # `title` and `title_copy` are one shared fact (same label and type, other slides)
    return [
        field("title") | {"anchor_id": "pptx:s2:sp3", "section_path": ["Slide 2", "Risks", "Box"]},
        field("title_copy")
        | {"anchor_id": "pptx:s5:sp1", "label": "Title", "section_path": ["Slide 5", "Box"]},
        field("owner") | {"anchor_id": "pptx:s9:sp1", "context": "Card A"},
        field("plain") | {"section_path": ["Doc", "Sub"]},
        field("note"),
    ]


async def seed_parent(session, user, project, status="completed"):
    ctx = ctx_of(user, project)
    ir_id = await seed_template_ir(session, ctx, _fields())
    await seed_document(session, ctx)
    results = {
        "title": _conflict("title"),
        "title_copy": _conflict("title_copy"),
        "owner": _conflict("owner", ("a", "b", "c")),
        "plain": _conflict("plain"),
        "note": _result("note"),
    }
    parent = await repo.create_mapping_run(
        session,
        ctx,
        template_ir_id=ir_id,
        document_ids=[DOC],
        status=status,
        results=results,
        summary={"populated": 0, "partial": 0, "missing": 1, "conflict": 4},
    )
    await session.commit()
    return ctx, parent


async def run_count(session) -> int:
    return await session.scalar(select(func.count()).select_from(MappingRun))


def conflicts(client, user, project, run_id):
    return client.get(
        f"/v1/projects/{project}/mapping-runs/{run_id}/conflicts", headers=headers(user)
    )


def resolve(client, user, project, run_id, *resolutions):
    return client.post(
        f"/v1/projects/{project}/mapping-runs/{run_id}/resolve-conflicts",
        json={"resolutions": list(resolutions)},
        headers=headers(user),
    )


PICK_TITLE = {"field_key": "title", "candidate_index": 1}
PICK_OWNER = {"field_key": "owner", "candidate_index": 2}
PICK_PLAIN = {"field_key": "plain", "candidate_index": 0}


@pytest.fixture(autouse=True)
def short_wait(monkeypatch):
    """No worker runs in most tests: the wait would otherwise last the whole cap."""
    monkeypatch.setattr(mapping_runs, "RESOLVE_WAIT_S", 0.3)
    monkeypatch.setattr(mapping_runs, "RESOLVE_POLL_S", 0.05)


@pytest.fixture
def worker_client(migrated_db, monkeypatch):
    """The real in-app worker (JOB_CONCURRENCY=1) renders the new run's artifact."""
    monkeypatch.setattr(mapping_runs, "RESOLVE_WAIT_S", 30)
    from fastapi.testclient import TestClient

    monkeypatch.setattr("doc_intel.writers.render", lambda *a, **k: (b"PK\x03\x04fake", []))
    app = create_app(get_settings().model_copy(update={"job_concurrency": 1}))
    with TestClient(app) as c:
        yield c


# -------------------------------------------------------------- GET conflicts


async def test_conflicts_list_in_ir_order_with_shared_fact_once(client, session, user_a, project):
    _, parent = await seed_parent(session, user_a, project)
    r = conflicts(client, user_a, project, parent.id)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["field_key"] for i in items] == ["title", "owner", "plain"]
    assert [i["field_label"] for i in items] == ["Title", "Owner", "Plain"]
    assert [i["slide_number"] for i in items] == [2, 9, None]
    # the field's context wins, else the slide title (after "Slide N"), else the last path element
    assert [i["section_title"] for i in items] == ["Risks", "Card A", "Sub"]
    assert items[0]["candidates"] == [
        {
            "index": 0,
            "value": "first",
            "source_filename": "first.pdf",
            "page_number": 3,
            "verbatim_quote": "the first passage",
        },
        {
            "index": 1,
            "value": "second",
            "source_filename": "second.pdf",
            "page_number": 4,
            "verbatim_quote": "the second passage",
        },
    ]
    assert [c["index"] for c in items[1]["candidates"]] == [0, 1, 2]


async def test_conflicts_empty_when_no_conflict(client, session, user_a, project):
    ctx = ctx_of(user_a, project)
    ir_id = await seed_template_ir(session, ctx, [field("note")])
    run = await repo.create_mapping_run(
        session,
        ctx,
        template_ir_id=ir_id,
        document_ids=[DOC],
        status="completed",
        results={"note": _result("note")},
        summary={},
    )
    await session.commit()
    assert conflicts(client, user_a, project, run.id).json() == {"items": []}


async def test_conflicts_other_tenant_and_unknown_run_are_identical_404(
    client, session, user_a, user_b, project
):
    _, parent = await seed_parent(session, user_a, project)
    b_project = client.post("/v1/projects", json={"name": "B"}, headers=headers(user_b)).json()
    for pid in (project, b_project["id"]):
        theirs = conflicts(client, user_b, pid, parent.id)
        random = conflicts(client, user_b, pid, uuid.uuid4())
        assert theirs.status_code == random.status_code == 404
        assert theirs.json()["error"]["code"] == random.json()["error"]["code"] == "not_found"


# ------------------------------------------------------- POST resolve-conflicts


async def test_resolve_candidate_pick_renders_and_returns_artifact_and_remaining(
    worker_client, session, user_a, project
):
    ctx, parent = await seed_parent(session, user_a, project)
    ir = await repo.get_template_ir_by_id(session, ctx, parent.template_ir_id)
    template = await repo.get_template(session, ctx, ir.template_id)
    doc = await repo.get_document(session, ctx, template.document_id)
    LocalStorage(get_settings().storage_root).put_bytes(doc.storage_key, b"TEMPLATE")

    r = resolve(worker_client, user_a, project, parent.id, PICK_TITLE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {
        "mapping_run_id",
        "version",
        "job_id",
        "status",
        "artifact",
        "remaining_conflicts",
    }
    assert body["version"] == 2 and body["status"] == "succeeded"
    assert body["artifact"]["mapping_run_id"] == body["mapping_run_id"]
    assert body["artifact"]["filename"] == "t_v2.pptx"
    assert body["artifact"]["download_url"].startswith("http")
    assert [c["field_key"] for c in body["remaining_conflicts"]] == ["owner", "plain"]

    session.expire_all()
    run = await session.get(MappingRun, uuid.UUID(body["mapping_run_id"]))
    assert run.edits == [{"op": "choose_candidate", "field_id": "title", "candidate_index": 1}]
    assert run.results["title"]["value"] == "second"
    assert run.results["title_copy"]["value"] == "second"  # the shared fact follows


async def test_resolve_custom_value_and_all_conflicts_gone(client, session, user_a, project):
    _, parent = await seed_parent(session, user_a, project)
    r = resolve(
        client,
        user_a,
        project,
        parent.id,
        {"field_key": "title", "custom_value": "Mine"},
        PICK_OWNER,
        PICK_PLAIN,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # no worker runs here: the job stays queued, so the artifact is null
    assert body["artifact"] is None and body["status"] == "queued"
    assert body["remaining_conflicts"] == []
    session.expire_all()
    run = await session.get(MappingRun, uuid.UUID(body["mapping_run_id"]))
    assert run.results["title"]["value"] == "Mine" and run.results["title"]["provenance"] == "user"
    assert run.results["owner"]["value"] == "c"
    assert run.edits[0] == {"op": "set_value", "field_id": "title", "value": "Mine"}


async def test_resolve_waits_only_up_to_the_cap(client, session, user_a, project):
    _, parent = await seed_parent(session, user_a, project)
    r = resolve(client, user_a, project, parent.id, PICK_TITLE)
    assert r.status_code == 200
    assert r.json()["artifact"] is None
    assert [c["field_key"] for c in r.json()["remaining_conflicts"]] == ["owner", "plain"]


@pytest.mark.parametrize(
    "resolutions",
    [
        [{"field_key": "title", "candidate_index": 9}],  # out of range
        [{"field_key": "note", "candidate_index": 0}],  # not a conflict
        [{"field_key": "nope", "candidate_index": 0}],
        [PICK_TITLE, {"field_key": "title", "custom_value": "x"}],  # twice
        [PICK_TITLE, {"field_key": "title_copy", "candidate_index": 0}],  # one shared fact
    ],
)
async def test_invalid_resolution_is_422_and_creates_no_run(
    client, session, user_a, project, resolutions
):
    _, parent = await seed_parent(session, user_a, project)
    before = await run_count(session)
    r = resolve(client, user_a, project, parent.id, *resolutions)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert await run_count(session) == before


@pytest.mark.parametrize(
    "body",
    [
        {"resolutions": []},
        {"resolutions": [{"field_key": "title"}]},  # neither
        {"resolutions": [{"field_key": "title", "candidate_index": 0, "custom_value": "x"}]},
        {"resolutions": [{"field_key": "title", "candidate_index": -1}]},
        {"resolutions": [{"field_key": "title", "custom_value": ""}]},
        {"resolutions": [{"field_key": "title", "custom_value": "x" * 10_001}]},
        {"resolutions": [{"field_key": "t" * 65, "candidate_index": 0}]},
        {"resolutions": [{"field_key": "title", "candidate_index": 0, "extra": 1}]},
        {"resolutions": [{"field_key": f"f{i}", "candidate_index": 0} for i in range(51)]},
    ],
)
async def test_malformed_body_is_422(client, session, user_a, project, body):
    _, parent = await seed_parent(session, user_a, project)
    r = client.post(
        f"/v1/projects/{project}/mapping-runs/{parent.id}/resolve-conflicts",
        json=body,
        headers=headers(user_a),
    )
    assert r.status_code == 422


async def test_resolve_other_tenant_is_404(client, session, user_a, user_b, project):
    _, parent = await seed_parent(session, user_a, project)
    b_project = client.post("/v1/projects", json={"name": "B"}, headers=headers(user_b)).json()
    before = await run_count(session)
    for pid in (project, b_project["id"]):
        theirs = resolve(client, user_b, pid, parent.id, PICK_TITLE)
        random = resolve(client, user_b, pid, uuid.uuid4(), PICK_TITLE)
        assert theirs.status_code == random.status_code == 404
        assert theirs.json()["error"]["code"] == random.json()["error"]["code"]
    assert await run_count(session) == before


async def test_resolve_parent_not_completed_is_409(client, session, user_a, project):
    _, parent = await seed_parent(session, user_a, project, status="rendering")
    r = resolve(client, user_a, project, parent.id, PICK_TITLE)
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"
