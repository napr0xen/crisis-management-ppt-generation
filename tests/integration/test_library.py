"""Template library: table, register, adopt (copy on use), operator CLI."""

import hashlib
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from doc_intel import cli
from doc_intel.core.errors import Conflict, UnsupportedMediaType, ValidationFailed
from doc_intel.core.tenancy import TenantContext
from doc_intel.db import repo
from doc_intel.db.models import Document, LibraryTemplate, Template
from doc_intel.db.session import make_engine
from doc_intel.ingestion.upload import library_key
from doc_intel.storage.local import LocalStorage
from doc_intel.templates import library
from doc_intel.templates.anchors import build_anchor_map
from tests.conftest import headers
from tests.fakes import FakeLLM
from tests.fixtures import templates
from tests.integration.test_extract_template_job import draft_reply

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("one_ir_batch", "clean_library"),
]

MAX = 10 * 1024 * 1024
# not byte-stable, so build each file once
FILES = {f: getattr(templates, f"{f}_template")() for f in ("pptx", "docx", "xlsx")}


def ir_reply(fmt: str = "pptx") -> str:
    return draft_reply(build_anchor_map(fmt, FILES[fmt]).anchors[0], "f1")


@pytest.fixture
async def sm(migrated_db):
    engine = make_engine(migrated_db)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def storage(tmp_path) -> LocalStorage:
    return LocalStorage(tmp_path / "storage")


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


async def register(sm, storage, tmp_path, name="Status", fmt="pptx", llm=None, **kw):
    path = write(tmp_path, f"{name}.{fmt}", FILES[fmt])
    return await library.register(
        sm, storage, llm or FakeLLM(ir_reply(fmt)), path, name=name, max_bytes=MAX, **kw
    )


# ------------------------------------------------------------------ table


async def test_table_enforces_unique_name_sha_and_format(sm):
    def row(name, sha, fmt="pptx"):
        return LibraryTemplate(
            name=name, format=fmt, filename="f", sha256=sha, size_bytes=1, storage_key="k",
            anchor_map={}, anchor_count=0, ir={},
        )  # fmt: skip

    async with sm.begin() as s:
        s.add(row("a", "1"))
    for bad in (row("a", "2"), row("b", "1"), row("c", "3", "pdf")):
        with pytest.raises(IntegrityError):
            async with sm.begin() as s:
                s.add(bad)


# --------------------------------------------------------------- register


async def test_register_stores_file_row_and_ir(sm, storage, tmp_path):
    lib = await register(sm, storage, tmp_path, description=" d ", category=" ops ")
    data = FILES["pptx"]
    assert lib.storage_key == library_key(lib.id, "pptx")
    assert storage.open_read(lib.storage_key).read() == data
    assert (lib.name, lib.description, lib.category, lib.format) == ("Status", "d", "ops", "pptx")
    assert lib.sha256 == hashlib.sha256(data).hexdigest() and lib.size_bytes == len(data)
    assert lib.filename == "Status.pptx" and lib.anchor_count == len(lib.anchor_map["anchors"])
    assert lib.ir["template_id"] == str(lib.id) and lib.ir["fields"][0]["field_id"] == "f1"


async def test_register_reports_batches(sm, storage, tmp_path):
    seen = []

    async def on_batch(done, total, part):
        seen.append((done, total))

    await register(sm, storage, tmp_path, on_batch=on_batch)
    assert seen == [(0, 1)]


async def test_duplicate_name_or_bytes_fail_before_any_llm_call(sm, storage, tmp_path):
    await register(sm, storage, tmp_path)
    llm = FakeLLM()
    other = write(tmp_path, "other.pptx", FILES["pptx"])
    with pytest.raises(Conflict):  # same bytes
        await library.register(sm, storage, llm, other, name="Other", max_bytes=MAX)
    docx = write(tmp_path, "m.docx", FILES["docx"])
    with pytest.raises(Conflict):  # same name
        await library.register(sm, storage, llm, docx, name="Status", max_bytes=MAX)
    assert llm.calls == []


