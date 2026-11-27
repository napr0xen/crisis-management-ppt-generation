# AGENTS.md

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
