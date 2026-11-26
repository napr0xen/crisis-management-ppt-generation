# AGENTS.md

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
