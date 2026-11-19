# Development Guidelines

Rules for code under `src/doc_intel/`. Module responsibilities: [system-design.md § 1](system-design.md#1-module-breakdown-srcdoc_intel). Names: [data-model.md](data-model.md), [api-specification.md](api-specification.md).

## 1. Repository layout

```
.
├── CLAUDE.md  README.md  pyproject.toml  uv.lock  .python-version  alembic.ini
├── docker-compose.yml  .env.example  .gitignore
├── docs/                         # source of truth (this folder)
├── migrations/                   # Alembic
├── integrations/openwebui/doc_intel_pipe.py
├── src/doc_intel/
│   ├── core/ db/ storage/ ingestion/ embeddings/ vector/ llm/
│   ├── templates/ mapping/ writers/ chat/ jobs/ api/v1/
│   └── main.py
├── tests/{unit,integration,security,eval,fixtures}/
└── data/                         # git-ignored: storage/ (DB/vector/UI data are Docker named volumes)
```

## 2. Tooling

- Python **3.12**, managed by `uv`:
  - `uv sync`, `uv run …`, `uv add <pkg>`;
  - never edit `uv.lock` by hand;
  - never `pip install`.
- Lint/format: `uv run ruff check .` and `uv run ruff format .`. Ruff rule sets: `E,F,I,B,UP,S,ASYNC,RUF`, line length 100.
- Run the API: `uv run uvicorn doc_intel.main:app --host 127.0.0.1 --port 8000`.
- Infra: `docker compose up -d` (Open WebUI, Postgres, Qdrant). Ollama runs natively (`ollama serve`).
- Dependencies: don't add a package for something a few lines or the stdlib can do. Every new dependency is mentioned in the PR description.

## 3. Typing & Pydantic v2

- Full type hints on all public functions. Use `X | None`, `list[str]`, `from __future__ import annotations` is not needed on 3.12.
- Pydantic v2 models for every API body, every JSONB payload (`AnchorMap`, `TemplateIR`, `FieldMappingResult`, `EditOp`), and every LLM structured output. Use `model_config = ConfigDict(extra="forbid")` on anything from outside (requests, LLM output).
- Cross-field invariants (e.g. `populated` ⇒ evidence) are `@model_validator(mode="after")`, never ad-hoc checks in callers.
- IDs are `uuid.UUID` in code, strings in JSON.
- No `Any` in signatures without a one-line reason comment.

## 4. Async conventions

- FastAPI handlers, DB (SQLAlchemy 2.0 async + `asyncpg`), Qdrant (`AsyncQdrantClient`), and HTTP (`httpx.AsyncClient`) are async.
- CPU/GPU-bound work (Docling, chunk tokenizing, reranker, writers) runs via `await asyncio.to_thread(...)`; embeddings are async HTTP calls to Ollama. Never call it directly in the event loop.
- One `httpx.AsyncClient`, DB engine, and Qdrant client per process, created in `main.py` lifespan and injected through dependencies.
- No fire-and-forget `asyncio.create_task` outside `jobs/worker.py`. Long work goes into `jobs`.
- **One transaction per request.**
  - Routes take `Depends(get_session, scope="function")`: it commits when the handler returns, before the response is sent, and rolls back if the handler raises.
  - `db/repo.py` functions `flush()` and never `commit()`, so multi-row writes (e.g. `documents` + `jobs`) are atomic.

## 5. Tenancy in code

- `TenantContext(user_id, project_id)` is built once per request by a dependency, after the project lookup that returns 404 for non-owners.
- Every repository function and every `qdrant_store` function takes `ctx: TenantContext` as its first argument and filters on both keys. There are no `get_by_id(id)` variants without `ctx`. The only exception is `artifacts` lookup in the signed download, which verifies the HMAC against the row's `user_id` before returning anything.
- Never import `qdrant_client` outside `vector/qdrant_store.py` (enforced by a test that greps imports).

## 6. Error handling

- Raise `AppError(code, http_status, message)` subclasses (`NotFound`, `Unauthorized`, `ValidationFailed`, `Conflict`, `PayloadTooLarge`, `UnsupportedMediaType`, `UpstreamUnavailable`). One exception handler renders the error envelope from [api-specification.md](api-specification.md#errors).
- Catch only what you can handle. In job handlers, any exception marks the job `failed` with an `error_code` and a safe `error_message`, then re-raises to the worker's logger. Never swallow silently.
- Field-level LLM failures in mapping become `missing` (`llm_error`). They never fail the run (system-design § 7).
- Never put document text, quotes, prompts, or filenames into exception messages that reach logs or responses.
- Engines are created with `hide_parameters=True` (`db/session.py` `make_engine`), so DB errors never carry bound values.
- `RequestIdMiddleware` turns unhandled errors into the 500 envelope and logs only `error_type`. It doesn't re-raise, so the server never prints the traceback.

## 7. Structured logging

- `structlog` with JSON output to stdout. Every line carries `event`, `level`, `ts`, and, when known, `request_id`, `user_id`, `project_id`, `document_id`, `job_id`, `mapping_run_id`.
- **Allowed:** ids, counts, durations (`duration_ms`), statuses, error codes, model names.
- **Forbidden:** document text, chunk text, quotes, prompts, LLM completions, filenames, file bytes, secrets, auth headers. A redaction processor drops any key in `{"text", "quote", "prompt", "completion", "content", "filename", "authorization"}`. A test asserts this.
- LLM calls log `model`, `prompt_tokens`, `completion_tokens`, `duration_ms` only.

## 8. Configuration (`.env`, read by `core/config.py` `Settings` via pydantic-settings)

`Settings` declares only the variables the code reads so far. Each phase adds its own fields and `.env.example` entries, using the names below.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://doc_intel:doc_intel@127.0.0.1:5432/doc_intel` | app DB |
| `QDRANT_URL` | `http://127.0.0.1:6333` | |
| `QDRANT_COLLECTION` | `doc_chunks` | tests set `doc_chunks_test` |
| `STORAGE_ROOT` | `./data/storage` | `LocalStorage` root |
| `PIPE_SHARED_SECRET` | — (required) | bearer secret shared with the Pipe |
| `SIGNING_SECRET` | — (required) | HMAC key for download URLs |
| `SIGNED_URL_TTL_S` | `900` | |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | prefix for `download_url` |
| `LLM_PROVIDER` | `ollama` | `ollama` \| `openai` \| `anthropic` |
| `LLM_BASE_URL` | `http://127.0.0.1:11434/v1` | ignored for `anthropic` |
| `LLM_MODEL` | `qwen2.5:14b-instruct` | |
| `LLM_API_KEY` | empty | required for `openai`/`anthropic` |
| `LLM_NUM_CTX` | `8192` | Ollama context window |
| `EMBEDDING_BASE_URL` | `http://localhost:11434` | Ollama |
| `EMBEDDING_MODEL` | `qwen3-embedding:0.6b` | any Ollama embedding model; changing it needs a re-ingest |
| `EMBEDDING_DIMENSIONS` | `1024` | sent to Ollama (Matryoshka); must match the Qdrant collection |
| `CHUNK_TOKENIZER` | `Qwen/Qwen3-Embedding-0.6B` | Hugging Face tokenizer that sizes chunks |
| `RERANKER_DEVICE` | `mps` | `mps` \| `cpu` |
| `RERANKER_ENABLED` | `false` | |
| `RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3` | |
| `CHUNK_MAX_TOKENS` | `512` | |
| `MAX_UPLOAD_MB` | `100` | |
| `JOB_CONCURRENCY` | `1` | `0` disables the in-process worker (tests set it, then drive jobs with `run_one`); max 1 on 24 GB |
| `MISSING_MARKER` | `[MISSING: {label}]` | |
| `CONFLICT_MARKER` | `[CONFLICT: See Chat Summary]` | |
| `LOG_LEVEL` | `INFO` | |

Docker Compose has its own variables in the same `.env`: `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `OPENWEBUI_IMAGE_TAG`, `QDRANT_IMAGE_TAG`. `Settings` fails fast at startup if a required variable is missing.

## 9. Prompts

- Prompts live in `src/doc_intel/llm/prompts/*.md`, loaded by name. No prompt strings inline in logic.
- Every prompt that consumes document text says: use only the provided chunks, copy quotes verbatim, return `not_found` instead of guessing.

## 10. Code style

- Smallest complete change (Ponytail). No speculative abstractions, options, or "for later" code.
- A known limit gets a comment: `# shortcut: <the limit>, <when to upgrade>`.
- Comments explain *why*, not *what*.
- No `pass`, `TODO`, `NotImplementedError`, or stub behind a passing test or a checked box.

## 11. Git hygiene

- Remote: `origin` = `https://github.com/napr0xen/crisis-management-ppt-generation.git`.
- **One branch per phase**, named `phase-<N>-<slug>` with the slug from the roadmap phase name: `phase-0-docs`, `phase-1-foundation`, `phase-2-ingestion`, `phase-3-embeddings-qdrant`, … through Phase 10.
- Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- **End of every phase**, only after `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `/ponytail-review` and `/ecc:quality-gate` pass:
  1. mark the phase complete in `docs/implementation-roadmap.md`;
  2. commit the phase work on `phase-<N>-<slug>` and `git push origin phase-<N>-<slug>`;
  3. `git checkout main && git merge --ff-only phase-<N>-<slug> && git push origin main`.

  `main` always tracks the latest verified phase. Phase branches are never deleted, so every phase stays preserved on GitHub.
- Never commit `.env`, `data/`, `.venv/`, model weights, or real customer documents. Test fixtures are synthetic.
- Each commit keeps code and docs consistent. If a commit changes a name, it updates `docs/` too.
- Merge a phase only after its exit gate passes, with the command output in the PR description.
