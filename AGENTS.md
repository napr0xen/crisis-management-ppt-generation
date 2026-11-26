# AGENTS.md

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
