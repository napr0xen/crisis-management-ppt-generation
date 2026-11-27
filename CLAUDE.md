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
| `docs/implementation-roadmap.md` | phases 0–12, entry/exit gates, status |
| `docs/development-guidelines.md` | layout, typing, async, errors, logging, `.env` vars, git |
| `docs/testing-strategy.md` | test layers, security gates, eval metrics, fidelity checks |
| `docs/DECISIONS.md` | ADR-001…ADR-019 |

Never invent a table, column, endpoint, env var, or path name: use the docs. If code and docs disagree, fix whichever is wrong **in the same commit**.

## Layout

All code lives in `src/doc_intel/`:
- packages `core/`, `db/`, `storage/`, `ingestion/`, `embeddings/`, `vector/`, `llm/`, `templates/`, `mapping/`, `writers/`, `chat/`, `jobs/`, `api/v1/`;
- entry point `main.py`; operator CLI `cli.py` (template library).

Elsewhere:
- Pipe: `integrations/openwebui/doc_intel_pipe.py`;
- custom web UI: `frontend/` (React + TypeScript + Vite + Tailwind, `npm run build`);
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
- `/ponytail-audit` at Phases 5 and 12;
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
- Gate: .venv/bin on PATH; uv run ruff check ., ruff format --check ., alembic check, pytest tests/unit tests/integration tests/security (~1.5 min, Postgres+Qdrant)
- Live bench: uv run pytest tests/eval/bench -m eval (~2.5-10 min, MLX :8080 + Ollama embeddings :11434); run in background; results tests/eval/report.json["bench"] git-ignored
- ECC quality gate: pipe hook JSON into ~/.claude/plugins/cache/ecc/ecc/2.2.3/scripts/hooks/quality-gate.js with PATH including .venv/bin
- Phase branches phase-<N>-<slug>, never deleted; commit only on phase branch; main ff only on user approval
- Fact-Forcing hook: state facts before first Bash/Edit
- Derived failure codes are free strings (summary_row, exclude_unverified, rows_incomplete, ...); logs carry codes only, never text

### Key Decisions
- Decision: ADR-018 in docs/DECISIONS.md, not docs/adr/ — Reason: single ADR index exists there.
- Decision: derived slot reads ALL table chunks via QdrantStore.table_chunks (tenant-filtered) — Reason: retrieval top-k would silently drop rows from a sum.
- Decision: LLM binds only table id + exact headers (LLMDerivation{table, not_found, derivation}), never returns numbers; preset IRField.derivation skips LLM — Reason: IR builder cannot see source headers (multilingual).
- Decision: CR/DR are tags not signs; diff without minus = sum CR minus sum DR — Reason: tag in a Debit column would otherwise flip sign and corrupt net.
- Decision: Computed.number carries exact Decimal, stored int/float, never re-parsed from rendered text — Reason: re-parsing stored European 12.000 as 12 is a 1000x bug.
- Decision: fail closed to missing with existing MissingReasons, codes logged only (row gaps, ambiguous/mixed number conventions, parentheses negatives, mixed CR/DR without tag, unparseable cells) — Reason: no MissingReason enum/API change.
- Decision: one Evidence per row with new optional Evidence.row_id; derived result populated — Reason: inputs verified rows, arithmetic deterministic.
- Decision: summary row = value equal to running or cumulative sum of >=2 earlier values → fail closed summary_row + one corrective re-binding naming row ids — Reason: never auto-exclude; language-agnostic.
- Decision: exclude_rows ids must be detected summary rows else exclude_unverified — Reason: LLM could drop a genuine transaction and a short total would still read as verified.
- Decision: writer renders computed currency in cited rows' convention/affix via derived.render_like (most common affix pair) — Reason: source_text finds no quote stating a total; deck showed 4295.5.
- Decision: IR_BUILDER_VERSION 4→5 — Reason: build_ir output gained derived mode.
- Decision: bench ingest opt-in structured: true routes pipe tables through CSV parse — Reason: md parser keeps pipe tables as plain text; existing cases unchanged.

<!-- handoff:learnings:end -->
