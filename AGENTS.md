# AGENTS.md

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
