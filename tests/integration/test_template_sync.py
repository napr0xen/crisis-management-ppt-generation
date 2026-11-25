"""templates/ folder sync (ADR-017): folder -> library rows, plus upload/delete/reset wiring."""

import asyncio
import time
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from doc_intel import cli
from doc_intel.db import repo
from doc_intel.db.session import make_engine
from doc_intel.storage.local import LocalStorage
from doc_intel.templates import library
from tests.conftest import headers
from tests.fakes import FakeLLM
from tests.integration.test_library import MAX, ir_reply
from tests.integration.test_library_api import SlowLLM, items, post, wait_scan
from tests.integration.test_pipe_library import unique

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("one_ir_batch", "clean_library")]


@pytest.fixture
async def sm(migrated_db):
    engine = make_engine(migrated_db)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def storage(tmp_path) -> LocalStorage:
    return LocalStorage(tmp_path / "storage")


@pytest.fixture
def folder(tmp_path) -> Path:
    path = tmp_path / "templates"
    path.mkdir()
    return path


async def sync(sm, storage, folder, *replies) -> tuple[library.SyncPlan, FakeLLM]:
    llm = FakeLLM(*replies)
    plan = await library.sync_templates_dir(
        sm, storage, llm, folder, max_bytes=MAX, lock=asyncio.Lock()
    )
    return plan, llm


async def rows(sm) -> dict:
    async with sm() as session:
        return {r.filename: r for r in await repo.list_library_templates(session)}


def put(folder: Path, name: str, title: str | None = None) -> bytes:
    data = unique("pptx" if name.endswith("pptx") else "docx", title or name)
    (folder / name).write_bytes(data)
    return data


async def test_new_file_is_added_with_stem_name_and_builder_version(sm, storage, folder):
    put(folder, "Weekly Status.pptx")
    plan, llm = await sync(sm, storage, folder, ir_reply())
    assert plan.add == ["Weekly Status.pptx"] and len(llm.calls) == 1
    row = (await rows(sm))["Weekly Status.pptx"]
    assert (row.name, row.description, row.category, row.format) == (
        "Weekly Status",
        "",
        None,
        "pptx",
    )
    assert row.ir_builder_version == library.IR_BUILDER_VERSION and row.ir["fields"]
    assert storage.open_read(row.storage_key).read()


async def test_only_library_formats_and_visible_files_are_read(sm, storage, folder):
    put(folder, "a.docx")
    (folder / ".hidden.pptx").write_bytes(b"x")
    (folder / "~$lock.pptx").write_bytes(b"x")
    (folder / "notes.txt").write_bytes(b"x")
    (folder / ".gitkeep").write_bytes(b"")
    (folder / "sub.pptx").mkdir()
    plan, _ = await sync(sm, storage, folder, ir_reply("docx"))
    assert plan.add == ["a.docx"] and list(await rows(sm)) == ["a.docx"]


async def test_unchanged_file_is_kept_without_an_llm_call(sm, storage, folder):
    put(folder, "a.pptx")
    await sync(sm, storage, folder, ir_reply())
    before = (await rows(sm))["a.pptx"]
    plan, llm = await sync(sm, storage, folder)
    assert plan == library.SyncPlan() and llm.calls == []
    assert (await rows(sm))["a.pptx"].id == before.id


async def test_changed_bytes_swap_the_row_and_keep_its_name(sm, storage, folder):
    put(folder, "a.pptx", "v1")
    await sync(sm, storage, folder, ir_reply())
    old = (await rows(sm))["a.pptx"]
    async with sm.begin() as s:  # a name and description set in the UI
        (await repo.get_library_template(s, old.id)).description = "Weekly"
    new_bytes = put(folder, "a.pptx", "v2")
    plan, llm = await sync(sm, storage, folder, ir_reply())
    assert len(plan.update) == 1 and len(llm.calls) == 1
    row = (await rows(sm))["a.pptx"]
    assert row.id != old.id and row.sha256 != old.sha256
    assert (row.name, row.description, row.size_bytes) == ("a", "Weekly", len(new_bytes))
    with pytest.raises(FileNotFoundError):
        storage.open_read(old.storage_key)
    assert storage.open_read(row.storage_key).read() == new_bytes
    assert len(await rows(sm)) == 1


async def test_newer_ir_builder_version_re_extracts(sm, storage, folder, monkeypatch):
    put(folder, "a.pptx")
    await sync(sm, storage, folder, ir_reply())
    monkeypatch.setattr(library, "IR_BUILDER_VERSION", library.IR_BUILDER_VERSION + 1)
    plan, llm = await sync(sm, storage, folder, ir_reply())
    assert len(plan.update) == 1 and len(llm.calls) == 1
    assert (await rows(sm))["a.pptx"].ir_builder_version == library.IR_BUILDER_VERSION


