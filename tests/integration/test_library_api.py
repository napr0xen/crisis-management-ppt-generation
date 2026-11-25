"""Template library over HTTP: `/v1/library/*` (Phase 19, ADR-014)."""

import asyncio
import io
import time
import uuid
import zipfile

import pytest
from pptx import Presentation

from doc_intel.db import repo
from doc_intel.ingestion.upload import library_key
from tests.conftest import headers
from tests.fakes import FakeLLM
from tests.integration.test_library import FILES, ir_reply
from tests.integration.test_pipe_library import unique

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("one_ir_batch", "clean_library")]


class SlowLLM(FakeLLM):
    """Holds the reply for a moment so a test can see the `scanning` state."""

    async def complete(self, messages, json_schema=None, think=False) -> str:
        await asyncio.sleep(0.4)
        return await super().complete(messages, json_schema)


def post(client, user, data=None, name="Status", filename="status.pptx", **form):
    return client.post(
        "/v1/library/templates",
        files={"file": (filename, data or unique("pptx", name))},
        data={"name": name, **form},
        headers=headers(user),
    )


def scan_of(client, user, scan_id):
    return client.get(f"/v1/library/scans/{scan_id}", headers=headers(user))


def wait_scan(client, user, scan_id, status="ready") -> dict:
    deadline = time.time() + 15
    while time.time() < deadline:
        scan = scan_of(client, user, scan_id).json()
        if scan["status"] != "scanning":
            break
        time.sleep(0.05)
    assert scan["status"] == status, scan
    return scan


def items(client, user) -> dict:
    r = client.get("/v1/library/templates", headers=headers(user))
    assert r.status_code == 200, r.text
    return r.json()


def test_upload_scans_in_background_then_lists_with_counts(client, user_a):
    client.app.state.llm = SlowLLM(ir_reply("pptx"))
    data = unique("pptx", "Status")
    r = post(client, user_a, data, description="Weekly", category="ops")
    assert r.status_code == 202, r.text
    assert set(r.json()) == {"scan_id"}
    scan_id = r.json()["scan_id"]

    first = scan_of(client, user_a, scan_id).json()
    assert first["status"] == "scanning" and first["template_id"] is None
    assert set(first) == {"scan_id", "name", "status", "progress", "template_id", "error_code"}
    assert first["scan_id"] == scan_id and first["name"] == "Status"
    listed = items(client, user_a)
    assert listed["items"] == [] and [s["scan_id"] for s in listed["scans"]] == [scan_id]

    deadline = time.time() + 5  # the first batch reports before its (slow) LLM reply
    while scan_of(client, user_a, scan_id).json()["progress"] is None and time.time() < deadline:
        time.sleep(0.02)
    assert scan_of(client, user_a, scan_id).json()["progress"] == {
        "done": 0,
        "total": 1,
        "part": None,
    }

    done = wait_scan(client, user_a, scan_id)
    assert done["error_code"] is None and uuid.UUID(done["template_id"])
    assert done["progress"] is None

    listed = items(client, user_a)
    assert listed["scans"] == []
    [t] = listed["items"]
    assert t["id"] == done["template_id"]
    assert (t["name"], t["description"], t["category"], t["format"]) == (
        "Status",
        "Weekly",
        "ops",
        "pptx",
    )
    assert (t["filename"], t["size_bytes"], t["scan_status"]) == ("status.pptx", len(data), "ready")
    # the LLM draft has one field; code adds fields for tables and tokens
    assert t["field_count"] >= 1 and t["anchor_count"] > 1 and t["slide_count"] >= 1
    assert t["created_at"]


async def test_counts_come_from_the_stored_ir(client, session, user_a):
    def f(fid, anchor, mode="replace_text", typ="text"):
        return {"field_id": fid, "anchor_id": anchor, "write_mode": mode, "expected_type": typ}

    ir = {
        "fields": [
            f("a", "pptx:s1:sp2"),
            f("b", "pptx:s4:sp3:r1c0", "fill_table_rows", "table"),
            f("c", "pptx:s4:sp3:r1c1", "fill_cell"),  # the same table as b
            f("d", "pptx:s4:sp5:r2c2", "fill_cell"),
            f("e", "pptx:s6:sp1", "fill_chart", "chart"),
            f("g", "pptx:s6:sp9", "insert_chart", "chart"),
        ]
    }
    await repo.create_library_template(
        session,
        name="Counts",
        description="",
        category=None,
        format="pptx",
        filename="c.pptx",
        sha256="a" * 64,
        size_bytes=5,
        storage_key="library/x/original.pptx",
        anchor_map={"anchors": [{"anchor_id": f"pptx:s{n}:sp1"} for n in (1, 2, 8)]},
        anchor_count=9,
        ir=ir,
    )
    await repo.create_library_template(
        session,
        name="Doc",
        description="",
        category=None,
        format="docx",
        filename="c.docx",
        sha256="b" * 64,
        size_bytes=5,
        storage_key="library/y/original.docx",
        anchor_map={"anchors": [{"anchor_id": "docx:body:t0:r1c0"}]},
        anchor_count=1,
        ir={"fields": [f("a", "docx:body:t0:r1c0", "fill_table_rows", "table")]},
    )
    await session.commit()
    by_name = {t["name"]: t for t in items(client, user_a)["items"]}
    c, d = by_name["Counts"], by_name["Doc"]
    assert (c["slide_count"], c["field_count"], c["table_count"], c["chart_count"]) == (8, 6, 2, 2)
    assert c["anchor_count"] == 9
    assert (d["slide_count"], d["table_count"]) == (None, 1)


