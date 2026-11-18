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
| `docs/DECISIONS.md` | ADR-001…ADR-010 |

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
