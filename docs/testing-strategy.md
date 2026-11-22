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

pytest markers (declared in `pyproject.toml`): `integration`, `llm` (needs Ollama + models), `eval`, `slow`, `perf` (NFR-3 timings and RAM, minutes long; excluded by default via `addopts = "-m 'not perf'"`, run with `uv run pytest -m perf -s tests/eval/test_perf.py`).

| Command | When |
|---|---|
| `uv run pytest tests/unit` | every change |
| `uv run pytest -m "integration and not llm"` | before each commit that touches db/vector/jobs/api |
| `uv run pytest tests/security` | every phase exit |
| `uv run pytest -m llm tests/eval` | phases 4, 6, 10 exit; before release |
| `uv run pytest --cov=doc_intel --cov-report=term-missing -m "not llm"` | phase exits; target ≥ 80% |

`TEST_RUN_ID=<id>` gives a parallel pytest run its own test database (`doc_intel_test_<id>`) and Qdrant collection (`doc_chunks_test_<id>`).

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
| `templates/anchors_*` | golden AnchorMap per fixture template; `anchor_id` grammar; formula cells excluded; vertical merges counted once; token false positives (`[1]`, `XXXL`, `___`) not anchored; `MAX_ANCHORS` boundary (400 passes, 401 and 0 anchors fail) |
| `templates/ir_builder.validate` | unknown/duplicate anchor; invalid `write_mode` for kind; `replace_token` without matching token; `sheet_cell` allows only `fill_cell`; `fill_table_rows` only at row 1, col 0; no second field on a `fill_table_rows` table; `chart` type only with `fill_chart`/`insert_chart` and back |
| `mapping/verifier` | exact; whitespace/curly-quote/dash normalization; quote of a Markdown table row without the `|` separators; chunk not in hits; < 8 chars; typed value absent from quote; number/date canonicalization; sign and thousand/million/billion scale; comma digit grouping; European numbers and the ambiguous single-group US reading; English and Spanish month-name dates |
| `mapping/engine` status rules | 0 → missing (each `missing_reason`); 1 → populated; incomplete → partial; ≥ 2 distinct → conflict; equal values merged; `llm_error` doesn't fail run; evidence metadata copied from hits, not LLM |
| `mapping/schemas` | `FieldMappingResult` invariants reject: populated(rag) without evidence, missing with value, conflict with < 2 candidates |
| `chat/edits` | each `EditOp`; invalid op → nothing applied; parent unchanged |
| `writers/markers` | `{label}` substitution; configured markers honored |
| `integrations/openwebui/doc_intel_pipe.py` (`tests/unit/test_openwebui_pipe.py`) | task-call skip (metadata and `### Task:` prefix); SSE → `__event_emitter__` mapping, split chunks, keep-alive comments; each file uploaded once; error paths (401, 503, timeout, connect error, malformed frame, closed stream); the shared secret never appears in output or statuses |
| `chat/router` (`tests/unit/test_chat_router.py`) | one LLM call; history cut to last 10 messages and 1,500 chars; id validation and de-duplication; format from storage key; confirmation guard (role, sole template, filename, list number) |
| `chat/summary` (`tests/unit/test_chat_summary.py`) | counts, missing/conflict/partial/warning sections, download link or "No file was produced"; citation ordering and truncation; markdown escaping of filenames, quotes, values, labels; image and link stripping from the LLM answer |

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
- `tests/integration/test_generation_e2e.py`: round trip per format (FakeLLM IR + extraction → render → signed download → reopen).
- `tests/integration/test_pipe_api.py`: `/v1/pipe/chat` SSE event sequence (`status*` → `delta*` → `error`? → `done`); `X-OWUI-Chat-Id` validation (422); files dedup on `sha256` with the file id appended; the same chat id for another user is another project; unknown and failed files warn and continue; the 51-message limit.
- `tests/integration/test_pipe_e2e.py`: the real Pipe against the real app over ASGI: upload, template fill, signed download (opens and holds the filled slides), and a cited answer. The LLM is scripted, the embedder is a seeded fake, and Open WebUI's file read is stubbed.

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
| `test_signed_urls.py` | valid → 200; altered `sig`/`exp`, a `sig` for another existing artifact, or for another user → 403; expired → 410; unknown id or missing file → 404 |
| `tests/unit/test_upload.py` | extension/magic mismatch → 415; oversize → 413; storage keys built from ids only, so malicious filenames never reach disk paths |
| `test_log_redaction.py` | run ingest + Q&A + mapping with sentinel strings in documents and prompts; assert sentinels never appear in captured logs |
| `test_template_files.py` | XXE and entity expansion in docx/pptx/xlsx never leak file content or expand; understated zip entry sizes fail; more than 5000 parts, any part over 32 MB (any name) or over 128 MB in total are rejected before parsing |

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

