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
| `docs/DECISIONS.md` | ADR-001…ADR-014 |

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
- Gate: .venv/bin on PATH; uv run ruff check ., ruff format --check ., alembic check, pytest tests/unit tests/integration tests/security (~1.5 min, needs Postgres+Qdrant)
- Live bench: uv run pytest tests/eval/bench -m eval (needs MLX :8080), results in tests/eval/report.json["bench"] (git-ignored)
- Phase branches phase-<N>-<slug>, never deleted; commit only when user asks; main ff only on approval
- Reason codes are free strings: row_label_mismatch, row_mixed added; logs carry codes only, never text
- Old Qdrant points lack rows/block_ids → checks are no-ops until re-ingest

### Key Decisions
- Decision: provenance ids never in chunk text — Reason: keeps embeddings, BM25 and number parsing clean; id digits must never verify a number.
- Decision: LLMQuote strips only displayed row ids ([P1-T2-R04]), not [B..] — Reason: [B12] is a real reference marker in documents.
- Decision: row_label_mismatch conservative, reject only when no value cell matches slot label AND another labelled cell states a value of slot type — Reason: avoids false rejects (multilingual, text-only rows).
- Decision: check lives in engine._rejection — Reason: it is the real acceptance gate (called by _candidates), so gate, log and retry note agree.
- Decision: _stray_cells only for multi-row record groups — Reason: keyed cards (one-row groups) take slots from different source rows by design.
- Decision: bench metrics recorded, not gated; only fabricated_citations==0 gated — Reason: guardrail 2.
- Decision: gold partial with matching value not counted populated-but-wrong — Reason: status mismatch already in field_accuracy.
- Decision: bench and test_mapping_eval share matches() and ingest() — Reason: avoid drift.
- Decision: Docling cell text once at top-left slot from table_cells; join same-header tables on consecutive pages after drop_repeated — Reason: avoid duplicated cell text and split-table row loss.

<!-- handoff:learnings:end -->
