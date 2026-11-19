# CLAUDE.md

Project: local-first, domain-agnostic **Document Intelligence & Template Mapping Platform** (RAG). Users chat in Open WebUI; a thin Pipe forwards to a native FastAPI backend that answers with verified citations and fills PPTX/DOCX/XLSX templates in place.

**Read `docs/` before coding. The docs are the source of truth.**

| Doc | Owns |
|---|---|
| `docs/data-model.md` | every table, column, Qdrant collection/payload key, storage path |
| `docs/api-specification.md` | every endpoint, header, request/response shape, SSE events |
| `docs/system-design.md` | modules, `AnchorMap`, `TemplateIR`, `FieldMappingResult`, state machine, writers |
| `docs/architecture.md` | components, data flows, Pipe design, 24 GB RAM budget |
| `docs/requirements.md` | FR/NFR, formats, security rules, out of scope |
| `docs/implementation-roadmap.md` | phases 0–10, entry/exit gates, status |
| `docs/development-guidelines.md` | layout, typing, async, errors, logging, `.env` vars, git |
| `docs/testing-strategy.md` | test layers, security gates, eval metrics, fidelity checks |
| `docs/DECISIONS.md` | ADR-001…ADR-011 |

Never invent a table, column, endpoint, env var, or path name: use the docs. If code and docs disagree, fix whichever is wrong **in the same commit**.

## Layout

All code lives in `src/doc_intel/`:
- packages `core/`, `db/`, `storage/`, `ingestion/`, `embeddings/`, `vector/`, `llm/`, `templates/`, `mapping/`, `writers/`, `chat/`, `jobs/`, `api/v1/`;
- entry point `main.py`.

Elsewhere:
- Pipe: `integrations/openwebui/doc_intel_pipe.py`;
- migrations: `migrations/`;
- tests: `tests/{unit,integration,security,eval,fixtures}/`.

Stack: Python 3.12 via `uv`, lint/format with `ruff`, Docker Compose for Open WebUI + PostgreSQL 16 + Qdrant, Ollama native.

## Workflow (every phase)

Work **only** on the current phase in `docs/implementation-roadmap.md`. Don't build later-phase code early.

**Git branching (mandatory):**
- Each phase is developed on its own branch `phase-<N>-<slug>` (`phase-0-docs`, `phase-1-foundation`, `phase-2-ingestion`, `phase-3-embeddings-qdrant`, …).
- Remote: `https://github.com/napr0xen/crisis-management-ppt-generation.git`.
- When the phase gate passes (pytest, ruff, `/ponytail-review`, `/ecc:quality-gate`):
  1. mark the phase complete in the roadmap;
  2. commit and push `origin phase-<N>-<slug>`;
  3. fast-forward `main` to it (`git merge --ff-only`) and push `origin main`.
- Phase branches are never deleted. Details: `docs/development-guidelines.md` § 11.

**ECC sequence (mandatory):**
1. `/ecc:plan`, then wait for approval;
2. `/ecc:tdd` (failing test first; if not installed, use the `ecc:tdd-workflow` skill), then implement;
3. `/ecc:code-review`;
4. `/ecc:verify` (run the phase exit gate; if not installed, use the `ecc:verification-loop` skill);
5. `/ecc:update-docs`.

**Ponytail (mandatory):**
- `/ponytail full` during implementation: the smallest complete change, no speculative code;
- `/ponytail-review` at the end of every phase;
- `/ponytail-audit` at Phases 5 and 10;
- known limits get a comment: `# shortcut: <the limit>, <when to upgrade>`.

## Guardrails (non-negotiable)

