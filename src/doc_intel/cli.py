"""Operator CLI for the template library: `uv run python -m doc_intel.cli <command>`."""

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

import httpx

from doc_intel.core.config import get_settings
from doc_intel.core.errors import AppError, NotFound
from doc_intel.db import repo
from doc_intel.db.models import LibraryTemplate
from doc_intel.db.session import make_engine
from doc_intel.llm.gateway import LLMClient, make_llm_client
from doc_intel.storage.base import StorageBackend
from doc_intel.storage.local import LocalStorage
from doc_intel.templates import library


def parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="doc_intel.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser("seed-template", help="analyze a template file and add it to the library")
    seed.add_argument("path", type=Path, help="a PPTX, DOCX or XLSX file")
    seed.add_argument(
        "--name", required=True, help="unique name users see in the Open WebUI template picker"
    )
    seed.add_argument("--description", default="", help="short text shown next to the name")
    seed.add_argument("--category", default=None, help="optional grouping label")
    sub.add_parser("list-templates", help="list the library templates")
    sub.add_parser(
        "reset-library", help="delete every library template (files in TEMPLATES_DIR stay)"
    )
    for command, text in (
        ("show-template", "show one template"),
        ("remove-template", "delete one"),
    ):
        sub.add_parser(command, help=text).add_argument("id", help="library template id")
    return parser.parse_args(argv)


def _template_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise NotFound("Not a valid template id.") from None


def _print_stderr(text: str) -> None:
    print(text, file=sys.stderr)


async def _seed(args, sessionmaker, storage, llm, max_bytes: int) -> None:
    async def on_batch(done: int, total: int, part: str | None) -> None:
        _print_stderr(f"Analyzing template fields: {part or 'template'} ({done + 1}/{total})")

    lib = await library.register(
        sessionmaker,
        storage,
        llm,
        args.path,
        name=args.name,
        description=args.description,
        category=args.category,
        max_bytes=max_bytes,
        on_batch=on_batch,
    )
    print(f"Added {lib.id}: {lib.name} ({lib.format}), {len(lib.ir['fields'])} fields")


async def run(args, sessionmaker, storage: StorageBackend, llm: LLMClient) -> int:
    """Execute one parsed command; an `AppError` is printed and becomes exit code 1."""
    try:
        if args.command == "seed-template":
            await _seed(args, sessionmaker, storage, llm, get_settings().max_upload_mb << 20)
        elif args.command == "list-templates":
            async with sessionmaker() as session:
                for t in await repo.list_library_templates(session):
                    print(
                        f"{t.id}  {t.name}  {t.format}  {t.category or '-'}  {len(t.ir['fields'])}"
                    )
        elif args.command == "show-template":
            async with sessionmaker() as session:
                _show(await repo.get_library_template(session, _template_id(args.id)))
        elif args.command == "reset-library":
            async with sessionmaker.begin() as session:
                rows = await repo.list_library_templates(session)
                for row in rows:
                    await session.delete(row)
            for row in rows:
                await asyncio.to_thread(storage.delete, row.storage_key)
            print(f"Removed {len(rows)} library templates")
        else:
            async with sessionmaker.begin() as session:
                lib = await repo.delete_library_template(session, _template_id(args.id))
            await asyncio.to_thread(storage.delete, lib.storage_key)
            print(f"Removed {lib.id}: {lib.name}")
    except AppError as exc:
        _print_stderr(exc.message)
        return 1
    return 0


def _show(lib: LibraryTemplate) -> None:
    print(f"name: {lib.name}\ndescription: {lib.description}\ncategory: {lib.category or '-'}")
    print(f"format: {lib.format}\nfilename: {lib.filename}\nanchor_count: {lib.anchor_count}")
    for f in lib.ir["fields"]:
        print(f"{f['field_id']}: {f['label']}")


async def _main(args: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    settings = get_settings()
    engine = make_engine(settings.database_url)
    try:
        async with httpx.AsyncClient() as http:
            return await run(
                args,
                async_sessionmaker(engine, expire_on_commit=False),
                LocalStorage(settings.storage_root),
                make_llm_client(settings, http),
            )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(parse(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