async def test_register_rejects_pdf_and_bad_names(sm, storage, tmp_path):
    llm = FakeLLM()
    pdf = write(tmp_path, "a.pdf", b"%PDF-1.7 x")
    with pytest.raises(UnsupportedMediaType):
        await library.register(sm, storage, llm, pdf, name="P", max_bytes=MAX)
    pptx = write(tmp_path, "a.pptx", FILES["pptx"])
    for bad in ("x" * 101, "   "):
        with pytest.raises(ValidationFailed):
            await library.register(sm, storage, llm, pptx, name=bad, max_bytes=MAX)
    with pytest.raises(ValidationFailed):
        await library.register(
            sm, storage, llm, pptx, name="ok", description="d" * 501, max_bytes=MAX
        )
    with pytest.raises(ValidationFailed):
        await library.register(sm, storage, llm, pptx, name="ok", category="c" * 51, max_bytes=MAX)
    assert llm.calls == []


async def test_insert_failure_deletes_the_stored_file(sm, storage, tmp_path, monkeypatch):
    async def boom(session, **values):
        raise Conflict()

    monkeypatch.setattr(repo, "create_library_template", boom)
    with pytest.raises(Conflict):
        await register(sm, storage, tmp_path)
    assert not list((storage.root / "library").rglob("original.*"))


# ------------------------------------------------------------------ adopt


async def project_ctx(sm, user: str) -> TenantContext:
    async with sm.begin() as s:
        project = await repo.create_project(s, user, "P")
    return TenantContext(user, project.id)


async def adopt(sm, storage, ctx, lib):
    async with sm.begin() as s:
        return await library.adopt(s, ctx, storage, lib.id)


async def test_adopt_copies_into_the_callers_tenant(sm, storage, tmp_path, user_a):
    lib = await register(sm, storage, tmp_path)
    ctx = await project_ctx(sm, user_a)
    ir_id = await adopt(sm, storage, ctx, lib)
    async with sm() as s:
        ir = await repo.get_template_ir_by_id(s, ctx, ir_id)
        template = await repo.get_template(s, ctx, ir.template_id)
        doc = await repo.get_document(s, ctx, template.document_id)
    assert (doc.role, doc.status, doc.filename, doc.sha256) == (
        "template", "indexed", "Status.pptx", lib.sha256,
    )  # fmt: skip
    assert storage.open_read(doc.storage_key).read() == FILES["pptx"]
    assert (template.format, template.anchor_count) == ("pptx", lib.anchor_count)
    assert ir.version == 1 and ir.ir["template_id"] == str(template.id) != str(lib.id)
    assert ir.ir["fields"] == lib.ir["fields"]


async def test_adopt_never_calls_the_llm_and_is_idempotent(sm, storage, tmp_path, user_a):
    llm = FakeLLM(ir_reply())
    lib = await register(sm, storage, tmp_path, llm=llm)
    calls = len(llm.calls)
    ctx = await project_ctx(sm, user_a)
    first = await adopt(sm, storage, ctx, lib)
    assert await adopt(sm, storage, ctx, lib) == first
    assert len(llm.calls) == calls
    async with sm() as s:
        count = await s.scalar(
            select(func.count()).select_from(Document).where(Document.project_id == ctx.project_id)
        )
    assert count == 1


async def test_other_tenant_cannot_see_the_copy(client, sm, storage, tmp_path, user_a, user_b):
    lib = await register(sm, storage, tmp_path)
    ctx = await project_ctx(sm, user_a)
    ir_id = await adopt(sm, storage, ctx, lib)
    async with sm() as s:
        ir = await repo.get_template_ir_by_id(s, ctx, ir_id)
        template = await repo.get_template(s, ctx, ir.template_id)
    base = f"/v1/projects/{ctx.project_id}"
    for path in (f"/templates/{template.id}", f"/documents/{template.document_id}"):
        assert client.get(base + path, headers=headers(user_a)).status_code == 200
        assert client.get(base + path, headers=headers(user_b)).status_code == 404


async def test_removing_the_library_template_keeps_the_copy(sm, storage, tmp_path, user_a):
    lib = await register(sm, storage, tmp_path)
    ctx = await project_ctx(sm, user_a)
    ir_id = await adopt(sm, storage, ctx, lib)
    async with sm.begin() as s:
        run = await repo.create_mapping_run(s, ctx, template_ir_id=ir_id, document_ids=[])
    async with sm.begin() as s:
        await repo.delete_library_template(s, lib.id)
    async with sm() as s:
        assert await repo.get_template_ir_by_id(s, ctx, ir_id)
        assert await repo.get_mapping_run(s, ctx, run.id)
        assert (
            await s.scalar(
                select(func.count())
                .select_from(Template)
                .where(Template.project_id == ctx.project_id)
            )
            == 1
        )