async def test_row_whose_file_is_gone_is_pruned_with_its_blob(sm, storage, folder):
    put(folder, "a.pptx")
    put(folder, "b.pptx")
    await sync(sm, storage, folder, ir_reply(), ir_reply())
    gone = (await rows(sm))["b.pptx"]
    (folder / "b.pptx").unlink()
    plan, llm = await sync(sm, storage, folder)
    assert len(plan.prune) == 1 and llm.calls == []
    assert list(await rows(sm)) == ["a.pptx"]
    with pytest.raises(FileNotFoundError):
        storage.open_read(gone.storage_key)


async def test_renamed_file_is_one_row_under_the_new_name(sm, storage, folder):
    put(folder, "a.pptx")
    await sync(sm, storage, folder, ir_reply())
    (folder / "a.pptx").rename(folder / "b.pptx")
    await sync(sm, storage, folder, ir_reply())
    assert list(await rows(sm)) == ["b.pptx"]


async def test_same_bytes_under_two_names_make_one_row(sm, storage, folder):
    data = put(folder, "a.pptx")
    (folder / "b.pptx").write_bytes(data)
    plan, _ = await sync(sm, storage, folder, ir_reply())
    assert (plan.add, plan.skipped) == (["a.pptx"], ["b.pptx"])
    assert list(await rows(sm)) == ["a.pptx"]


async def test_failed_rebuild_keeps_the_old_row_and_other_files_continue(sm, storage, folder):
    put(folder, "a.pptx", "v1")
    await sync(sm, storage, folder, ir_reply())
    old = (await rows(sm))["a.pptx"]
    put(folder, "a.pptx", "v2")
    put(folder, "b.pptx")
    await sync(sm, storage, folder, ir_reply(), "not json", "not json")  # b is added first
    found = await rows(sm)
    assert found["a.pptx"].id == old.id and found["a.pptx"].sha256 == old.sha256
    assert storage.open_read(old.storage_key).read()
    assert "b.pptx" in found


async def test_missing_folder_prunes_nothing(sm, storage, folder, tmp_path):
    put(folder, "a.pptx")
    await sync(sm, storage, folder, ir_reply())
    plan, llm = await sync(sm, storage, tmp_path / "absent")
    assert plan == library.SyncPlan() and llm.calls == []
    assert list(await rows(sm)) == ["a.pptx"]


async def test_sync_logs_counts_without_names(sm, storage, folder, capsys):
    put(folder, "Secret Plan.pptx")
    await sync(sm, storage, folder, ir_reply())
    out = capsys.readouterr().out
    assert "template_sync" in out and "Secret" not in out


async def test_on_scan_reports_the_row_name_and_outcome(sm, storage, folder):
    from contextlib import contextmanager

    seen = []

    @contextmanager
    def on_scan(name):
        seen.append(name)
        yield None
        seen.append("ok")

    put(folder, "a.pptx")
    await library.sync_templates_dir(
        sm,
        storage,
        FakeLLM(ir_reply()),
        folder,
        max_bytes=MAX,
        lock=asyncio.Lock(),
        on_scan=on_scan,
    )
    assert seen == ["a", "ok"]


# ------------------------------------------------------------------ API


@pytest.fixture
def api(client, folder, monkeypatch):
    # the settings object is cached for the whole run: restore it after the test
    monkeypatch.setattr(client.app.state.settings, "templates_dir", folder)
    return client


def wait_items(client, user, count) -> list[dict]:
    deadline = time.time() + 15
    while time.time() < deadline:
        listed = items(client, user)["items"]
        if len(listed) == count:
            return listed
        time.sleep(0.05)
    raise AssertionError(listed)


def test_sync_endpoint_starts_a_background_sync(api, folder, user_a):
    put(folder, "a.pptx")
    api.app.state.llm = FakeLLM(ir_reply())
    r = api.post("/v1/templates/sync", headers=headers(user_a))
    assert (r.status_code, r.json()) == (202, {"status": "started"})
    assert [t["name"] for t in wait_items(api, user_a, 1)] == ["a"]
    assert api.post("/v1/templates/sync").status_code == 401


def test_sync_endpoint_is_409_while_one_runs(api, folder, user_a):
    put(folder, "a.pptx")
    api.app.state.llm = SlowLLM(ir_reply())
    assert api.post("/v1/templates/sync", headers=headers(user_a)).status_code == 202
    deadline = time.time() + 5  # the scan is listed once the sync holds the lock
    while not items(api, user_a)["scans"] and time.time() < deadline:
        time.sleep(0.02)
    again = api.post("/v1/templates/sync", headers=headers(user_a))
    assert (again.status_code, again.json()["error"]["code"]) == (409, "conflict")
    wait_items(api, user_a, 1)


