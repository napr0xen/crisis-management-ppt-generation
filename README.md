# Document Intelligence & Template Mapping Platform

A local-first, domain-agnostic RAG platform. You chat in **Open WebUI**: upload source documents (PDF, DOCX, PPTX, XLSX, TXT, CSV, MD), ask questions, and get answers with **verified, verbatim citations**. You can also name a **PPTX, DOCX, or XLSX template** and get it back filled in place, with styles and layout preserved. Anything the sources can't prove is marked `[MISSING: …]` or `[CONFLICT: See Chat Summary]`, never guessed.

> The repo name reflects the first use case (crisis-management slide decks). Nothing in the platform is domain-specific.

**Status:** Phases 1-9 complete (foundation, ingestion, embeddings + Qdrant, grounded Q&A, template intelligence, evidence mapping, surgical PPTX/DOCX/XLSX writers with signed downloads, Open WebUI Pipe, template library). See [docs/implementation-roadmap.md](docs/implementation-roadmap.md).

## How it works

```
Open WebUI chat ──Pipe Function──▶ FastAPI (native, MPS) ──▶ PostgreSQL 16 · Qdrant · ./data/storage
                                       │
                                       ├─ Docling + RapidOCR → structural chunks → qwen3-embedding (Ollama) + BM25 → Qdrant (RRF)
                                       ├─ Q&A: hybrid retrieval → LLM → deterministic quote verification
                                       └─ Templates: Anchor Map → Template IR → per-field evidence mapping
                                                    → surgical in-place write → signed download link
                                       ▼
                                Ollama (native, Metal) — swappable to OpenAI/Anthropic via .env
```

- **Docker Compose:** Open WebUI, PostgreSQL 16, Qdrant.
- **Native macOS (`uv`, Python 3.12):** FastAPI backend with Docling and the optional BGE reranker; Ollama for the LLM and `qwen3-embedding` embeddings.
- **Isolation:** every row and vector is scoped by `user_id` + `project_id`; one project per Open WebUI chat.

## Target machine: MacBook Pro M5 Pro, 24 GB

- ML runs natively because Docker on macOS has no Metal/MPS access.
- Cap Docker Desktop's VM memory at **3 GB**.
- Default LLM: `qwen3:14b` (Q4_K_M, 10 GB resident at 8k context): `ollama pull qwen3:14b`. Use `qwen3:4b` (3.9 GB) if memory is tight.
- Jobs run one at a time (`JOB_CONCURRENCY=1`). Docling and the reranker load lazily.
- Start Ollama with `OLLAMA_NUM_PARALLEL=1` and `OLLAMA_KEEP_ALIVE=10m`.
- Full budget table: [docs/architecture.md § 6](docs/architecture.md#6-memory-budget-24-gb-unified-memory).

## Quick start

```bash
cp .env.example .env          # set POSTGRES_PASSWORD (also in DATABASE_URL), PIPE_SHARED_SECRET, SIGNING_SECRET
docker compose up -d          # Open WebUI :3000, Postgres :5432, Qdrant :6333 (localhost only)
uv sync
uv run alembic upgrade head
uv run uvicorn doc_intel.main:app --host 127.0.0.1 --port 8000 --no-access-log
curl 127.0.0.1:8000/v1/health # {"status":"ok","postgres":"ok","qdrant":"ok",...}
uv run pytest                 # integration tests use a throwaway doc_intel_test database
```

## Open WebUI Pipe setup

The Pipe is the only bridge between Open WebUI and the backend. Its code is [integrations/openwebui/doc_intel_pipe.py](integrations/openwebui/doc_intel_pipe.py).

1. Start the services and the API on the host (see Quick start): `docker compose up -d`, then `uv run uvicorn doc_intel.main:app --host 127.0.0.1 --port 8000 --no-access-log`.
2. Open http://localhost:3000 → **Admin Panel → Functions → "+"** (new function). Paste the whole content of `integrations/openwebui/doc_intel_pipe.py`, save, and enable the function.
3. Open the function's **Valves** (gear icon) and set:
   - `BACKEND_URL` = `http://host.docker.internal:8000`
   - `PIPE_SHARED_SECRET` = the same value as `PIPE_SHARED_SECRET` in `.env` (at least 32 characters)
   - `TIMEOUT_S` = `1800`
4. Turn off Open WebUI's built-in file handling for the Pipe model, so it does not inject file text itself: **Workspace → Models → edit the Pipe model → Capabilities**, untick **File Context** and **Builtin Tools** (`meta.capabilities` `file_context: false`, `builtin_tools: false`). The API alternative is `POST /api/v1/models/create` with `id` set to the Pipe id. See [architecture.md § 5](docs/architecture.md#5-open-webui-pipe-design).
5. Use it: start a new chat, select the Pipe model, attach your documents and a template, and say "fill the template". The summary ends with a download link to `http://127.0.0.1:8000/...`. Open that link in the host browser; it expires, so download the file soon.
6. Template library (optional): register reusable templates once, so users can attach only source documents and say "make a powerpoint out of this". Registration extracts the template fields up front (Ollama must be running), so chat runs skip that step.
   ```bash
   uv run python -m doc_intel.cli seed-template path/to/deck.pptx --name "Procurement Review" --description "Quarterly procurement status deck"
   uv run python -m doc_intel.cli list-templates
   uv run python -m doc_intel.cli show-template <id>
   uv run python -m doc_intel.cli remove-template <id>
   ```
   `--name` and `--description` are what users see in the chat picker. A template attached to the chat always takes precedence over the library.
7. Edits: after a filled document, correct it in the same chat, for example "set the owner to Ann", "use the first value for customers affected" or "clear the date". Each edit renders a new version (`…_v2.pptx`) with its own download link; earlier versions stay downloadable. A typed value must be written in your message: the model never fills one in for you.

Conventions: [docs/development-guidelines.md](docs/development-guidelines.md).

## Documentation

| Doc | Contents |
|---|---|
| [requirements.md](docs/requirements.md) | functional/non-functional requirements, formats, security rules, out of scope |
| [architecture.md](docs/architecture.md) | components, ingestion/Q&A/mapping flows, Open WebUI Pipe, RAM budget |
| [system-design.md](docs/system-design.md) | modules, `AnchorMap`, `TemplateIR`, `FieldMappingResult`, state machine, writers |
| [data-model.md](docs/data-model.md) | PostgreSQL schema, Qdrant collection, storage layout |
| [api-specification.md](docs/api-specification.md) | endpoints, auth headers, SSE events, schemas |
| [implementation-roadmap.md](docs/implementation-roadmap.md) | phases 0–12 with entry/exit gates |
| [development-guidelines.md](docs/development-guidelines.md) | layout, typing, async, errors, logging, config, git |
| [testing-strategy.md](docs/testing-strategy.md) | unit, integration, isolation, RAG eval, template fidelity |
| [DECISIONS.md](docs/DECISIONS.md) | ADR-001 … ADR-012 |

Contributor and agent rules: [CLAUDE.md](CLAUDE.md).
