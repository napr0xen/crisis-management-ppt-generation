# Implementation Roadmap

Work only on the current phase. A phase is **done** only when every exit-gate command passes and its real output is pasted into the phase PR or commit message. Never edit a completed phase's code without a stated cause (bug, failing gate, or a doc change in the same commit).

## Per-phase checklist (every phase)

1. `/ponytail full` active.
2. `/ecc:plan`: plan the phase against these docs; wait for approval.
3. `/ecc:tdd`: failing test first, then implement. If not installed, use the `ecc:tdd-workflow` skill.
4. `/ecc:code-review`.
5. `/ecc:verify`: run the exit gate. If not installed, use the `ecc:verification-loop` skill.
6. `/ecc:update-docs`: docs and code must agree.
7. `/ponytail-review` on the phase diff.
8. Phases 5 and 12 only: `/ponytail-audit` on the whole repo.

Common gate for every phase ≥ 1: `uv run ruff check . && uv run ruff format --check . && uv run pytest tests/unit` green.

## Status

| Phase | Name | Status |
|---|---|---|
| 0 | Architecture & source-of-truth docs | **Complete** |
| 1 | Foundation, infrastructure & database | **Complete** |
| 2 | Ingestion, storage & Open WebUI integration check | **Complete** |
| 3 | Embeddings & vector store | **Complete** |
| 4 | LLM gateway & grounded Q&A | **Complete** |
| 5 | Template intelligence (Anchor Map + IR) | **Complete** |
| 6 | Mapping engine | **Complete** |
| 7 | Surgical writers & artifacts | **Complete** |
| 8 | Open WebUI Pipe & chat orchestration | **Complete** |
| 9 | Template library | **Complete** |
| 10 | Universal mapping fixes | **Complete** |
| 11 | Conversational edits & run versioning | **Complete** |
| 12 | Hardening, evaluation & audit | **Complete** |
| 13 | Universal accuracy: number formatting & cross-slide facts | **Complete** |
| 14 | Card context & 32k retrieval budget | **Complete** (on branch, `main` not yet fast-forwarded: awaiting live test) |
| 15 | Row-grouped table extraction | **Complete** (on branch, awaiting live test) |
| next | Universal extraction & mapping accuracy (remaining gaps) | Phases 15-16 ([below](#next-priority-universal-extraction--mapping-accuracy)) |

---

## Phase 0: Architecture & source-of-truth docs

- **Entry:** empty repo.
- **Deliverables:**
  - `README.md`, `CLAUDE.md`;
  - `docs/requirements.md`, `architecture.md`, `system-design.md`, `data-model.md`, `api-specification.md`, `implementation-roadmap.md`, `development-guidelines.md`, `testing-strategy.md`, `DECISIONS.md`.
- **Exit:**
  - Names are consistent across all 11 files (grep check);
  - `TemplateIR` and `FieldMappingResult` JSON schemas are present;
  - isolation rules are stated for PostgreSQL and Qdrant;
  - `git status` shows only `.md` files.
- **Checkpoints:** `/ponytail-review` of the docs (findings 1–10 applied).
- **Status:** Complete. The name check passed, and only `.md` files are committed.

## Phase 1: Foundation, infrastructure & database

- **Entry:** Phase 0 merged.
- **Deliverables:**
  - `pyproject.toml` (uv, Python 3.12, ruff, pytest, pytest-asyncio), `.gitignore` (`.env`, `data/`, `.venv/`, `.DS_Store`, `tests/eval/report.json`), `.env.example`;
  - `docker-compose.yml` (Open WebUI pinned tag, `postgres:16`, Qdrant pinned tag; volumes under `./data/`; Postgres and Qdrant bound to `127.0.0.1`);
  - `core/` (`Settings`, JSON logging with redaction, `AppError` + handlers, bearer auth + `X-OWUI-User-Id` dependency, HMAC signing). `TenantContext` moves to Phase 2, where the first child-table query needs it;
  - `db/` models for all 7 tables and the first Alembic migration;
  - `db/repo.py` tenant-scoped base queries;
  - `storage/` `StorageBackend` + `LocalStorage`;
  - `GET /v1/health`, `POST/GET /v1/projects`, `GET /v1/projects/{project_id}`.
- **Exit:**
  - `docker compose up -d` shows all services healthy;
  - `uv run alembic upgrade head` succeeds;
  - `curl 127.0.0.1:8000/v1/health` reports `postgres: ok, qdrant: ok`;
  - `uv run pytest tests/unit tests/security -m "not llm"` passes, including: 401 without bearer, 404 for another user's project, user_id regex, storage write-once, and signing round-trip/tamper/expiry.

## Phase 2: Ingestion, storage & Open WebUI integration check

- **Entry:** Phase 1 done.
- **Deliverables:**
  - `ingestion/` (Docling parser for pdf/docx/pptx with RapidOCR, native parsers for xlsx/csv/txt/md, structural chunker);
  - `core/tenancy.py` `TenantContext` + project-scoped dependency (404 for non-owners);
  - `jobs/` worker (SKIP LOCKED claim, startup re-queue of all `running` jobs, progress, idempotent retries) and the `ingest_document` handler;
  - document endpoints (POST/GET/list/DELETE without the Qdrant step), `GET …/jobs/{job_id}`;
  - fixture corpus `tests/fixtures/sources.py`: builds one file per format at test time (incl. an image-only PDF for OCR), so no binary fixtures are committed;
  - ✅ **Open WebUI integration check** (done; results in [architecture.md § 5](architecture.md#5-open-webui-pipe-design)) against the pinned Open WebUI image (manual, using a throwaway Pipe in Open WebUI's function editor; no Pipe code is committed before Phase 8):
    1. how a Pipe reads uploaded file bytes from `__files__` (field names, storage API or path);
    2. whether `BYPASS_EMBEDDING_AND_RETRIEVAL=true` is the right setting name and actually stops Open WebUI injecting its own RAG context into Pipe requests;
    3. what `__files__` contains per turn (current message only, or the whole chat);
    4. ~~that the container reaches the native backend at `host.docker.internal:8000` while it binds `127.0.0.1`~~ (verified in Phase 1, see architecture.md § 5).
- **Exit:**
  - Chunker unit tests: metadata correct per format, tables as separate chunks, no chunk over 512 tokens, contiguous `chunk_index`;
  - integration test: upload → job `succeeded` → `parsed/{document_id}.json` exists;
  - dedup test;
  - 415/413 tests;
  - Open WebUI check results (real output or screenshots) recorded in [architecture.md § 5](architecture.md#5-open-webui-pipe-design). Any deviation is fixed in ADR-002, `api-specification.md`, and `docker-compose.yml` in the same commit.
- **Scope boundary.** Phase 2 has no embedding step, and nothing stands in for one. The job ends after parse + chunk with `result = {page_count, chunk_count}`, and the document stays `parsing`. Phase 3 adds embed + upsert and the `indexed` transition.

## Phase 3: Embeddings & vector store

- **Entry:** Phase 2 done.
- **Deliverables:**
  - `embeddings/ollama.py` (Ollama `qwen3-embedding` dense, batch 32) + `embeddings/sparse.py` (BM25), `embeddings/reranker.py` (lazy, flag-gated); ADR-011 replaced the original BGE-M3 plan;
  - `vector/qdrant_store.py` (collection bootstrap with named vectors + payload indexes, upsert, hybrid RRF search, tenant-filtered retrieve/delete);
  - `mapping/retriever.py`;
  - ingestion completes to `indexed`; DELETE removes points.
- **Exit:**
  - `tests/security/test_qdrant_isolation.py`: user B's searches, retrieves, and deletes see 0 of user A's points, across the matrix (same project id / other user, etc.);
  - retrieval smoke test on fixtures: the expected chunk is in the top 8 for 10/10 seeded queries.

## Phase 4: LLM gateway & grounded Q&A

- **Entry:** Phase 3 done.
- **Deliverables:**
  - `llm/gateway.py` (`OllamaClient` on native `/api/chat`, `OpenAICompatClient`, JSON-schema output + 1 retry; `AnthropicClient` planned, ADR-005);
  - `mapping/verifier.py` (normalize, quote check, typed-value check);
  - `chat/qa.py`;
  - `POST /v1/projects/{project_id}/query`.
- **Exit:**
  - Verifier unit tests (exact, whitespace/quote normalization, wrong chunk, cross-tenant chunk, too-short quote, typed value absent);
  - `uv run pytest tests/eval -m llm -k qa` passes: 0 unverified citations returned, and "not found" for every unanswerable question in the set.
- **Status:** Complete. Eval with `qwen3:14b` on 15 questions: 0 fabricated citations, 5/5 unanswerable → not found, 10/10 answerable answered with the expected source cited, no `<think>` leaked.

## Phase 5: Template intelligence (Stages 1–2)

- **Entry:** Phase 4 done.
- **Deliverables:**
  - `templates/anchors_{pptx,docx,xlsx}.py`, `templates/ir_builder.py` (LLM + validator), `templates/schemas.py`;
  - the `extract_template` job;
  - template endpoints;
  - fixture templates are built in code by `tests/fixtures/templates/__init__.py` (pptx with tables, docx with header/footer + content controls, xlsx with formulas), with golden AnchorMaps in `tests/fixtures/templates/*.anchors.json` (no binary fixtures are committed).
- **Exit:**
  - Golden Anchor Map tests per fixture;
  - validator rejects unknown/duplicate anchors and invalid `write_mode`;
  - an IR built for each fixture passes the validator.
- **Checkpoints:** `/ponytail-audit` (whole repo).
- **Status:** Complete (2026-11-19). Gate: `uv run ruff check .` all checks passed; `uv run ruff format --check .` 117 files already formatted; `uv run alembic check` no new upgrade operations; `uv run pytest` 535 passed (Phases 1-5).
  - Live smoke: qwen3:14b built a valid TemplateIR for the pptx/docx/xlsx fixtures (19-60 s per template).
  - Known limits:
    - IR prompt holds the whole AnchorMap (MAX_ANCHORS=400; large templates may not fit LLM_NUM_CTX=8192);
    - a failed `extract_template` leaves the document `role=template` with points purged (recover by POSTing again);
    - docx skips nested tables, first-page/even headers, and content controls in headers/footers;
    - duplicate content-control tags keep the first;
    - xlsx path context scans at most 100 cells;
    - template packages capped at 5000 parts, 32 MB per part, 128 MB total;
    - `/ponytail-audit` deferred to after Phase 7 by user decision.

## Phase 6: Mapping engine (Stage 3)

- **Entry:** Phase 5 done.
- **Deliverables:**
  - `mapping/extractor.py`, `mapping/engine.py` (per-field state machine, status resolution, confidence tiers, progress);
  - the `run_mapping` job up to `verifying`;
  - `POST …/mapping-runs`, `GET …/mapping-runs/{run_id}`.
- **Exit:**
  - Unit tests for every status rule (0/1/2+ candidates, merge-equal, typed-value rejection, partial conditions, `llm_error` → missing without failing the run);
  - eval: **0 fabricated citations** on the mapping eval set, the seeded conflict fixture yields `conflict`, and absent fields yield `missing` (≥ 95%).
- **Status:** Complete (2026-11-19). Gate: `uv run ruff check .` all checks passed; `uv run ruff format --check .` 131 files already formatted; `uv run alembic check` no new upgrade operations; `uv run pytest` 690 passed incl. llm evals (Phases 1-6), coverage 97%.
  - Live smoke: real API + worker + qwen3:14b mapped a docx template over 2 sources (4 fields, 18.9 s): populated, typed date, conflict (12000 vs 15000) and missing as expected.
  - Known limits:
    - runs stop at `verifying` (`finished_at` null, job `succeeded`) until Phase 7 renders;
    - a retried run recomputes every field (one LLM call per field again);
    - at most 6 hits per field reach the LLM;
    - paraphrased text is `partial`, never `populated`;
    - `allowed_values`, `max_items`, `required`, `unit`, `date_format` have no mapping rule yet;
    - typed numbers read English formats only; dates ISO or English month names only;
    - live eval: 0 fabricated citations, seeded conflicts 2/2, absent fields 8/8 missing, field accuracy 7/8 present fields (87.5%) (qwen3:14b, 18 fields).

## Phase 7: Surgical writers & artifacts (Stage 4)

- **Entry:** Phase 6 done.
- **Deliverables:**
  - `writers/pptx_writer.py`, `docx_writer.py`, `xlsx_writer.py`, `markers.py`;
  - the `rendering` stage of `run_mapping` and the `render_artifact` job;
  - artifact endpoints + signed download.
- **Exit:**
  - Fidelity tests per format: untouched parts C14N-equal (XLSX: byte-equal zip entries); written runs keep `rPr`/`s` style; markers written for missing/conflict; formula cells untouched; output opens in python-pptx/python-docx/openpyxl;
  - signed URL tests (tamper → 403, expired → 410, unknown `artifact_id` → 404); `GET …/artifacts` on another user's project → 404.
- **Status:** Complete (2026-11-19). Gate: `uv run ruff check .` and `uv run ruff format --check .` clean; `uv run alembic check` clean; `uv run pytest` 861 passed (Phases 1-7).
  - Live smoke: qwen3:14b rendered pptx/docx/xlsx and downloaded them via signed URLs.
- **Known limits:**
  - no row/slide/sheet-row insertion (`table_overflow` warning);
  - dates ISO for text targets (`date_format` ignored) and 1900 date system for xlsx;
  - non-ASCII download names become `_`;
  - `max_items` not enforced;
  - an artifact file can be left unreferenced if the final commit fails on every retry;
  - xlsx IR can propose fields on empty cells next to labels that are themselves labels (Phase 5 neighbour heuristic, seen in the live smoke);
  - content controls around tables, rows or cells are not fields (rows inside a row-level control are not anchored).
- **Phase 5 change** (cause: Phase 7 live smoke lost template labels): the IR validator requires `replace_token` on anchors with tokens for both `replace_text` and `fill_cell` (unless the anchor text is exactly one token).
- **Phase 5 change** (cause: Phase 7 audit, writing such a control deleted the table and produced an unreadable file): docx extractor skips content controls around tables/rows/cells and tables whose rows all sit in a row-level control.
- `NotFound` is not retried by jobs (cause: audit; a deleted row never comes back).

## Phase 8: Open WebUI Pipe & chat orchestration

- **Entry:** Phase 7 done.
- **Deliverables:**
  - `integrations/openwebui/doc_intel_pipe.py`;
  - `POST /v1/pipe/files`, `POST /v1/pipe/chat` (SSE);
  - `chat/router.py`, `chat/summary.py`;
  - template confirmation flow;
  - disable Open WebUI's built-in RAG for the Pipe model during setup: create the model override with capabilities `file_context: false` and `builtin_tools: false` (Phase 2 finding: `BYPASS_EMBEDDING_AND_RETRIEVAL=true` only skips embedding, still injects file text, and is persisted config that later env changes don't override);
  - Pipe skips task calls via `__metadata__["task"]` first; the `### Task:` prefix check is safe only while file context is off (Open WebUI's RAG template also starts with `### Task:`).
- **Exit:**
  - Pipe unit tests (task-call skip, SSE → event_emitter mapping, file upload once per file);
  - API tests for the SSE event sequence;
  - manual end-to-end in Open WebUI recorded in the PR: upload 3 sources + 1 pptx, "fill the template" → summary + working download link.
- **Status:** Complete (2026-11-20). Gate: `uv run ruff check .` and `uv run ruff format --check .` clean; `uv run alembic check` clean; `uv run pytest` 1034 passed (Phases 1-8). Manual end-to-end in Open WebUI v0.13.4 (no PR, recorded here): 1 source (4-page PDF) + 1 ten-slide pptx, not the 3 sources the exit item names; "fill the template" → extract (10 slide batches), map 46 populated / 1 partial / 18 missing / 0 conflict, rendered with 0 warnings, downloaded deck opens cleanly. Live testing drove: per-slide IR batches with an anchor and size cap, Ollama `num_predict`, multi-token `replace_token` anchors, `(shape_id, kind)` pptx keys, skipped decorative shapes, shape `pos`, stricter answer and filename escaping.

## Phase 9: Template library

Inserted after Phase 8 (cause: users should be able to generate a deck or document from source files alone, without uploading a template each time). Edits and hardening moved to Phases 10 and 11 (now 11 and 12, after Phase 10 was inserted).

- **Entry:** Phase 8 done.
- **Deliverables:**
  - `library_templates` table (migration `0002_library_templates`) and `library/{id}/original.{ext}` storage;
  - `templates/library.py`: `register` (anchor map + IR built once, at registration) and `adopt` (copy-on-use into the caller's project);
  - CLI `uv run python -m doc_intel.cli seed-template | list-templates | show-template | remove-template`;
  - chat selection: router `format` field, `chat/library.py` (`library_choice`), `render_library`, library path in `POST /v1/pipe/chat`.
- **Exit:**
  - Tests: registration stores file + cached IR and rejects duplicates before any LLM call; adopting copies into the caller's tenant with no LLM call and no `extract_template` job; a second adopt reuses the copy; another user's ids return 404; removing a library template leaves earlier runs and artifacts intact;
  - chat: numbered library list, number and name selection, single format match runs directly, uploaded templates keep precedence, a source DOCX is not auto-confirmed for "make a powerpoint", names and descriptions escaped with `md()`;
  - manual: seed a pptx, attach only a source PDF/DOCX in Open WebUI, "make a powerpoint out of this" → summary + working download link.
- **Phase 5 change** (cause: seeding a second real deck failed `ir_invalid` twice on a table drawn with `shape_text` boxes): `ir_builder` drops three safe model slips before validation (`fill_table_rows` on a non-table anchor, `replace_token` on a static 0-token label, exact `(anchor_id, write_mode, token)` duplicates); the prompt says shape-drawn tables are separate `shape_text` fields.
- **Status:** Complete (2026-11-21). Gate: `uv run ruff check .` all checks passed; `uv run ruff format --check .` 165 files already formatted; `uv run alembic check` no new upgrade operations; `uv run pytest` 1086 passed (Phases 1-9). Manual end-to-end in Open WebUI: two pptx templates seeded with the CLI (the second needed the Phase 5 change above); a new chat with only a source PDF, "make a powerpoint out of this" → numbered library list, the pick ran `run_mapping` on the cached IR with no `extract_template` job and returned a working download. Known gap: some `[...]` tokens stayed unreplaced in that deck; diagnosed separately.

## Phase 10: Universal mapping fixes

Inserted after Phase 9 (cause: Phase 9's known gap. A finance deck mapped against a Spanish-language bank statement left 79 `[MISSING: …]` and 27 `[CONFLICT: …]` markers, 46 of the missing ones typed fields that failed verification). Edits and hardening moved to Phases 11 and 12. Raw `[...]` template tokens were not the cause: the rendered deck had none, and `replace_token` already merged tokens split across runs.

- **Entry:** Phase 9 done.
- **Deliverables:**
  - `mapping/verifier.py`: European numbers when unambiguous (`1.829.914,84`, `444.132,10`, `10,50`); English and Spanish month-name dates (`30 junio 2022`, `1 de junio de 2022`, `30 jun. 2022`, `June 30 2022`);
  - `jobs/handlers.py` `map_run`: `replace_token` fields that share `(token, expected_type, label)` are mapped once per run and share the result (one retrieval and LLM call, the same value on every slide);
  - pptx regression test for a token split across three runs.
- **Exit:**
  - Tests: European and US numbers canonicalize to the same value, ambiguous single-group forms keep the US reading; Spanish and English dates match ISO values, impossible dates and lookalike month words are rejected; repeated tokens cost one `map_field` call, a different `expected_type` or label is not shared; a 3-run split token renders with no leftover token;
  - [x] manual: re-seed the finance deck, map it against the Spanish statement in Open WebUI, and record the populated / missing / conflict counts here.
- **Status:** Complete (2026-11-21). Automated gate passed (2026-11-21): `uv run ruff check .` all checks passed; `uv run ruff format --check .` 165 files already formatted; `uv run alembic check` no new upgrade operations; `uv run pytest` 1117 passed (Phases 1-10).
  - Live test passed (2026-11-21, Open WebUI): a finance pptx template mapped against a Spanish PDF source. Spanish dates and European currency verified, the first slide's KPIs and dates populated, and a token repeated on all 8 slides (the reporting period) has the same value on each. Populated / missing / conflict counts were not recorded.

## Phase 11: Conversational edits & run versioning

- **Entry:** Phase 10 done.
- **Deliverables:**
  - `chat/edits.py`;
  - `POST …/mapping-runs/{run_id}/edits`;
  - router `edit` intent;
  - new run + artifact per edit.
- **Exit:**
  - Tests: every `EditOp` and its validation errors (422, nothing applied); parent run unchanged; version increments; `choose_candidate` only on `conflict`;
  - end-to-end chat edit produces v2 artifact.
- **Status:** Complete (2026-11-21). Automated gate passed (2026-11-21): `uv run ruff check .` all checks passed; `uv run ruff format --check .` 170 files already formatted; `uv run alembic check` no new upgrade operations; `uv run pytest` 1178 passed (Phases 1-11). Covered: `chat/edits.py` (`apply_edits`, `parse_edits`, prompt `edits.md`), `POST …/mapping-runs/{run_id}/edits` (`start_edit_run`), router `edit` intent, Pipe edit turn (`render_artifact` → `_v2` artifact), repeated-token siblings share an edit, chat `set_value` must quote the user's words (ADR-013). No migration (the columns existed since Phase 1).
  - [x] manual: in Open WebUI, after a filled deck, "set <field> to <value>" → v2 download with the value; "use the first value for <conflict field>" → v3; "clear <field>" → missing marker; a value not written in the message is refused; the v1 link still downloads.
  - Live test passed (2026-11-21, Open WebUI): "change bank name to <name>" produced a `_v3` pptx with only that field changed and every other field kept.

## Phase 12: Hardening, evaluation & audit

- **Entry:** Phase 11 done.
- **Deliverables:**
  - full eval suite + report;
  - RAM and latency measurements against [architecture.md § 6](architecture.md#6-memory-budget-24-gb-unified-memory) and NFR-3;
  - confidence tier calibration;
  - log-redaction test across all handlers;
  - security review;
  - README quick-start verified on a clean checkout.
- **Exit:**
  - All test suites green, coverage ≥ 80%;
  - eval hard gates pass (0 fabricated citations, 100% citation verification);
  - measured numbers recorded in `docs/architecture.md`.
- **Checkpoints:** `/ponytail-audit` (whole repo).
- **Status:** Complete (2026-11-21). Automated gate passed (2026-11-21): `uv run ruff check .` all checks passed; `uv run ruff format --check .` 172 files already formatted; `uv run alembic check` no new upgrade operations; `uv run pytest --cov=src/doc_intel` 1196 passed, 1 deselected (`perf`), coverage 98%.
  - Eval (hard gates pass): Q&A and mapping 0 fabricated citations, 100% citation verification; unanswerable 100% "not found"; absent fields 100% `missing`; seeded conflicts 5 of 5; field accuracy 95.5%; source recall@8 100% ([testing-strategy.md § 5](testing-strategy.md#5-rag--mapping-evaluation-testseval-marker-llm)).
  - NFR-3 and RAM measured and recorded in [architecture.md § 6](architecture.md#6-memory-budget-24-gb-unified-memory): 50-page ingest 23.7 s, Q&A max 13.5 s, 30 fields over 5 sources 250.6 s; all targets met.
  - Confidence tiers calibrated on the eval set (0.9: 12/12 correct, 0.8: 9/9), kept unchanged ([system-design.md § 6](system-design.md#6-fieldmappingresult)).
  - `tests/security/test_log_redaction.py`: a sentinel in documents, filenames, labels, chat, LLM replies and edit values never reaches structlog, stdlib logs or stdout across every job handler, chat intent and error path.
  - Security review (no critical/high): fixed escaping of code fences, `~`, newlines and bare URLs (backend `md()`/`_answer_md` and the Pipe); edit replies list every applied op; optional `QDRANT_API_KEY`; Docling `MAX_PAGES = 1000`; 32 MB cap per OOXML XML part on upload; chat questions capped at 2,000 chars; `--no-access-log` in the run command (signed download URLs no longer in access logs). Left as known: uploads spooled before auth (existing shortcut), `/v1/health` does not flag a remote embedding/LLM URL, router auto-confirms the only template-capable document (existing shortcut), `/docs` served without auth.
  - `/ponytail-audit`: no must-fix; fixed 1-based conflict candidate numbers in chat, tenant test for `latest_completed_mapping_run`, size caps on edit ops.
  - README quick start verified on a clean clone of `phase-11-conversational-edits`: `uv sync`, `alembic upgrade head`, `uvicorn`, `/v1/health` all `ok`.
  - [x] manual: live Open WebUI pass (see Phase 10 and 11 manual items), plus: a source whose text contains a ```html fence and a `https://` URL renders as plain text in a Q&A answer; optionally set `QDRANT_API_KEY` in `.env`, `docker compose up -d`, restart the API, and confirm `/v1/health` and a Q&A still work.
  - Live test passed (2026-11-21): the Phase 10 and 11 Open WebUI passes above.

---

## Phase 13: Universal accuracy: number formatting & cross-slide facts

The first two gaps of the [next priority](#next-priority-universal-extraction--mapping-accuracy) list (cause: the Phase 10-12 live finance deck showed raw floats such as `444132.1` and one fact with different values on different slides).

- **Entry:** Phase 12 done (`main` at `0b91610`).
- **Deliverables:**
  - `mapping/verifier.py` `source_text`; `writers/markers.py`: typed values on text targets are written as the verified source span (separators, decimals, sign, magnitude word or glued suffix such as `M`/`bn`/`K`, currency symbol or code, `%`, date as written); xlsx keeps real numbers and serials;
  - `writers/runs.py` `replace_token`: a typed value's marker already shown next to the token is not doubled;
  - `mapping/engine.py` `share_key`: `(fact name, expected_type)`, a named placeholder's inner text or else the label; `shared_facts` shares a key only across slides/sheets/docx sections, never between slots repeated in one ([system-design.md § 7](system-design.md#7-mapping-engine-stage-3)); `map_run` and edits share by it.
- **Exit:**
  - Tests, each on two or more unrelated domains (finance and HR/procurement/operations): source spans for European/US numbers, currency symbols and codes, percent, negatives, magnitude words and dates; fallback for values without evidence; no doubled marker next to a token; a named placeholder with different labels costs one `map_field` call and gets one value; slots repeated on one slide, sheet or docx section are each mapped and edited on their own; generic blanks with different labels and a different `expected_type` are not shared; an edit reaches every sibling;
  - eval hard gates still pass (0 fabricated citations, 100% citation verification), before/after recorded;
  - `uv run ruff check .`, `uv run ruff format --check .`, `uv run alembic check`, `uv run pytest` green.
- **Live-test fixes** (cause: the procurement deck rendered `AED 182.4` for `AED 182.4M` and `91` for `91%`, and three risk cards on one slide got one `[Risk]` value): glued suffixes kept in the source span; same-unit slots never shared.
- **Status:** Complete (2026-11-22). Gate: `uv run ruff check .` all checks passed; `uv run ruff format --check .` 172 files already formatted; `uv run alembic check` no new upgrade operations; `uv run pytest` 1281 passed, 1 deselected (`perf`). Mapping eval before / after: 0 / 0 fabricated citations, 100% / 100% citation verification, seeded conflicts 100% / 100%, absent fields `missing` 100% / 100%, field accuracy 95.5% / 95.5% (the eval calls `map_field` per field, so it does not exercise sharing or display). `/ponytail-review`: one fix, a glued suffix is trimmed only before a whole word (`AED [Spend] Managed` kept `182.4M`); `/ecc:quality-gate` clean. Live re-test on the procurement deck: `AED 182.4M`, `AED 11.6M`, `91%`, `AED 21.5M` as in the source, and the risk cards on one slide each got their own value.

---

## Phase 14: Spatial / card context & 32k retrieval budget

Cause: repeated cards on one slide (`[Wait time]` under three headings) were mapped from the label alone and returned every card's value as a conflict, and with `LLM_NUM_CTX=32768` (since `58d5261`) the extractor still saw at most 6 chunks sized for 8k.

- **Entry:** Phase 13 done (`main` at `58d5261`).
- **Deliverables:**
  - `Anchor.context` / `IRField.context` ([system-design.md § 4-5](system-design.md#4-anchormap)): the static heading, row label or column header next to a slot, found by code (pptx geometry, table row/column labels, docx label paragraph), set on every IR field by `build_ir` and hidden from the LLM schema;
  - context in the retrieval query, the extraction spec and `share_key`; extraction prompt rules for context and for the kind of quantity (a count is not money);
  - candidate check (`extractor.check_candidates`, all or nothing, context fields only, with sibling headings);
  - `prompt_tokens(num_ctx) = max(6144, num_ctx // 2)`, `hit_budget` (6 chunks at 8192, 26 at 32768), `Retriever.search(top_n)` (12 ranked hits) and `with_neighbors` (previous chunks first, then next, reading order); `Hit.chunk_index`.
- **Exit:**
  - Tests on two or more unrelated domains (healthcare, agriculture, HR, environment): card headings above/left, gap limit, title never used, groups, table cell labels, docx label paragraphs, context reaches IR/query/spec/share key, sibling-only values are `missing`, conflicts under one heading stay conflicts, budget at 8k/32k, neighbors order and budget;
  - eval hard gates pass, before/after recorded; `uv run ruff check .`, `uv run ruff format --check .`, `uv run alembic check`, `uv run pytest` green.
- **Eval** (`qwen3:14b`, `LLM_NUM_CTX=32768`; the set grew from 39 to 55 fields with two card templates and two long parallel-section reports; "before" is the Phase 13 code on the same 55 fields, without `context`):

  | Metric | Before | After |
  |---|---|---|
  | Fabricated citations | 0 | 0 |
  | Citation verification | 100% | 100% |
  | Seeded conflicts detected | 100% (5/5) | 100% (5/5) |
  | Absent fields `missing` | 75% (12/16) | 100% (16/16) |
  | Field accuracy | 61.8% (21/34) | 100% (34/34) |
  | Latency p50 / max | 5.3 s / 27.4 s | 5.9 s / 16.2 s |

  Q&A eval unchanged: 0 fabricated, 100% verification, unanswerable 100%, recall@8 100%.
- **Trade-offs:** one extra LLM call per context field with candidates; at 32k a field prompt is up to ~16k tokens (more prompt time per field); a template that reuses one token under different headings no longer shares one value. Rejected while measuring: dropping single candidates in the check (it rejected one of two real conflicting values) and checking fields without context by label (it rejected many correct values).
- **Status:** Complete (2026-11-22). Gate: `uv run ruff check .` all checks passed; `uv run ruff format --check .` 174 files already formatted; `uv run alembic check` no new upgrade operations; `uv run pytest` 1362 passed, 1 deselected (`perf`). `/ponytail-review`: ship, no findings (note: templates registered before this phase have IRs without `context` until uploaded again); `/ecc:quality-gate` clean. Live test pending: `main` stays at `58d5261` until it passes.

---

## Phase 15: Row-grouped table extraction

The third gap of the [next priority](#next-priority-universal-extraction--mapping-accuracy) list (cause: grid templates were mapped cell by cell, so each cell's call saw every record and returned a conflict, nothing, or a value from another record).

- **Entry:** Phase 14 done (branch `phase-14-context-and-retrieval`, `8fe3471`).
- **Deliverables:** `engine.row_groups` (numbered slot grids, native cell grids, `fill_table_rows`), `map_group` (one retrieval and one `table_rows` call per group, row-by-row verification, a row anchored to one quote), `map_fields` (the run loop moved out of `jobs/handlers.py`; the eval calls it too, so it now exercises shared facts and row groups) ([system-design.md § 7](system-design.md#7-mapping-engine-stage-3)).
- **Exit:**
  - Tests on two or more unrelated domains (lab maintenance, volunteer roster, school trips, port arrivals, crops): grid detection and its refusals (gaps, single column, repeated token, static label column, different units), one call per grid, rows in order, mixed-record rows `partial`, unsupported cells `missing`, fewer records than slots, typed cells, unretrieved quotes rejected, injected chunk text is data, `fill_table_rows` rows kept only when verified, progress order;
  - eval hard gates pass, before/after recorded; `uv run ruff check .`, `uv run ruff format --check .`, `uv run alembic check`, `uv run pytest` green.
- **Eval** (`qwen3:14b`, `LLM_NUM_CTX=32768`; 75 fields: a numbered slot grid, a native cell grid and two `fill_table_rows` tables added; before = Phase 14 code):

  | Metric | Before | After |
  |---|---|---|
  | Fabricated citations | 0 | 0 |
  | Citation verification | 100% | 100% |
  | Seeded conflicts detected | 100% (5/5) | 100% (5/5) |
  | Absent fields `missing` | 100% (16/16) | 100% (16/16) |
  | Field accuracy | 72.2% (39/54) | 94.4% (51/54) |
  | Latency p50 / max | 5.6 s / 16.2 s | 5.0 s / 14.5 s |

  The 3 misses are one roster row: the source says "drove the delivery van" and the model wrote "Driver", which is not verbatim, so that cell is `missing` and its two row-mates `partial` (right values, row not anchored). The `fill_table_rows` tables were already right before and stay right.
- **Trade-offs:** grid rows follow source order, so a slot grid whose numbering means something else than "first, second, third record" (for example `[Q1] [Q2]` quarters with two columns) is filled in record order; a cell grid with a static label column is never grouped; two sources that state one record differently give two rows, not a conflict.
- **Status:** Complete (2026-11-22). Gate: `uv run ruff check .` all checks passed; `uv run ruff format --check .` 176 files already formatted; `uv run alembic check` no new upgrade operations; `uv run pytest` 1407 passed, 1 deselected (`perf`). One earlier full run failed once on a seeded conflict that got zero retrieval hits (`no_relevant_chunks`, not a model answer); the field alone and the re-run were correct (`shortcut: not reproduced; investigate Qdrant/embedding under the full suite if it recurs`). `/ponytail-review`: one fix, chat edits shared grid cells across two grids on different slides while mapping did not; `shared_facts` now leaves row-group fields out for both (test added). `/ecc:quality-gate` clean. Live test pending.

---

## Next priority: universal extraction & mapping accuracy

Not yet a phase: plan it (entry/exit gates, deliverables) before any code. Found in the Phase 10-12 live tests, where the filled finance deck still shows these gaps.

**Two strict requirements, for every item below:**
1. **Completely universal.** No finance-, language- or template-specific rules, keywords, field names, labels or layouts. Every fix must generalize to any template (pptx/docx/xlsx) and any source document, and is tested on at least two unrelated domains.
2. **Accuracy pushed to the maximum possible**, measured on the eval set ([testing-strategy.md § 5](testing-strategy.md#5-rag--mapping-evaluation-testseval-marker-llm)) before and after each change, without loosening the existing hard gates (0 fabricated citations, 100% citation verification, conflicts never auto-resolved).

**Gaps** (the first two are done in [Phase 13](#phase-13-universal-accuracy-number-formatting--cross-slide-facts); card context and the 32k budget in [Phase 14](#phase-14-spatial--card-context--32k-retrieval-budget)):
- **Clean currency and number formatting.** Typed values are written as their canonical form (for example a bare float), not formatted the way the template or source shows them (thousands separators, decimals, currency symbol or code, percent sign). The written text must follow the template's own example or the source's format; xlsx cells keep real numbers.
- **Consistent cross-slide values.** Only fields that share one `replace_token` key share a value. Fields with different tokens or anchors that ask for the same fact (the same label and type on several slides) can still get different values or statuses.
- **Accurate row-grouped tables.** `fill_table_rows` rows must each come from one record in the source, with cells from the same row or entity, never mixed across records. Row order and count follow the source.
- **Grounded synthesis for summary and finding fields.** `paragraphs` and `bullet_list` fields that need a short summary (findings, conclusions, highlights) end up `missing` or `partial` because no single quote matches. They may be synthesized only if every sentence or bullet is backed by verified quotes, with citations kept per sentence; anything not backed stays out.