1. **Strict tenant isolation.** Every DB query and every Qdrant operation filters on `user_id` **and** `project_id`. Cross-tenant access returns `404`. Qdrant is accessed only through `src/doc_intel/vector/qdrant_store.py`.
2. **Zero fabricated citations.** A value is `populated` only with deterministically verified quotes, unless the user set it. Otherwise it is `partial`, `missing`, or `conflict`. Never guess. Never auto-resolve conflicts.
3. **No placeholder code marked done.** No `pass`/`TODO`/stub behind a passing test or a checked box. Unfinished means unfinished.
4. **Never modify a completed phase without cause** (bug, failing gate, or doc change), and state the cause in the commit.
5. **Secrets and data stay out of git:** `.env`, `data/`, `.venv/`, model weights, real documents.
6. **No document text, quotes, prompts, completions, filenames, or secrets in logs.**
7. **A phase is done only when its exit gate passes**, with the real command output shown.

<!-- handoff:learnings:begin -->
## Session Learnings (auto-updated by handoff)

### Implicit Rules
- Stack: Python 3.12 via uv, ruff (E,F,I,B,UP,S,ASYNC,RUF; line 100), pytest + pytest-asyncio auto mode; FastAPI 0.164, SQLAlchemy 2 async + asyncpg, Alembic, structlog JSON with redaction, Docling 2.150 + RapidOCR, qdrant-client 1.20, Ollama 0.40.13
- Docker compose: postgres:16, qdrant v1.20.2, open-webui v0.13.4, named volumes, localhost-only ports; Docker Desktop VM is 8.3 GB (docs recommend 3 GB)
- Tests: throwaway doc_intel_test DB, doc_chunks_test Qdrant collection, JOB_CONCURRENCY=0, temp STORAGE_ROOT; markers integration/llm/eval/slow; uv run pytest runs all and needs Docker + Ollama
- Env vars: DATABASE_URL, QDRANT_URL, QDRANT_COLLECTION, STORAGE_ROOT, PIPE_SHARED_SECRET, SIGNING_SECRET, SIGNED_URL_TTL_S, LOG_LEVEL, MAX_UPLOAD_MB, CHUNK_MAX_TOKENS, EMBEDDING_BASE_URL, EMBEDDING_MODEL, EMBEDDING_DIMENSIONS, CHUNK_TOKENIZER, RERANKER_ENABLED/MODEL/DEVICE, JOB_CONCURRENCY
- Fact-Forcing Gate hook requires stating facts before first Bash/new-file writes and destructive commands

### Key Decisions
- Decision: one transaction per request via Depends(get_session, scope='function'); repo functions flush only — Reason: atomic multi-row writes, commit before response
- Decision: worker claims jobs cross-tenant (documented exception), re-queues all running jobs at startup, MAX_JOB_ATTEMPTS=3, per-kind failure hooks mark document failed — Reason: no stuck 'parsing' docs after crashes/OOM
- Decision: QdrantStore.connect/close, update_filter tenant guard, UPSERT_BATCH=256, ensure_collection once per store (also run by hybrid_search) and refuses mismatched dims/no-IDF collection — Reason: isolation, 32 MB Qdrant request cap (~1250 points), avoid silent bad ranking
- Decision: embeddings = Ollama qwen3-embedding:0.6b /api/embed, dimensions=1024, num_ctx=1024, Qwen3 instruction on queries only; sparse = hashed-token BM25, Qdrant modifier=IDF — Reason: user request (ADR-011); num_ctx 1024 cuts Ollama footprint 1.24→0.81 GB
- Decision: Embedder protocol async embed_documents/embed_query; CHUNK_TOKENIZER=Qwen/Qwen3-Embedding-0.6B separate from EMBEDDING_MODEL — Reason: Ollama model name not a HF tokenizer
- Decision: reranker optional/off, now on plain transformers (RERANKER_DEVICE) — Reason: FlagEmbedding removed
- Decision: DELETE document flushes row delete, then deletes Qdrant points, then commits (503 if Qdrant down) — Reason: 409/404 never wipe vectors; deleted text never stays searchable
- Decision: BYPASS_EMBEDDING_AND_RETRIEVAL insufficient; Phase 8 must create Pipe model override capabilities file_context=false, builtin_tools=false — Reason: verified live in Open WebUI v0.13.4

<!-- handoff:learnings:end -->
