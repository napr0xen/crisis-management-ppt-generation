# Testing Strategy

Every phase is test-first (`/ecc:tdd`). Exit gates per phase are in [implementation-roadmap.md](implementation-roadmap.md). Names come from [data-model.md](data-model.md) and [api-specification.md](api-specification.md).

## 1. Layout & markers

```
tests/
├── unit/          # pure logic, no network, no Docker; runs in < 30 s
├── integration/   # real Postgres + Qdrant (docker compose), FakeLLM
├── security/      # tenant isolation, auth, signing, path safety, log redaction
├── eval/          # RAG + mapping quality against real models (marked llm)
└── fixtures/
    ├── sources/   # synthetic pdf (text + scanned), docx, pptx, xlsx, csv, txt, md
    ├── templates/ # synthetic pptx, docx, xlsx templates
    └── eval/      # *.jsonl datasets + expected outputs
```

pytest markers (declared in `pyproject.toml`): `integration`, `llm` (needs Ollama + models), `eval`, `slow`.

| Command | When |
|---|---|
| `uv run pytest tests/unit` | every change |
| `uv run pytest -m "integration and not llm"` | before each commit that touches db/vector/jobs/api |
| `uv run pytest tests/security` | every phase exit |
| `uv run pytest -m llm tests/eval` | phases 4, 6, 10 exit; before release |
| `uv run pytest --cov=doc_intel --cov-report=term-missing -m "not llm"` | phase exits; target ≥ 80% |

**Test doubles.**
- `FakeLLM` implements the `LLMClient` protocol and returns scripted JSON, so mapping and routing logic is deterministic in unit and integration tests.
- `FakeEmbedder` returns seeded vectors for integration tests that don't need semantic quality.
- Real Postgres and Qdrant are **not** mocked in integration tests: isolation must be tested against the real filter semantics.

## 2. Unit tests (what must be covered)

| Area | Cases |
|---|---|
| `core/security` | bearer constant-time compare; `user_id` regex; HMAC sign/verify, tamper, expiry |
| `storage/local` | write-once refusal without `overwrite=True`; key built only from ids; path traversal attempts rejected |
| `ingestion/chunker` | per-format metadata; tables separate; ≤ `CHUNK_MAX_TOKENS`; contiguous `chunk_index`; no cross-section chunks |
| `templates/anchors_*` | golden AnchorMap per fixture template; `anchor_id` grammar; formula cells excluded |
| `templates/ir_builder.validate` | unknown/duplicate anchor; invalid `write_mode` for kind; `replace_token` without matching token |
| `mapping/verifier` | exact; whitespace/curly-quote/dash normalization; quote of a Markdown table row without the `|` separators; chunk not in hits; < 8 chars; typed value absent from quote; number/date canonicalization |
| `mapping/engine` status rules | 0 → missing (each `missing_reason`); 1 → populated; incomplete → partial; ≥ 2 distinct → conflict; equal values merged; `llm_error` doesn't fail run; evidence metadata copied from hits, not LLM |
| `mapping/schemas` | `FieldMappingResult` invariants reject: populated(rag) without evidence, missing with value, conflict with < 2 candidates |
| `chat/edits` | each `EditOp`; invalid op → nothing applied; parent unchanged |
| `writers/markers` | `{label}` substitution; configured markers honored |

## 3. Integration tests

- Job lifecycle:
  - queued → running → succeeded/failed;
  - SKIP LOCKED with two claimers;
  - every `running` job is re-queued at startup, and fails on the 3rd attempt;
  - idempotent retry: kill ingest after `parsed/` is written → retry succeeds; kill render after the `artifacts` row exists → retry reuses it.
- Re-requesting the same template reuses its `templates` row and latest IR.
- Two edits on the same parent run get distinct `version`s.
- Upload → `ingest_document` → `indexed` with correct `chunk_count`; DELETE removes points and files.
- Dedup on `sha256`: re-attaching the same bytes under a new Open WebUI file id appends to `openwebui_file_ids`, and `/v1/pipe/chat` resolves both ids.
- Template → `extract_template` (FakeLLM IR) → `run_mapping` → artifact row + file on disk.
- `/v1/pipe/chat` SSE: event order `status*` → `delta+` → `done`; error path emits `error` then `done`.

