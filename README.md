# Document Intelligence & Template Mapping Platform

A local-first, domain-agnostic RAG platform. You chat in **Open WebUI**: upload source documents (PDF, DOCX, PPTX, XLSX, TXT, CSV, MD), ask questions, and get answers with **verified, verbatim citations**. You can also name a **PPTX, DOCX, or XLSX template** and get it back filled in place, with styles and layout preserved. Anything the sources can't prove is marked `[MISSING: …]` or `[CONFLICT: See Chat Summary]`, never guessed.

> The repo name reflects the first use case (crisis-management slide decks). Nothing in the platform is domain-specific.

**Status:** Phase 1 complete (foundation, infrastructure, database). See [docs/implementation-roadmap.md](docs/implementation-roadmap.md).

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
- Default LLM: `qwen2.5:14b-instruct` (Q4_K_M, ~9 GB). Use `qwen2.5:7b-instruct` if you enable the reranker or memory is tight.
- Jobs run one at a time (`JOB_CONCURRENCY=1`). Docling and the reranker load lazily.
- Start Ollama with `OLLAMA_NUM_PARALLEL=1` and `OLLAMA_KEEP_ALIVE=10m`.
- Full budget table: [docs/architecture.md § 6](docs/architecture.md#6-memory-budget-24-gb-unified-memory).

## Quick start (Phase 1)

```bash
cp .env.example .env          # set POSTGRES_PASSWORD (also in DATABASE_URL), PIPE_SHARED_SECRET, SIGNING_SECRET
docker compose up -d          # Open WebUI :3000, Postgres :5432, Qdrant :6333 (localhost only)
uv sync
uv run alembic upgrade head
uv run uvicorn doc_intel.main:app --host 127.0.0.1 --port 8000
curl 127.0.0.1:8000/v1/health # {"status":"ok","postgres":"ok","qdrant":"ok",...}
uv run pytest                 # integration tests use a throwaway doc_intel_test database
```

Conventions: [docs/development-guidelines.md](docs/development-guidelines.md).

## Documentation

| Doc | Contents |
|---|---|
| [requirements.md](docs/requirements.md) | functional/non-functional requirements, formats, security rules, out of scope |
| [architecture.md](docs/architecture.md) | components, ingestion/Q&A/mapping flows, Open WebUI Pipe, RAM budget |
| [system-design.md](docs/system-design.md) | modules, `AnchorMap`, `TemplateIR`, `FieldMappingResult`, state machine, writers |
| [data-model.md](docs/data-model.md) | PostgreSQL schema, Qdrant collection, storage layout |
| [api-specification.md](docs/api-specification.md) | endpoints, auth headers, SSE events, schemas |
| [implementation-roadmap.md](docs/implementation-roadmap.md) | phases 0–10 with entry/exit gates |
| [development-guidelines.md](docs/development-guidelines.md) | layout, typing, async, errors, logging, config, git |
| [testing-strategy.md](docs/testing-strategy.md) | unit, integration, isolation, RAG eval, template fidelity |
| [DECISIONS.md](docs/DECISIONS.md) | ADR-001 … ADR-010 |

Contributor and agent rules: [CLAUDE.md](CLAUDE.md).