async def test_source_with_the_same_hash_is_a_conflict(sm, storage, tmp_path, user_a):
    lib = await register(sm, storage, tmp_path)
    ctx = await project_ctx(sm, user_a)
    async with sm.begin() as s:
        await repo.create_document(
            s, ctx, uuid.uuid4(), filename="mine.pptx", mime_type="x", size_bytes=1,
            sha256=lib.sha256, storage_key="k.pptx",
        )  # fmt: skip
    with pytest.raises(Conflict, match="already attached"):
        await adopt(sm, storage, ctx, lib)


async def test_template_without_ir_yet_is_a_conflict(sm, storage, tmp_path, user_a):
    lib = await register(sm, storage, tmp_path)
    ctx = await project_ctx(sm, user_a)
    async with sm.begin() as s:
        await repo.create_document(
            s, ctx, uuid.uuid4(), filename="t.pptx", mime_type="x", size_bytes=1,
            sha256=lib.sha256, storage_key="k.pptx", role="template",
        )  # fmt: skip
    with pytest.raises(Conflict, match="still being prepared"):
        await adopt(sm, storage, ctx, lib)


async def test_unknown_library_template_is_not_found(sm, storage, user_a):
    from doc_intel.core.errors import NotFound

    ctx = await project_ctx(sm, user_a)
    with pytest.raises(NotFound):
        await adopt(sm, storage, ctx, LibraryTemplate(id=uuid.uuid4()))


# -------------------------------------------------------------------- CLI


async def run_cli(sm, storage, llm, *argv):
    return await cli.run(cli.parse(list(argv)), sm, storage, llm)


async def test_cli_round_trip(sm, storage, tmp_path, capsys):
    path = write(tmp_path, "deck.pptx", FILES["pptx"])
    llm = FakeLLM(ir_reply())
    assert await run_cli(sm, storage, llm, "seed-template", str(path), "--name", "Deck",
                         "--description", "Weekly", "--category", "ops") == 0  # fmt: skip
    out = capsys.readouterr()
    assert "Analyzing template fields" in out.err and "(1/1)" in out.err
    assert "Deck" in out.out and "pptx" in out.out
    async with sm() as s:
        (lib,) = await repo.list_library_templates(s)

    assert await run_cli(sm, storage, llm, "list-templates") == 0
    listed = capsys.readouterr().out
    assert str(lib.id) in listed and "Deck" in listed and "ops" in listed

    assert await run_cli(sm, storage, llm, "show-template", str(lib.id)) == 0
    shown = capsys.readouterr().out
    assert "Weekly" in shown and "deck.pptx" in shown and "f1: First field" in shown

    assert await run_cli(sm, storage, llm, "remove-template", str(lib.id)) == 0
    assert "Deck" in capsys.readouterr().out
    async with sm() as s:
        assert await repo.list_library_templates(s) == []
    assert not (storage.root / lib.storage_key).exists()


async def test_cli_errors_exit_one(sm, storage, tmp_path, capsys):
    llm = FakeLLM()
    assert await run_cli(sm, storage, llm, "show-template", "not-a-uuid") == 1
    assert capsys.readouterr().err.strip()
    assert await run_cli(sm, storage, llm, "remove-template", str(uuid.uuid4())) == 1
    pdf = write(tmp_path, "a.pdf", b"%PDF-1.7 x")
    assert await run_cli(sm, storage, llm, "seed-template", str(pdf), "--name", "P") == 1
    assert "Unsupported" in capsys.readouterr().err


def test_cli_name_is_required(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.parse(["seed-template", "x.pptx"])
    assert exc.value.code == 2
    assert "--name" in capsys.readouterr().err


def test_main_returns_the_exit_code(monkeypatch, capsys):
    async def fake_run(args, sm, storage, llm):
        return 1

    monkeypatch.setattr(cli, "run", fake_run)
    assert cli.main(["list-templates"]) == 1