## 4. Security tests (`tests/security/`), hard gate at every phase exit

| Test | Assertion |
|---|---|
| `test_auth.py` | every non-public route (discovered from the app's OpenAPI schema, not a hand list) returns 401 without/with wrong bearer; 422 on bad `X-OWUI-User-Id` |
| `test_error_leaks.py` | unhandled error → 500 envelope, not re-raised, sentinel absent from output; DB errors hide bound parameters |
| `test_tenant_isolation_api.py` | parametrized over **every** route with a path id: user B using user A's ids → 404, response identical to a random id |
| `test_tenant_isolation_db.py` | every `db/repo.py` function returns nothing for a mismatched `user_id` or `project_id`; composite FK rejects a child row pointing at another user's project |
| `test_qdrant_store.py` (integration) | collection config + payload indexes, idempotent upsert, dense/sparse/fused search, filters, scoped delete |
| `test_qdrant_isolation.py` | search / retrieve-by-id / delete scoped to B never see or remove A's points (same `project_id` value under a different `user_id` included) |
| `test_qdrant_access.py` | no module outside `vector/qdrant_store.py` imports `qdrant_client` |
| `test_signed_urls.py` | valid → 200; altered `sig`/`exp`/`artifact_id` → 403; expired → 410 |
| `tests/unit/test_upload.py` | extension/magic mismatch → 415; oversize → 413; storage keys built from ids only, so malicious filenames never reach disk paths |
| `test_log_redaction.py` | run ingest + Q&A + mapping with sentinel strings in documents and prompts; assert sentinels never appear in captured logs |

## 5. RAG & mapping evaluation (`tests/eval/`, marker `llm`)

Datasets (synthetic, versioned in `tests/fixtures/eval/`):
- `qa.jsonl`: `{question, answerable, expected_answer_contains, expected_sources: [{filename, page_number}]}`.
- `mapping.jsonl`: `{template, sources, expected: {field_id: {status, value?}}}`. It includes deliberately absent fields and one seeded conflict per template.

| Metric | Definition | Gate |
|---|---|---|
| Fabricated citations | returned evidence/citations whose quote is not in the cited chunk (re-checked independently of the verifier) | **= 0 (hard)** |
| Citation verification rate | returned citations that pass re-verification | **= 100% (hard)** |
| Unanswerable handling | unanswerable questions answered "not found" | ≥ 95% |
| Absent-field handling | fields absent from sources returned `missing` | ≥ 95% |
| Seeded conflicts detected | `conflict` on seeded conflict fields | 100% |
| Field accuracy | `populated` values matching expected (normalized) | ≥ 80% (soft, tracked) |
| Source recall@8 | expected source chunk in retrieval top 8 | ≥ 90% (soft, tracked) |

The eval run writes `tests/eval/report.json` (git-ignored) and prints a summary table. Phase 10 records the numbers in the PR.

## 6. Template fidelity tests (`tests/integration/test_writers_*.py`)

For each fixture template × format:

| Check | PPTX | DOCX | XLSX |
|---|---|---|---|
| Untouched package parts unchanged | C14N-equal | C14N-equal | zip entries byte-equal |
| Written anchor keeps style | first-run `a:rPr` + `a:pPr` equal | `w:rPr` + `w:pPr` (incl. `w:numPr`) equal | cell `s` attribute equal |
| Markers | `MISSING_MARKER`/`CONFLICT_MARKER` text present at the anchor for missing/conflict fields |  |  |
| Structure | slide/shape count equal; layouts/masters untouched | headers/footers, sections, tables count equal | sheets, charts, images, defined names untouched; formula cells unchanged; `fullCalcOnLoad="1"` set |
| Typed values | — | — | numbers/dates written as `<v>`, strings as `inlineStr` |
| Opens cleanly | `python-pptx` loads | `python-docx` loads | `openpyxl` loads |

Manual check (Phase 7 and 10 PRs): open each output in PowerPoint/Word/Excel or LibreOffice and attach screenshots.