The eval run writes `tests/eval/report.json` (git-ignored), one top-level key per eval (`qa`, `mapping`, and `perf` for `test_perf.py`), and prints a summary table. Each row also records `latency_s`; mapping rows record `confidence`, and the mapping summary has a `calibration` table (count and fraction correct per confidence tier).

Phase 12 results (2026-11-21, `qwen3:14b`; 15 questions, 5 templates with 39 fields):

| Metric | Gate | Result |
|---|---|---|
| Fabricated citations (Q&A, mapping) | = 0 | 0, 0 |
| Citation verification rate (Q&A, mapping) | = 100% | 100%, 100% |
| Unanswerable handling | ≥ 95% | 100% |
| Absent-field handling | ≥ 95% | 100% |
| Seeded conflicts detected | 100% | 100% (5 of 5) |
| Field accuracy | ≥ 80% soft | 95.5% (21 of 22); the miss is a title that appears only as the source's first heading (`not_found_by_llm`, so `missing`, never a wrong value) |
| Source recall@8 | ≥ 90% soft | 100% |

Phase 14 results (2026-11-22, `qwen3:14b`, `LLM_NUM_CTX=32768`; 55 fields: two card templates and two long parallel-section reports added; before = Phase 13 code): fabricated 0 / 0, verification 100% / 100%, seeded conflicts 100% / 100%, absent fields 75% / 100%, field accuracy 61.8% / 100%. Q&A unchanged.

Phase 15 results (2026-11-22; 75 fields: slot grid, cell grid and two row tables added; the eval now maps each case with `map_fields`; before = Phase 14 code): fabricated 0 / 0, verification 100% / 100%, seeded conflicts 100% / 100%, absent fields 100% / 100%, field accuracy 72.2% / 94.4%. A table value is correct when it has the expected row count and each expected row matches its own actual row (each expected cell contained in the actual cell).

Phase 16 results (2026-11-22; 85 fields: summary/findings fields in four sources, one Spanish, added; the mapping summary now also reports `synthesized` and `length_violations`): fabricated 0 / 0, verification 100% / 100%, seeded conflicts 100% / 100%, absent fields 100% / 100%, field accuracy 93.3% / 95.0%, length violations 3 after (not measured before), synthesized 0 in the recorded run.

## 6. Template fidelity tests (`tests/integration/test_writers_*.py`)

For each fixture template × format:

| Check | PPTX | DOCX | XLSX |
|---|---|---|---|
| Untouched package parts unchanged | C14N-equal | C14N-equal | zip entries byte-equal |
| Written anchor keeps style | first-run `a:rPr` + `a:pPr` equal | `w:rPr` + `w:pPr` (incl. `w:numPr`) equal | cell `s` attribute equal |
| Markers | `MISSING_MARKER`/`CONFLICT_MARKER` text present at the anchor for missing/conflict fields |  |  |
| Structure | slide/shape count equal (an `insert_chart` swaps one placeholder for one chart); layouts/masters untouched; only filled charts' chart parts and embedded workbooks change | headers/footers, sections, tables count equal | sheets, charts, images, defined names untouched; formula cells unchanged; `fullCalcOnLoad="1"` set |
| Typed values | — | — | numbers written as `<v>`; dates as `<v>` only in date-formatted cells; strings and other dates as `inlineStr` |
| Opens cleanly | `python-pptx` loads | `python-docx` loads | `openpyxl` loads |

Tests live in `tests/integration/test_writers_{pptx,docx,xlsx}.py`:
- C14N probe: `python-pptx`/`python-docx` save without changes keeps every part C14N-equal (guards the first row).
- Round trip: write a unique value at every golden anchor, re-extract, values match.
- Overlapping paragraph + inline content control: a paragraph `replace_text` that overlaps an inline content control target writes both fields.
- Merged-cell `fill_table_rows`: merged docx cells and spanned pptx cells are written once.
- XLSX: a date is typed only in date-formatted cells.

Manual check (Phase 7 and 10 PRs): open each output in PowerPoint/Word/Excel or LibreOffice and attach screenshots.