def test_scan_failure_keeps_only_a_code(client, user_a):
    client.app.state.llm = FakeLLM("not json", "not json")
    scan_id = post(client, user_a).json()["scan_id"]
    scan = wait_scan(client, user_a, scan_id, "failed")
    assert scan["template_id"] is None and scan["error_code"]
    assert "not json" not in str(scan)
    listed = items(client, user_a)
    assert listed["items"] == [] and [s["status"] for s in listed["scans"]] == ["failed"]


def test_duplicate_name_or_bytes_is_409(client, user_a):
    client.app.state.llm = FakeLLM(ir_reply("pptx"))
    data = unique("pptx", "Status")
    wait_scan(client, user_a, post(client, user_a, data).json()["scan_id"])
    for r in (post(client, user_a, unique("pptx", "Other")), post(client, user_a, data, "Other")):
        assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"
    assert len(items(client, user_a)["items"]) == 1


def blank_pptx() -> bytes:
    out = io.BytesIO()
    Presentation().save(out)
    return out.getvalue()


def broken_pptx() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", "<nope")
        z.writestr("ppt/presentation.xml", "<nope")
    return out.getvalue()


@pytest.mark.parametrize(
    ("kwargs", "status", "code"),
    [
        ({"filename": "x.pdf", "data": b"%PDF-1.7 x"}, 415, "unsupported_media_type"),
        ({"filename": "x.txt", "data": b"hello"}, 415, "unsupported_media_type"),
        ({"data": blank_pptx()}, 422, "validation_error"),  # no fillable location
        ({"data": broken_pptx()}, 422, "validation_error"),  # unreadable
        ({"name": ""}, 422, "validation_error"),
        ({"name": "x" * 101}, 422, "validation_error"),
        ({"description": "x" * 501}, 422, "validation_error"),
        ({"category": "x" * 51}, 422, "validation_error"),
    ],
)
def test_upload_rejections_start_no_scan(client, user_a, kwargs, status, code):
    r = post(client, user_a, **kwargs)
    assert (r.status_code, r.json()["error"]["code"]) == (status, code)
    assert items(client, user_a)["scans"] == []


def test_oversize_upload_is_413(client, user_a, monkeypatch):
    monkeypatch.setattr(client.app.state.settings, "max_upload_mb", 1)
    r = post(client, user_a, FILES["pptx"] + b"x" * (1024 * 1024 + 1))
    assert (r.status_code, r.json()["error"]["code"]) == (413, "payload_too_large")


def test_missing_file_is_422_and_auth_is_required(client, user_a):
    r = client.post("/v1/library/templates", data={"name": "x"}, headers=headers(user_a))
    assert r.status_code == 422
    for call in (
        lambda: client.get("/v1/library/templates"),
        lambda: client.get(f"/v1/library/scans/{uuid.uuid4()}"),
        lambda: client.delete(f"/v1/library/templates/{uuid.uuid4()}"),
        lambda: client.post("/v1/library/templates", data={"name": "x"}),
    ):
        assert call().status_code == 401


def test_unknown_scan_is_404(client, user_a):
    assert scan_of(client, user_a, uuid.uuid4()).status_code == 404
    assert scan_of(client, user_a, "nope").status_code in (404, 422)


def test_delete_removes_row_and_file(client, user_a):
    client.app.state.llm = FakeLLM(ir_reply("pptx"))
    scan = wait_scan(client, user_a, post(client, user_a).json()["scan_id"])
    template_id = uuid.UUID(scan["template_id"])
    key = library_key(template_id, "pptx")
    storage = client.app.state.storage
    assert storage.open_read(key).read()
    r = client.delete(f"/v1/library/templates/{template_id}", headers=headers(user_a))
    assert r.status_code == 204 and r.content == b""
    assert items(client, user_a)["items"] == []
    with pytest.raises(FileNotFoundError):
        storage.open_read(key)
    again = client.delete(f"/v1/library/templates/{template_id}", headers=headers(user_a))
    assert again.status_code == 404
    assert (
        client.delete(f"/v1/library/templates/{uuid.uuid4()}", headers=headers(user_a)).status_code
        == 404
    )


class HangingLLM(FakeLLM):
    async def complete(self, messages, json_schema=None, think=False) -> str:
        await asyncio.Event().wait()  # never replies
        return ""


def test_shutdown_cancels_a_scan_in_flight(migrated_db, user_a):
    from fastapi.testclient import TestClient

    from doc_intel.main import create_app

    with TestClient(create_app()) as c:
        c.app.state.llm = HangingLLM()
        assert post(c, user_a, name="Hangs").status_code == 202
        tasks = set(c.app.state.scan_tasks)
        assert tasks
    assert all(t.cancelled() for t in tasks)
