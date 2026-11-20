"""`POST /v1/projects/{p}/mapping-runs/{run_id}/edits` against real Postgres (Phase 11)."""

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from doc_intel.core.config import get_settings
from doc_intel.db import repo
from doc_intel.db.models import Job, MappingRun
from doc_intel.db.session import make_engine
from doc_intel.jobs.handlers import make_failure_hooks
from doc_intel.jobs.worker import run_one
from doc_intel.mapping.schemas import Candidate, ConflictDetails, Evidence, FieldMappingResult
from doc_intel.storage.local import LocalStorage
from tests.conftest import headers
from tests.integration.test_mapping_runs_api import (
    ctx_of,
    field,
    seed_document,
    seed_template_ir,
)
from tests.integration.test_run_mapping_job import handlers_for

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("empty_queue")]

DOC = uuid.uuid4()


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


def _evidence(quote):
    return Evidence(
        document_id=DOC,
        chunk_id=uuid.uuid4(),
        filename="a.pdf",
        page_number=1,
        section_heading=None,
        quote=quote,
    )


def _conflict(fid) -> dict:
    cands = [
        Candidate(value="first", evidence=[_evidence("the first passage")]),
        Candidate(value="second", evidence=[_evidence("the second passage")]),
    ]
    return _result(
        fid, "conflict", missing_reason=None, conflict_details=ConflictDetails(candidates=cands)
    )


async def seed_parent(session, user, project, status="completed"):
    ctx = ctx_of(user, project)
    ir_id = await seed_template_ir(session, ctx, [field("title"), field("owner")])
    await seed_document(session, ctx)
    results = {"title": _conflict("title"), "owner": _result("owner")}
    summary = {"populated": 0, "partial": 0, "missing": 1, "conflict": 1}
    summary["warnings"] = [{"field_id": "owner", "code": "formula_cell"}]
    parent = await repo.create_mapping_run(
        session,
        ctx,
        template_ir_id=ir_id,
        document_ids=[DOC],
        status=status,
        results=results,
        summary=summary,
    )
    await session.commit()
    return ctx, parent


def edit(client, user, project, run_id, *edits):
    return client.post(
        f"/v1/projects/{project}/mapping-runs/{run_id}/edits",
        json={"edits": list(edits)},
        headers=headers(user),
    )


SET_OWNER = {"op": "set_value", "field_id": "owner", "value": "Ann"}
CHOOSE = {"op": "choose_candidate", "field_id": "title", "candidate_index": 1}


async def test_edit_creates_a_new_version_and_leaves_the_parent_alone(
    client, session, user_a, project
):
    _, parent = await seed_parent(session, user_a, project)
    parent_id, before = parent.id, (parent.results, parent.summary, parent.status)

    r = edit(client, user_a, project, parent.id, SET_OWNER, CHOOSE)
    assert r.status_code == 202, r.text
    body = r.json()
    assert set(body) == {"mapping_run_id", "version", "job_id"} and body["version"] == 2

    session.expire_all()
    run = await session.get(MappingRun, uuid.UUID(body["mapping_run_id"]))
    assert (run.trigger, run.parent_run_id, run.status) == ("edit", parent_id, "rendering")
    assert run.version == 2 and run.document_ids == [DOC]
    assert run.edits == [SET_OWNER, CHOOSE]
    assert run.results["owner"]["value"] == "Ann" and run.results["owner"]["provenance"] == "user"
    assert run.results["title"]["value"] == "second"
    assert run.summary == {
        "populated": 2,
        "partial": 0,
        "missing": 0,
        "conflict": 0,
        "warnings": [],
    }
    job = await session.get(Job, uuid.UUID(body["job_id"]))
    assert job.kind == "render_artifact" and job.payload == {"mapping_run_id": str(run.id)}

    old = await session.get(MappingRun, parent_id)
    assert (old.results, old.summary, old.status) == before


async def test_two_edits_on_one_parent_get_distinct_versions(client, session, user_a, project):
    _, parent = await seed_parent(session, user_a, project)
    first = edit(client, user_a, project, parent.id, SET_OWNER).json()
    second = edit(client, user_a, project, parent.id, CHOOSE).json()
    assert (first["version"], second["version"]) == (2, 3)


async def test_render_job_writes_the_versioned_artifact(
    client, session, migrated_db, user_a, project, monkeypatch
):
    monkeypatch.setattr("doc_intel.writers.render", lambda *a, **k: (b"PK\x03\x04fake", []))
    ctx, parent = await seed_parent(session, user_a, project)
    ir = await repo.get_template_ir_by_id(session, ctx, parent.template_ir_id)
    template = await repo.get_template(session, ctx, ir.template_id)
    doc = await repo.get_document(session, ctx, template.document_id)
    LocalStorage(get_settings().storage_root).put_bytes(doc.storage_key, b"TEMPLATE")
    run_id = uuid.UUID(edit(client, user_a, project, parent.id, SET_OWNER).json()["mapping_run_id"])

    engine = make_engine(migrated_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    await run_one(sm, handlers_for(sm), make_failure_hooks(sm))
    async with sm() as s:
        artifact = await repo.get_artifact_by_run(s, ctx, run_id)
        run = await repo.get_mapping_run(s, ctx, run_id)
    await engine.dispose()
    assert run.status == "completed" and artifact.filename == "t_v2.pptx"


async def test_parent_not_completed_is_409(client, session, user_a, project):
    _, parent = await seed_parent(session, user_a, project, status="rendering")
    r = edit(client, user_a, project, parent.id, SET_OWNER)
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"


async def test_unknown_and_other_tenants_run_are_identical_404(
    client, session, user_a, user_b, project
):
    _, parent = await seed_parent(session, user_a, project)
    b_project = client.post("/v1/projects", json={"name": "B"}, headers=headers(user_b)).json()
    hdrs = {"X-Request-Id": "r1"}

    def call(pid, rid):
        return client.post(
            f"/v1/projects/{pid}/mapping-runs/{rid}/edits",
            json={"edits": [SET_OWNER]},
            headers=headers(user_b) | hdrs,
        )

    for pid in (project, b_project["id"]):
        theirs, random = call(pid, parent.id), call(pid, uuid.uuid4())
        assert theirs.status_code == random.status_code == 404
        assert theirs.json() == random.json()
    count = await session.scalar(select(func.count()).select_from(MappingRun))
    session.expire_all()
    assert count == await session.scalar(select(func.count()).select_from(MappingRun))


@pytest.mark.parametrize(
    "edits",
    [
        [SET_OWNER, {"op": "clear", "field_id": "nope"}],
        [SET_OWNER, {"op": "clear", "field_id": "owner"}],
        [{"op": "choose_candidate", "field_id": "owner", "candidate_index": 0}],
        [{"op": "choose_candidate", "field_id": "title", "candidate_index": 5}],
        [{"op": "choose_candidate", "field_id": "title", "candidate_index": -1}],
        [{"op": "set_value", "field_id": "owner"}],
        [{"op": "set_value", "field_id": "owner", "value": ["x"]}],
        [{"op": "bogus", "field_id": "owner"}],
        [],
    ],
)
async def test_rejected_edits_create_no_run_and_no_job(client, session, user_a, project, edits):
    _, parent = await seed_parent(session, user_a, project)
    runs = await session.scalar(select(func.count()).select_from(MappingRun))
    r = edit(client, user_a, project, parent.id, *edits)
    assert r.status_code == 422, r.text
    assert "nope" not in r.text
    session.expire_all()
    assert await session.scalar(select(func.count()).select_from(MappingRun)) == runs
    assert await session.scalar(select(func.count()).select_from(Job)) == 0
