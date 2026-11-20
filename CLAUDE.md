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
| `docs/implementation-roadmap.md` | phases 0–11, entry/exit gates, status |
| `docs/development-guidelines.md` | layout, typing, async, errors, logging, `.env` vars, git |
| `docs/testing-strategy.md` | test layers, security gates, eval metrics, fidelity checks |
| `docs/DECISIONS.md` | ADR-001…ADR-012 |

Never invent a table, column, endpoint, env var, or path name: use the docs. If code and docs disagree, fix whichever is wrong **in the same commit**.

## Layout

All code lives in `src/doc_intel/`:
- packages `core/`, `db/`, `storage/`, `ingestion/`, `embeddings/`, `vector/`, `llm/`, `templates/`, `mapping/`, `writers/`, `chat/`, `jobs/`, `api/v1/`;
- entry point `main.py`; operator CLI `cli.py` (template library).

Elsewhere:
- Pipe: `integrations/openwebui/doc_intel_pipe.py`;
- migrations: `migrations/`;
- tests: `tests/{unit,integration,security,eval,fixtures}/`.

Stack: Python 3.12 via `uv`, lint/format with `ruff`, Docker Compose for Open WebUI + PostgreSQL 16 + Qdrant, Ollama native.

## Codebase Navigation (Graphify)

- Before grepping, read `graphify-out/GRAPH_REPORT.md`, then use `graphify query "<question>"`, `graphify explain "<symbol>"` or `graphify path "A" "B"` to find the files that matter. Open only those.
- After code changes pass the gates, refresh the graph with `/graphify --update`.
- `graphify-out/` is git-ignored and never staged.

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
- `/ponytail-audit` at Phases 5 and 11;
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
- Stack: Python 3.12 via uv; ruff line length 100; no en dashes in strings or docstrings (RUF002); pytest-asyncio auto; FastAPI; SQLAlchemy 2 async; Alembic; structlog; python-pptx 1.0.2; Ollama qwen3:14b LLM_NUM_CTX=8192
- Gate: ruff check, ruff format --check, alembic check, full pytest; run with venv bin on PATH; Postgres and Qdrant must be up
- Fact-Forcing Gate hook blocks first edit or command until caller, coverage, and user's verbatim instruction are stated
- 3-tier routing: Opus for diagnose, review, docs; Sonnet tdd-guide for implementation; Haiku for lookups and handoff; pass model explicitly
- Scratch scripts go in session scratchpad; run with PYTHONPATH=.
- Phase close: never stage .handoff, .env, data, .venv, .DS_Store; ff-only merge to main then push main
- Real test deck procurement_test_template.pptx lives outside the repo; never commit it

### Key Decisions
- Decision: IR batches grouped by slide (pptx) / sheet (xlsx) / single group (docx), tables kept whole, max 40 anchors, oversized table split row-major — Reason: whole AnchorMap overflowed num_ctx; per-slide max ~2.8k tokens fits with reply
- Decision: later chunks of a table already owned by fill_table_rows dropped before LLM call — Reason: fill_table_rows writes all body rows; prevents cross-batch double-write
- Decision: cross-batch field_id collision renamed {id[:60]}_{n}, merged IR revalidated against full map — Reason: slides independently pick same ids
- Decision: BatchDraft schema allows empty fields; whole-template empty -> IRInvalid — Reason: per-batch min_length=1 would force fabricated field on finished slides and overwrite text
- Decision: num_predict = num_ctx // 2 in OllamaClient, no new env var — Reason: degenerate JSON/whitespace loop otherwise runs to LLM_TIMEOUT_SECONDS
- Decision: LLMInvalidOutput.retry = False — Reason: temperature 0 is deterministic; avoids 3 full job reruns; mapping already maps it to missing/llm_error
- Decision: progress payloads — ir: {stage, done, total, part 'slide N'/'sheet N' by position, never sheet name}; mapping: {stage, done, total, field=field_id} before each field — Reason: live per-slide/field visibility with no content in logs
- Decision: validate() allows one anchor to carry several fields only if all are replace_token with distinct tokens — Reason: real decks put several bracket tokens in one text box; old one-field rule made valid IR impossible
- Decision: render() stable-sorts replace_token items longest token first — Reason: shorter token substring (____ inside ______) destroyed longer one, causing token_not_found
- Decision: ir_invalid log redacts quoted values, keeps slug or structural pptx/docx id, else '?' — Reason: xlsx ids hold sheet names, sdt ids hold tags, unknown anchor_id is free LLM text; guardrail forbids document text in logs
- Decision: pptx anchor ids keyed by (shape_id, kind) via shared keyed_shapes(); text+table sharing an id both stay sp{id}; k-th same-kind duplicate gets sp{id}_{k} — Reason: keeps standard ids so saved IRs stay valid; extractor and writer cannot drift
- Decision: render_failed log carries mapping_run_id, format, error_type, and code only if in allowlist {anchor_unresolved, token_not_found} — Reason: diagnosable without leaking text
- Decision: one_ir_batch conftest fixture on e2e tests scripts a single IR reply — Reason: multi-slide fixtures now produce several batches; batching covered by unit tests and test_extract_template_job

<!-- handoff:learnings:end -->