def test_upload_saves_into_the_folder_and_a_later_sync_keeps_it(api, folder, user_a):
    api.app.state.llm = FakeLLM(ir_reply())
    data = unique("pptx", "Status")
    r = post(api, user_a, data, description="Weekly", filename="dir/status.pptx")
    assert r.status_code == 202, r.text
    wait_scan(api, user_a, r.json()["scan_id"])
    assert (folder / "status.pptx").read_bytes() == data
    [t] = items(api, user_a)["items"]
    assert (t["name"], t["description"], t["filename"]) == ("Status", "Weekly", "status.pptx")

    api.app.state.llm = idle = FakeLLM()
    assert api.post("/v1/templates/sync", headers=headers(user_a)).status_code == 202
    time.sleep(0.5)
    assert [x["id"] for x in items(api, user_a)["items"]] == [t["id"]]
    assert idle.calls == []


def test_upload_over_an_existing_file_is_409_and_stores_nothing(api, folder, user_a):
    (folder / "status.pptx").write_bytes(b"mine")
    r = post(api, user_a, unique("pptx", "Status"))
    assert (r.status_code, r.json()["error"]["code"]) == (409, "conflict")
    assert (folder / "status.pptx").read_bytes() == b"mine"
    assert items(api, user_a) == {"items": [], "scans": []}


@pytest.mark.parametrize("filename", [".pptx", "..pptx"])
def test_upload_with_a_dotfile_name_is_422(api, folder, user_a, filename):
    r = post(api, user_a, filename=filename)
    assert (r.status_code, r.json()["error"]["code"]) == (422, "validation_error")
    assert list(folder.iterdir()) == []


def test_delete_removes_the_file_from_the_folder(api, folder, user_a):
    api.app.state.llm = FakeLLM(ir_reply())
    scan = wait_scan(api, user_a, post(api, user_a).json()["scan_id"])
    assert (folder / "status.pptx").exists()
    r = api.delete(f"/v1/library/templates/{scan['template_id']}", headers=headers(user_a))
    assert r.status_code == 204 and not (folder / "status.pptx").exists()


def test_delete_with_the_file_already_gone_is_fine(api, folder, user_a):
    api.app.state.llm = FakeLLM(ir_reply())
    scan = wait_scan(api, user_a, post(api, user_a).json()["scan_id"])
    (folder / "status.pptx").unlink()
    r = api.delete(f"/v1/library/templates/{scan['template_id']}", headers=headers(user_a))
    assert r.status_code == 204


def test_delete_never_touches_a_file_outside_the_folder(api, tmp_path, user_a):
    api.app.state.llm = FakeLLM(ir_reply())
    scan = wait_scan(api, user_a, post(api, user_a).json()["scan_id"])
    outside = tmp_path / "outside.pptx"
    outside.write_bytes(b"keep")
    link = Path(api.app.state.settings.templates_dir) / "status.pptx"
    link.unlink()
    link.symlink_to(outside)
    api.delete(f"/v1/library/templates/{scan['template_id']}", headers=headers(user_a))
    assert outside.read_bytes() == b"keep"


def test_startup_syncs_the_folder(migrated_db, folder, user_a, monkeypatch):
    from fastapi.testclient import TestClient

    from doc_intel.core.config import get_settings
    from doc_intel.main import create_app

    monkeypatch.setattr("doc_intel.main.make_llm_client", lambda s, h: FakeLLM(ir_reply()))
    put(folder, "boot.pptx")
    settings = get_settings().model_copy(update={"templates_dir": folder})
    with TestClient(create_app(settings)) as c:
        assert [t["name"] for t in wait_items(c, user_a, 1)] == ["boot"]


# ------------------------------------------------------------------ CLI


async def test_reset_library_removes_rows_and_blobs_but_not_files(sm, storage, folder, capsys):
    put(folder, "a.pptx")
    put(folder, "b.pptx")
    await sync(sm, storage, folder, ir_reply(), ir_reply())
    keys = [r.storage_key for r in (await rows(sm)).values()]
    args = cli.parse(["reset-library"])
    assert await cli.run(args, sm, storage, FakeLLM()) == 0
    assert await rows(sm) == {}
    assert "Removed 2 library templates" in capsys.readouterr().out
    for key in keys:
        with pytest.raises(FileNotFoundError):
            storage.open_read(key)
    assert sorted(p.name for p in folder.iterdir()) == ["a.pptx", "b.pptx"]
