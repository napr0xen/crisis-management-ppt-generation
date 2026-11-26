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
- Gate: .venv/bin on PATH; ruff check ., ruff format --check ., alembic check, full pytest (~11 min, Postgres+Qdrant+live MLX :8080)
- LLM: openai_compat mlx-community/Qwen3-14B-4bit; FakeLLM in tests routes structured replies by schema title (now BatchReply for IR)
- Never print DB URL: make_engine(settings.database_url), not settings object
- Diagnostics print shapes/booleans only, never document text
- Commit only when user asks; never stage .env, data/, .venv, .handoff

### Key Decisions
- Decision: typed values verified only against verified quotes, never whole retrieved context — Reason: guardrail 2, matching context would let a value cite a quote that never states it
- Decision: quote side reads ambiguous numbers both ways (one thousands group, DR/CR or trailing minus either sign), value side one way — Reason: value must state exactly one number
- Decision: RETRIEVAL_CONTEXT_TOKENS default stays 4500 — Reason: at 3072 mapping eval lost a seeded conflict
- Decision: curate tiers pinned opening chunks, then scored hits, then 0.0-score neighbors — Reason: neighbors crowded out ranked hits; cover chunks must survive
- Decision: IRField.mode default extractive stored, required with description in ir_builder.BatchReply/LLMIRField — Reason: optional mode set on 0/116 fields by Qwen3-14B
- Decision: synthesis text/list slots skip extraction and heading check, mapped alone outside batches — Reason: verbatim lookup and heading check fail written summaries
- Decision: synthesis item with word share under 0.6 kept only if it states >=1 number all quoted, slot partial — Reason: table-row quotes share few words; number-free items had nothing verified
- Decision: derived synthesis numbers (sum/diff of two quoted) always partial — Reason: no quote states them, flag for review
- Decision: no high_score_miss escalation for synthesis slots or all-synthesis groups — Reason: thinking retry cannot find a verbatim value that does not exist
- Decision: IR_BUILDER_VERSION 2->3 — Reason: build_ir output gained mode

<!-- handoff:learnings:end -->
