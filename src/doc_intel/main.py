import re
import uuid
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from doc_intel.api.v1 import (
    artifacts,
    documents,
    health,
    jobs,
    mapping_runs,
    projects,
    query,
    templates,
)
from doc_intel.core.config import Settings, get_settings
from doc_intel.core.errors import AppError, error_response, install_error_handlers
from doc_intel.core.logging import configure_logging
from doc_intel.db.session import make_engine
from doc_intel.embeddings.ollama import OllamaQwen3Embedder
from doc_intel.embeddings.reranker import BGEReranker
from doc_intel.jobs.handlers import make_failure_hooks, make_handlers
from doc_intel.jobs.worker import start_worker
from doc_intel.llm.gateway import make_llm_client
from doc_intel.mapping.retriever import Retriever
from doc_intel.storage.local import LocalStorage
from doc_intel.vector.qdrant_store import QdrantStore

log = structlog.get_logger()

_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


class RequestIdMiddleware:
    """Echo a safe `X-Request-Id` (or generate one), bind it to every log line, and turn
    unhandled errors into a 500 envelope without re-raising: Starlette's outer error middleware
    would otherwise hand the exception (and its message) to the server's plain-text logger."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        sent = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        rid = sent if _SAFE_REQUEST_ID.fullmatch(sent) else uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=rid)

        started = False

        async def send_with_id(message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [(k, v) for k, v in message.get("headers", []) if k != b"x-request-id"]
                message["headers"] = [*headers, (b"x-request-id", rid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            log.error("unhandled_error", error_type=type(exc).__name__)
            if not started:
                await error_response(500, "internal", AppError.message)(
                    scope, receive, send_with_id
                )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(settings.log_level)
        app.state.settings = settings
        app.state.engine = make_engine(settings.database_url)
        app.state.sessionmaker = async_sessionmaker(app.state.engine, expire_on_commit=False)
        app.state.storage = LocalStorage(settings.storage_root)
        # Closed below by this reference: tests may swap app.state.vector_store for a fake.
        store = QdrantStore.connect(
            settings.qdrant_url, settings.qdrant_collection, settings.embedding_dimensions
        )
        app.state.vector_store = store
        try:
            await store.ensure_collection()
        except RuntimeError:
            raise  # built for another embedding setup: refuse to start, the message says the fix
        except Exception as exc:  # the API still serves; health reports Qdrant, ingest retries
            log.warning("qdrant_unavailable_at_startup", error_type=type(exc).__name__)
        async with httpx.AsyncClient() as http:
            app.state.http = http
            # Ollama loads the model on the first request and unloads it after OLLAMA_KEEP_ALIVE.
            app.state.embedder = OllamaQwen3Embedder(
                http,
                settings.embedding_base_url,
                settings.embedding_model,
                settings.embedding_dimensions,
            )
            reranker = (
                BGEReranker(settings.reranker_model, settings.reranker_device)
                if settings.reranker_enabled
                else None
            )
            app.state.retriever = Retriever(app.state.embedder, store, reranker)
            app.state.llm = make_llm_client(settings, http)
            stop_worker = (
                await start_worker(
                    app.state.sessionmaker,
                    make_handlers(
                        app.state.sessionmaker,
                        app.state.storage,
                        settings,
                        app.state.embedder,
                        store,
                        app.state.llm,
                        app.state.retriever,
                    ),
                    make_failure_hooks(app.state.sessionmaker),
                )
                if settings.job_concurrency
                else None
            )
            try:
                yield
            finally:
                if stop_worker:
                    await stop_worker()
        await store.close()
        await app.state.engine.dispose()

    app = FastAPI(title="doc_intel", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(health.router, prefix="/v1")
    app.include_router(projects.router, prefix="/v1")
    app.include_router(documents.router, prefix="/v1")
    app.include_router(jobs.router, prefix="/v1")
    app.include_router(query.router, prefix="/v1")
    app.include_router(templates.router, prefix="/v1")
    app.include_router(mapping_runs.router, prefix="/v1")
    app.include_router(artifacts.router, prefix="/v1")
    return app


app = create_app()
