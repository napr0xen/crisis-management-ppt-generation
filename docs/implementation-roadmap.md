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
| 14 | Card context & 32k retrieval budget | **Complete** (live-tested; `main` at `1a3b6b4`) |
| 15 | Row-grouped table extraction | **Complete** (live-tested; `main` at `1a3b6b4`) |
| 16 | Grounded synthesis for summary & finding fields | **Complete** (live-tested; `main` at `1a3b6b4`) |
| 17 | MLX provider, token scanner fix, tile-card & numbered-list grouping | **Complete** (on branch, awaiting live test) |
| 18 | Native PPTX chart population & generation | **Complete** (on branch, awaiting live test) |
| 19 | Extraction edge cases, conflict API & custom web UI | **In progress** (branch `phase-19-custom-web-ui`) |
| next | Universal extraction & mapping accuracy | all four gaps addressed in Phases 13-16; Phases 17-18 follow the Phase 14-16 live test |

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

## Phase 16: Grounded synthesis for summary & finding fields

The fourth gap of the [next priority](#next-priority-universal-extraction--mapping-accuracy) list (cause: `paragraphs`/`bullet_list` summary fields ended `partial`, `missing`, or, measured in this phase, a fake `conflict` of one candidate per source paragraph; verbatim lists overflowed their slot).

- **Entry:** Phase 15 done (branch `phase-15-row-grouped-tables`, `98515e9`).
- **Deliverables:** `extractor.synthesize` (one call, one corrective retry when the reply overflows `max_items`/`max_chars`), `verifier.supported` (exact canonical numbers, >= 60% content words, <= 400 chars per item), `engine._synthesized` (structural trigger, all-or-fallback replacement, confidence `0.7`), `Evidence.item` (per-sentence citations), the chat summary line, and the list one-candidate extraction rule ([system-design.md § 7](system-design.md#7-mapping-engine-stage-3) step 5).
- **Exit:**
  - Tests on two or more unrelated domains (museum survey, warehouse incident; Spanish and German text for `supported`): trigger and its refusals (not found, no chunks, LLM error, conflict, fitting verbatim, other types), unbacked items dropped, unretrieved quotes rejected, `1.8` never backed by `18`, injection text is data, overflow retry, verbatim value replaced only by a complete fitting synthesis, `Evidence.item` round-trips, old runs without it validate;
  - eval hard gates pass, before/after recorded; `uv run ruff check .`, `uv run ruff format --check .`, `uv run alembic check`, `uv run pytest` green.
- **Eval** (`qwen3:14b`, `LLM_NUM_CTX=32768`; 85 fields: summary and findings fields in museum, app beta, warehouse and Spanish library sources added; before = Phase 15 code: the full 81-field run plus a probe of the 4 fields added after it):

  | Metric | Before | After |
  |---|---|---|
  | Fabricated citations | 0 | 0 |
  | Citation verification | 100% | 100% |
  | Seeded conflicts detected | 100% (5/5) | 100% (5/5) |
  | Absent fields `missing` | 100% | 100% (19/19) |
  | Field accuracy | 93.3% (56/60) | 95.0% (57/60) |
  | Values over `max_chars`/`max_items` | not measured (metric added here) | 3 |
  | Synthesized values in the run | n/a | 0 |

  The accuracy gain is the one-candidate rule: the one-sentence incident summary was a fake conflict and is now `populated`. The 3 overflows are verbatim but correct values (museum summary, incident summary, Spanish findings), kept because the synthesis did not pass the bar in this run: one paraphrase item had 56% word support, one retry was still 20 characters over, and the third varied by run (probes on the same input gave a fitting, fully backed 150-character summary in one run and the verbatim fallback in another). Q&A eval unchanged.
- **Trade-offs:** synthesized values are `populated` at confidence `0.7` only when every item passes `supported`; the word check is a sanity filter (an item can still add a few unbacked words), the hard guarantees are verified quotes and exact numbers. A field with limits can cost up to 4 LLM calls. Synthesis is not deterministic on `qwen3:14b`; when it fails the verbatim value stays (never a worse value). For `paragraphs`/`bullet_list`, items from different chunks are now one candidate, so a list conflict needs chunks that contradict each other about the same item.
- **Status:** Complete (2026-11-22). Gate: `uv run ruff check .` all checks passed; `uv run ruff format --check .` 178 files already formatted; `uv run alembic check` no new upgrade operations; `uv run pytest` 1460 passed, 1 deselected (`perf`). `/ponytail-review`: before the eval, synthesized numbers were compared with separators stripped (`1.8` matched `18`); fixed to canonical numbers (test added), then ship. `/ecc:quality-gate` clean. Live test pending.

---

## Phase 17: MLX provider, token scanner fix, tile-card & numbered-list grouping

Cause (Phase 14-16 live test on a 9-slide deck): `[# 1]`..`[# 6]` stage counters were never detected (the bracket token needed a letter) and stayed in the output; repeating KPI, award and risk cards were mapped slot by slot, so sibling slots came back `missing`/`conflict` and award values numbered `[Value 5..7]` fell out of the 1..3 grid; numbered `[Key message 1..3]`, `[Insight 1..3]` lists cost one call per slot and gave duplicate sentences or conflicts; and a faster local LLM server was wanted.

- **Entry:** Phase 16 done and live-tested (`main` at `1a3b6b4`).
- **Deliverables:**
  - `LLM_PROVIDER=openai_compat` (renamed from `openai`): `max_tokens` (same cap as Ollama), `temperature: 0`, `chat_template_kwargs.enable_thinking` (opt-in via `LLM_CHAT_TEMPLATE_KWARGS`, so strict servers never get it), `response_format` json_schema, `<think>` stripping via `complete_json`; `mlx_lm.server` launch command in `.env.example` and development-guidelines § 8 ([DECISIONS.md ADR-005](DECISIONS.md)).
  - Token scanner: `[…]` with any non-space first character and a letter or digit inside (`[# 1]`, `[$ 0.0]`); `[1]` markers and symbol-only units (`Cost [€]`) are not tokens. Own-label context `heading / own label` ([system-design.md § 4](system-design.md#4-anchormap)).
  - `Anchor.card` / `IRField.card` (code-set, hidden from the LLM): repeating cards found by identical container shapes, numbered in reading order.
  - Mapping: tile groups and numbered lists in `row_groups`; one records call per group (`extractor.extract_records`, prompt `slot_records.md`, records keyed by `n`), per-record verification; lists collapse duplicates and fall back to grounded synthesis ([system-design.md § 7](system-design.md#7-mapping-engine-stage-3)).
- **Exit:**
  - Tests on two or more unrelated domains (museum, clinic, garden club, ferry, cycling scheme, choir): provider request shape, think stripping, schema-ignoring server retried; token rules incl. linearity; own label; card detection and refusals; tile and list grouping and refusals; keyed records with skipped `n`; list duplicate collapse; synthesis fallback and its refusals (not after `not_found`, not for typed slots, only when it fills more slots); unretrieved quotes rejected; injected text is data;
  - eval hard gates pass, before/after recorded; `uv run ruff check .`, `uv run ruff format --check .`, `uv run alembic check`, `uv run pytest` green.
- **Eval** (`qwen3:14b`, `LLM_NUM_CTX=32768`; 99 fields: a 3-card station tile group keyed by district, stated in another order in the source, a prose `[Highlight 1..3]` list and an absent `[Sponsor 1..2]` list added; before = Phase 16 code on the same 99 fields, `card` stripped since that code has no tiles):

  | Metric | Before | After |
  |---|---|---|
  | Fabricated citations | 0 | 0 |
  | Citation verification | 100% | 100% |
  | Seeded conflicts detected | 100% | 100% |
  | Absent fields `missing` | 100% | 100% |
  | Field accuracy | 91.7% (66/72) | 95.8% (69/72) |
  | Latency p50 / max | 5.6 s / 29.1 s | 4.1 s / 30.3 s |

  The gain is the highlights list (conflict, conflict, missing before; all three populated and distinct after, verbatim in one records call). The keyed station cards were already right field by field and stay right in one call instead of nine. The 3 misses are the Phase 15 roster row ("drove the delivery van" written as "Driver"), unchanged.
- **Trade-offs:** cards drawn without a container shape are not tiles; a keyed record is tied to its heading by the prompt only (group fields skip the per-field candidate check); synthesized list items are not cut to their own slot's `max_chars`. Templates registered before Phase 17 have no `card` and keep the old grouping until re-registered.
- **Status:** Complete (2026-11-22). Gate: `uv run ruff check .` all checks passed; `uv run ruff format --check .` 181 files already formatted; `uv run alembic check` no new upgrade operations; `uv run pytest` 1556 passed, 1 deselected (`perf`). `/ponytail-review`: symbol-only brackets (`Cost [€]`) had become tokens, fixed to letter-or-digit (test added); `openai_compat` cloud limit documented; synthesized list slots now carry `item = 0` so the chat summary names them. A local scan of the live deck then showed the stage counters still got only the section heading, so the own label is now appended (`Sourcing Pipeline / Requirement`, test added). `/ecc:quality-gate` clean. Live test pending.

---

## Phase 18: Native PPTX chart population & generation

Cause: charts were skipped by the IR prompt, so a template's native chart kept its sample numbers (the live deck's spend chart showed `[Category 1..6]` with values 6..1) and a chart placeholder stayed a token.

- **Entry:** Phase 17 done (branch `phase-17-mlx-and-grouping`, `ee4658f`).
- **Deliverables:** `chart` anchor kind with `ChartRef` (type, series, category count; XY, bubble and seriesless charts skipped), `expected_type` `chart`, write modes `fill_chart` (`chart.replace_data`, styling kept, title tokens unwrapped) and `insert_chart` (native chart at the placeholder's box, type from the data's shape: pie / line / bar / column); IR rules and series columns set by code; `engine._chart_result` (one point per row, kept only when one verified quote states the category and its numbers in order, `_states_point`; at least 2 points or `missing`); empty chart plus marker title for `missing`/`conflict`; chat edits for chart rows ([system-design.md § 4-8](system-design.md#4-anchormap)).
- **Exit:**
  - Tests on two or more unrelated domains (weather station rainfall, library loans by genre, survey shares): scanner ids unchanged by charts, IR validation both ways, point verification (placeholder, non-numeric, duplicate, unanchored, unretrieved, injected, `1.8` vs `18`, swapped pairs in a shared quote, number before its category), `chart_type_for` branches incl. Spanish months and quarters, `replace_data` and `add_chart` outputs reopened with exact values and boxes, markers on both modes, only chart parts change;
  - eval hard gates pass, before/after recorded; `uv run ruff check .`, `uv run ruff format --check .`, `uv run alembic check`, `uv run pytest` green.
- **Eval** (`qwen3:14b`, `LLM_NUM_CTX=32768`; 102 fields: a `fill_chart` rainfall chart stated in prose and a markdown table, an `insert_chart` loans-by-genre chart and an absent wind-speed chart added; before = Phase 17, where `chart` fields could not exist):

  | Metric | Before (Phase 17, 99 fields) | After (102 fields) |
  |---|---|---|
  | Fabricated citations | 0 | 0 |
  | Citation verification | 100% | 100% |
  | Seeded conflicts detected | 100% | 100% |
  | Absent fields `missing` | 100% | 100% (absent chart `missing`) |
  | Field accuracy | 95.8% (69/72) | 95.9% (71/74) |
  | Chart fields correct | not supported | 2/2 populated with exactly the verified points |

  The 3 misses are the Phase 15 roster row, unchanged. An earlier run dropped the table months May and June (`| May | 48 |` is 6 characters after normalization, under the 8-character quote minimum, so the chart was `partial`); the row prompt now asks for such a row to be quoted with its neighbour, and `_states_point` keeps a multi-point quote from backing a swapped pair. Another run in this phase gave `power_outage.estimated_cost` a conflict (an untouched path, `missing` again on re-run): run variance, noted.
- **Trade-offs:** numbers carry no K/M scaling (`$1.2M` is 1.2); a number written before its category is not read as a point (the chart may end `partial`/`missing`, never wrong); the inserted chart type is a rule of thumb on the data's shape; a chart placeholder inside a group gets group child coordinates; docx/xlsx charts are not touched.
- **Status:** Complete (2026-11-22). Gate: `uv run ruff check .` all checks passed; `uv run ruff format --check .` 183 files already formatted; `uv run alembic check` no new upgrade operations; `uv run pytest` 1633 passed, 1 deselected (`perf`). `/ponytail-review`: `markers.content` turned any `fill_table_rows` text value into one-character rows, guard restored (test added); seriesless charts now skipped; the eval finding above led to `_states_point` and the short-row quote rule (tests added). `/ecc:quality-gate` clean. Live test 1 on `mlx_lm.server` (`mlx-community/Qwen3-14B-4bit`) failed `extract_template` with `LLMInvalidOutput` (it ignores `response_format`, so JSON came back fenced or off-schema); fixed on this branch: `openai_compat` appends the schema to the prompt and cuts the JSON object out of fences or prose, and every JSON retry names the failing fields (ADR-005, tests added). Live test 2 on `mlx_lm.server` (charts, Slide 3 key messages, Slide 4 commentary, Slide 9 approvals right) caught three Phase 17 grouping regressions, fixed on this branch (tests on two domains each): keyed cards (distinct static headings) are no longer one records call and map per card again; record cells are anchored per record (re-quoted from the cited chunk's own line; a value shifted in from the next record is dropped); `#` count tokens are typed `number`; `slot_records.md` asks for every record with its own quotes and has an example; a short group logs `group_short`; the IR prompt says an empty native table is always a slot. Live test 3 (`cb2ff2d`) found five more gaps, fixed on this branch with tests on two or more domains each: month-year tokens typed `text`; empty native tables get a code-made `fill_table_rows` field; quote recovery from the cited chunk's lines and its adjacent retrieved chunks (wrong-neighbour citations, table header glued onto rows 2..N); the heading check runs only for slots with sibling headings; `#` count slots need a whole unit-less number stated for their own label and `text` slots one table cell, with one corrective retry; a tile never repeats one value in two slots; the live health test reads `LLM_PROVIDER`. Live test pending.

---

## Next priority: universal extraction & mapping accuracy

Not yet a phase: plan it (entry/exit gates, deliverables) before any code. Found in the Phase 10-12 live tests, where the filled finance deck still shows these gaps.

**Two strict requirements, for every item below:**
1. **Completely universal.** No finance-, language- or template-specific rules, keywords, field names, labels or layouts. Every fix must generalize to any template (pptx/docx/xlsx) and any source document, and is tested on at least two unrelated domains.
2. **Accuracy pushed to the maximum possible**, measured on the eval set ([testing-strategy.md § 5](testing-strategy.md#5-rag--mapping-evaluation-testseval-marker-llm)) before and after each change, without loosening the existing hard gates (0 fabricated citations, 100% citation verification, conflicts never auto-resolved).


## Phase 19: Extraction edge cases, conflict API & custom web UI

Cause (live test of Phases 14-18 on a cash-report deck): bracket tokens in a reconciliation table's cells stayed literal in the output; two side-by-side tables (largest receipts / largest payments) had no heading context of their own, so one could take the other's rows; a credit row appeared in a debits table; a keyed card slot (`Risk:` under `Price inflation`) came back as its own heading. Open WebUI shows conflicts as text only, so a user cannot pick a candidate with its source in one click.

- **Entry:** Phase 18 merged (`main` at `63a821c`).
- **Plan:**
  1. Token cells (`ir_builder`, `runs.fill_rows`): a native table whose every body row is a static label in column 0 plus token cells is a label/value grid, never `fill_table_rows`; every token in a table cell that no field covers gets a code-made `replace_token` field; `fill_rows` clears tokens left in body cells it did not write (marker or fewer rows).
  2. Sibling tables (`anchors_pptx`): table cells carry the nearest heading (`_context`) before the row/column label, so each side-by-side table has its own context, query and prompt.
  3. Direction (`table_rows.md`, `slot_records.md`, extractor specs): row prompts get the sibling headings on the slide and exclude rows that contradict the table's heading or direction; rows the candidate check places under a sibling heading are dropped.
  4. Heading echo (`engine._rule_note`): a text value whose words are all words of its own context is rejected, with one corrective retry; still echoing, it is `missing`.
  5. API: `GET .../mapping-runs/{run_id}/conflicts`, `POST .../mapping-runs/{run_id}/resolve-conflicts` (edit ops under the hood, waits for the render, returns the artifact and the remaining conflicts); `GET/POST/DELETE /v1/library/templates` and `GET /v1/library/scans/{scan_id}` (ADR-014 supersedes the CLI-only part of ADR-012); `pipe/chat` takes an optional `library_template_id` or `template_document_id` that skips routing; CORS for `http://localhost:5173`; `frontend/dist` served at `/` when built.
  6. `frontend/` (React, TypeScript, Vite, Tailwind): chat sessions sidebar with a `+` menu (new chat, Templates), Templates page (drag-and-drop upload, scan status, counts, delete with confirmation), chat workspace (multi-file attachments, template selector, live progress, download card, inline conflict cards with candidates and a custom value, "Apply & Re-render PPTX").
- **Exit:** tests on two or more unrelated domains for each of fixes 1-4 (no domain or template hardcoding); API tests incl. tenant isolation (`404`) on the new run-scoped routes; `uv run ruff check .`, `uv run ruff format --check .`, `uv run alembic check`, `uv run pytest -m "not slow"`, `npm run build` in `frontend/` green; `/ponytail-review`, `/ecc:quality-gate`.
  7. Live-test fixes (procurement and finance decks on `mlx_lm.server`): `_repair` swaps a bracket-less `replace_token` token for the anchor's own token when exactly one matches (procurement failed `ir_invalid` at slide 6); the pptx writer shrinks text that overflows its box with `normAutofit fontScale`; text slots of one box that got the same value get one comparative role check (`slot_roles.md`), and a role the source never states becomes `missing` (finance "Bank / institution" got the account holder's name). See [system-design.md](system-design.md) § 5, § 7 and § 8.
  8. Live-test fixes (procurement deck, run `e9f05471`, 85 populated / 27 partial / 7 missing, 12.5 min): a `fill_cell` cell typed as a number that states a word with no digit is verified as verbatim text (5 supplier ratings were missing and made 25 correct cells partial); a card's text slot that only restates its tile's number is `missing`, and `slot_records.md` asks for the measure's name (4 KPI captions repeated their values); picture tokens (`[Company Logo 1]`, `[Image 2]`) are dropped from the IR, `IR_BUILDER_VERSION` 2; a `#` count quoted as a table row reads its label from that table's header row in the same chunk (6 counts were each remapped alone, 6 retrievals and 12 calls). `LLM_HYBRID_THINKING=false` measured lossless on 70 fields.
  9. Fast-first, think on escalation (`THINK_ON_ESCALATION`, `THINK_ESCALATION_MAX_TOKENS`, `THINK_ESCALATION_SCORE_THRESHOLD`): every first pass stays `think=False`; a conflict, a verification failure or a high-score miss gets one bounded thinking `extract` (budget on top of the reply cap, Ollama and `openai_compat`), `populated`/`partial` never do, a `missing` or failed retry keeps the first result. Tables, charts and record groups escalate once per group (also for an empty group or a column empty in every row), kept only if they fill more cells; an escalated list is re-checked against its length limits. See [system-design.md](system-design.md) § 7.
  10. Finance statements (cause: a MXN bank-statement deck took ~23 min with 34 missing / 25 partial, mostly `verification_failed` and `high_score_miss`, on ~4.5k-token prompts): locale-aware quote numbers (one thousands group read both ways, `DR`/`CR` tags and a trailing minus either sign) and dates (upper-case Spanish, `31/MAR/2025`, `31-mar-25`, unambiguous numeric `31/03/2025`); `IRField.mode` `extractive` | `synthesis` set by the IR model, a synthesis text/list slot written by grounded synthesis without a verbatim pass and never escalated on a high-score miss, a synthesis number derived as the sum or difference of two quoted figures and always `partial`; the context budget is claimed by a batch's opening chunks, then scored hits, then 0.0-score neighbor chunks (`RETRIEVAL_CONTEXT_TOKENS` stays 4500: at 3072 the mapping eval lost a seeded conflict); a synthesis slot gets no heading check and keeps an item worded unlike its quotes when every number it states is quoted, as `partial`; `IRField.mode` is required in the IR model's reply schema (`BatchReply`).
- **Status:** Complete (2026-11-26, closed by the user). Items 1-9 are on branch `phase19`; item 10 continued as [Phase 20](#phase-20-finance-statement-accuracy-locale-verifier-synthesis-mode-context-pruning). Last recorded gate: `ruff check .`, `ruff format --check .` and unit tests green; the full eval failed only `absent_missing` 0.91 < 0.95 (model false positives that predate this work, accepted by the user). The full `pytest` and `npm run build` were not re-run after the last fixes.

**Gaps** (the first two are done in [Phase 13](#phase-13-universal-accuracy-number-formatting--cross-slide-facts); card context and the 32k budget in [Phase 14](#phase-14-spatial--card-context--32k-retrieval-budget); row-grouped tables in [Phase 15](#phase-15-row-grouped-table-extraction); grounded synthesis in [Phase 16](#phase-16-grounded-synthesis-for-summary--finding-fields)):
- **Clean currency and number formatting.** Typed values are written as their canonical form (for example a bare float), not formatted the way the template or source shows them (thousands separators, decimals, currency symbol or code, percent sign). The written text must follow the template's own example or the source's format; xlsx cells keep real numbers.
- **Consistent cross-slide values.** Only fields that share one `replace_token` key share a value. Fields with different tokens or anchors that ask for the same fact (the same label and type on several slides) can still get different values or statuses.
- **Accurate row-grouped tables.** `fill_table_rows` rows must each come from one record in the source, with cells from the same row or entity, never mixed across records. Row order and count follow the source.
- **Grounded synthesis for summary and finding fields.** `paragraphs` and `bullet_list` fields that need a short summary (findings, conclusions, highlights) end up `missing` or `partial` because no single quote matches. They may be synthesized only if every sentence or bullet is backed by verified quotes, with citations kept per sentence; anything not backed stays out.

## Phase 20: Finance-statement accuracy (locale verifier, synthesis mode, context pruning)

Cause: Phase 19 item 10 (a MXN bank-statement deck, ~23 min, 34 missing / 25 partial), carried on branch `phase20` (commit `014d819`).

- **Entry:** Phase 19 items 1-9 on `phase19`.
- **Plan:** Phase 19 item 10 (locale-aware verifier, `IRField.mode`, grounded synthesis path, derived sum/difference figures as `partial`, curate budget tiers, `IR_BUILDER_VERSION` 3).
- **Status:** Complete (2026-11-26, closed by the user). Last recorded gate: as for Phase 19 (full eval `absent_missing` 0.91 < 0.95 accepted; full `pytest` not re-run after the last fixes). Live finance run after it: 64 populated / 37 partial / 15 missing of 116. An audit of that run found the gaps that Phase 21 closes.

## Phase 21: Zero-Fabrication Slot Guards, Token Coverage, and Verifier Diagnostics

Cause (audit of the Phase 20 live runs on the finance and procurement decks): NET MOVEMENT was `populated` with the closing balance, because typed values are verified only as "the number is in a quote", and the one-value-two-roles check covered only `text` slots in one text box. The grid copies of `[Opening Balance]`, `[Net Cash Movement]` and `[Closing Balance]` (text boxes, not a native table) and procurement's `[Scoring method / thresholds]` got no IR field, because the code-made token fields covered only native table cells. Verification failures were retried blind (no reason given, none logged). Free-text slots the IR model marked `extractive` (Key Findings 1-4) had only a verbatim lookup.

- **Entry:** Phase 20 complete (`phase20` at `014d819`).
- **Plan:**
  1. One value, two roles, typed (`engine._check_slot_roles`): `number`/`currency`/`percent`/`date` slots of one slide, sheet or section with different labels that got the same value get the comparative role check (`check_roles`), as `text` slots of one box already do; a role the source does not state is `missing`. A value both roles really hold stays when the check keeps both.
  2. Token coverage for every anchor kind (`ir_builder._token_cells`): every token that no field covers gets a code-made `replace_token` field, not only native table cells (outside a table cell only a named token `{{x}}` / `[x]` / `<x>`: a bare `TBD` or `____` in prose may be the author's text); a token another field already covers in the same unit copies that field's label, description, type and mode. `IR_BUILDER_VERSION` 4; library IRs rebuilt.
  3. Verifier reasons in the retry (`engine._rejection`, `_escalated`): each rejected candidate's reason (`quote_not_in_chunk`, `number_not_in_quote`, `date_not_in_quote`, `slot_rule` with its rule note, `value_unusable`) goes back to the thinking retry through `extract(retry=...)`, over the cited chunks and their neighbors (`_near_cited`); reason codes are logged as `verification_rejected` (codes only, never text). A slot the role check rejects (item 1) gets one `map_field` retry told `slot_role: <value> is the value of <kept role>` (`engine._retry_roles`); the kept role is never dropped.
  4. Synthesis fallback for free-text slots: an `extractive` `text`/list slot that the model reports not found, with a slot large enough for prose (`engine._prose`: `fit_chars`, else `max_chars`, >= 40), gets one grounded synthesis attempt (`_synthesized`; numbered lists such as Key Findings 01-04 through `_list_results`), at most `partial` when weakly backed (`_backing`). Typed, short and unsized slots stay strict `not_found`.
- **Exit:** tests on two or more unrelated domains for each item; `uv run ruff check .`, `uv run ruff format --check .`, `uv run alembic check`, `uv run pytest` green; live re-run of both decks; `/ponytail-review`, `/ecc:quality-gate`.
- **Later phases** (from the same audit, in order): 5. a regression benchmark (`tests/eval/bench/`: gold values for real-shaped decks, populated-but-wrong rate, row alignment, multilingual cases); 6. Docling tables: collapse spanned cells (`data.grid` repeats them), join a table split across consecutive pages; 7. row and block provenance ids (`[P1-T2-R04]`) with structured rows in the chunk payload, each cell verified against its cited row; 8. a `derived` field mode, aggregates computed in Python over parsed table rows (needs 7 and an ADR).
- **Status:** Complete (2026-11-26). Live re-run of both decks (backend restarted on the Phase 21 code): procurement slide 5 `[Scoring method / thresholds]` and finance slide 8 Key Findings 01–04 and the slide 7 chart summary populated. `/ponytail-review` over `15f7263^..HEAD`: no tenant, citation or logging violation; fixed: a weak synthesized list item no longer replaces a verbatim `populated` slot (`_list_results`, new test in `test_prose_fallback.py`), and `_retry_roles` overwrites only `rag` shared copies; `_token_cells` covering instruction-style text-box tokens is left as a `# shortcut:`. `/ecc:quality-gate` (ruff check and format per changed file) clean. Final gate: `uv run ruff check .` all checks passed; `uv run ruff format --check .` 204 files already formatted; `uv run alembic check` no new upgrade operations; `uv run pytest tests/unit tests/integration tests/security` 2152 passed (eval not re-run after the review fixes; last full run as below).
  Known limits from the live finance run, carried to later phases (no clean general guard without an extra LLM call per typed slot): an extractive typed slot accepts a number that sits in its quote under another label (`NET MOVEMENT` took the closing balance from the header block; the reconciliation `Closing balance` took a "balance brought forward" row), because the slot-role check runs only when two slots on a slide share a value under different labels; slide 1 `CLOSING BALANCE`, `TOTAL MONEY IN`, `TOTAL MONEY OUT` went `missing` (`evidence_failed_verification`) in this run but populated in an earlier one, and candidates are not persisted, so the rejection codes are not recoverable. Fix belongs with item 7 (each value verified against its cited row's label).
  Items 3 and 4 done (2026-11-26): `ruff check .` all checks passed; `ruff format --check .` 204 files already formatted; `alembic check` no new upgrade operations; full `pytest` 2152 passed, 1 failed: the eval's `absent_missing` 0.913 < 0.95 (21/23, the Phase 19/20 baseline the user accepted; the engine before items 3 and 4 scores the same 0.913 on this eval, so no regression). Integration fixtures got the prose-sized slots' extra synthesis reply. Remaining: the live re-run of both decks, `/ponytail-review`, `/ecc:quality-gate`.
  Items 1 and 2 done (2026-11-26): `ruff check .` all checks passed; `ruff format --check .` 202 files already formatted; `alembic check` no new upgrade operations; `pytest tests/unit tests/integration tests/security` 2120 passed; library IRs rebuilt at `IR_BUILDER_VERSION` 4 (finance 119 fields, every token covered; procurement 89 fields, only the two logo tokens left uncovered). Remaining: items 3 and 4, the full `pytest` with the eval, the live re-run of both decks, `/ponytail-review`, `/ecc:quality-gate`.

## Phase 22: Regression Benchmark Harness & Docling Table Span/Multi-Page Cleanup

Cause: Phase 21 audit items 5 and 6 (see Phase 21 "Later phases"). The eval had no gold set for real-shaped decks and could not catch a value `populated` under the wrong label (`NET MOVEMENT` taking the closing balance) or a table row mixed across source rows; Docling's `data.grid` repeats a spanned cell's text in every grid position it covers, and a table split across consecutive pages is parsed as separate tables.

- **Entry:** Phase 21 complete (`phase-21-slot-guards` at `bf47bbb`).
- **Plan:**
  1. Regression benchmark (`tests/eval/bench/`): synthetic real-shaped cases (finance statement, procurement review, a multilingual case) with `gold.json`, scoring (a) field accuracy, (b) populated-but-wrong rate (a `populated` value that differs from gold, e.g. a label mismatch), (c) row-alignment accuracy (every cell of a populated template row comes from the same source row).
  2. Docling tables (`ingestion/docling_parser._parse`): collapse spanned/merged cells using the cells' real row/column spans instead of repeating `data.grid` text; join tables with the same header on consecutive pages into one table.
- **Exit:** failing tests first for each item; `uv run ruff check .`, `uv run ruff format --check .`, `uv run alembic check`, `uv run pytest tests/unit tests/integration tests/security` green; `/ponytail-review`, `/ecc:quality-gate`.
- **Later phases:** 7. row and block provenance ids (`[P1-T2-R04]`) with structured rows in the chunk payload, each cell verified against its cited row (Phase 23); 8. a `derived` field mode, aggregates computed in Python over parsed table rows (needs 7 and an ADR; Phase 23).
- **Status:** Complete (2026-11-26). Item 1: `tests/eval/bench/` (`score.py` pure scorer, `gold.json`, cases `finance_statement`, `procurement_review`, `multilingual`; `test_bench.py` live runner, only `fabricated_citations == 0` gated); `matches` and the chunk `ingest` shared with `test_mapping_eval.py`. Live bench baseline (MLX Qwen3-14B): field accuracy 0.905 (19/21), populated-but-wrong 0.0 (0/17), row alignment 1.0 (3 rows, all procurement slot rows; the finance and multilingual transaction tables did not come back `populated`, so no table row was scored live). Item 2: `docling_parser._table_rows` (each cell once at its top-left slot from `table_cells`) and `join_split_tables` (same header row, next page, chains), tests in `tests/unit/test_docling_tables.py` with real docling_core classes; not checked on a real multi-page PDF. `/ponytail-review`: fixed a gold `partial` with a matching value counted as populated-but-wrong, and the duplicated ingest block. `/ecc:quality-gate` clean on the 6 changed Python files. Gate: `uv run ruff check .` all checks passed; `uv run ruff format --check .` 209 files already formatted; `uv run alembic check` no new upgrade operations; `uv run pytest tests/unit tests/integration tests/security` 2176 passed (full mapping eval not re-run).

## Phase 23: Row & Block Provenance IDs (`[P1-T2-R04]`) & Per-Row Label Verification

Cause: Phase 21 audit item 7 and the Phase 21 known limit: an extractive typed slot accepts a number that sits in its quote under another label (`NET MOVEMENT` took the closing balance from the header block; the reconciliation `Closing balance` took a "balance brought forward" row), because a typed value is verified only as "the number is in a quote". Table cells carry no source-row identity, so a template row can mix cells from different source rows unseen.

- **Entry:** Phase 22 complete (`phase-22-bench-and-tables` at `4f23e90`).
- **Plan:**
  1. Provenance ids: deterministic ids per parsed element, `[P<page>-T<table>-R<row>]` for a table row and `[P<page>-B<block>]` for a text block; table chunks keep their structured rows (`{column header: cell value}` with the row id) in the chunk payload.
  2. Per-row label verification: when a typed value is cited from a labelled table row (or column), the row/column label must match the slot's role; a mismatch (`Balance brought forward` or `Net movement` for a `Closing balance` slot) is rejected with reason code `row_label_mismatch`, which the existing verifier-reason retry sends back so the model can find the true row.
  3. Row alignment: every cell of a template table row (row groups, `fill_table_rows`) must cite the same source row id; a row mixing source rows is rejected.
- **Exit:** failing tests first for each item (unit and integration); `uv run ruff check .`, `uv run ruff format --check .`, `uv run alembic check`, `uv run pytest tests/unit tests/integration tests/security` green; `/ponytail-review`, `/ecc:quality-gate`.
- **Later phases:** 8. a `derived` field mode, aggregates computed in Python over the structured rows (needs an ADR; Phase 24).
- **Status:** Complete (2026-11-26). Item 1: `chunker._provenance` / `_table_rows` give ids in element order (`P<page>-T<n>-R<nn>`, `P<page>-B<nn>`, no `P` segment without pages); payload keys `block_ids` and `rows` (`TableRow{id, cells}`), chunk text unchanged; `Hit` / `_hit` read them optionally (old points: none until re-ingested); `format_hit` shows `[<row id>]` before each table row; `LLMQuote` strips displayed row ids only. Item 2: `engine._row_label_note` in `_rejection` (the real gate): `row_label_mismatch` when the value's cells are labelled otherwise and another cell labelled like the slot states a value of its type; goes to the thinking retry with both row ids (`verifier.states_value`). Item 3: `engine._mixed_rows`: a `fill_table_rows` row whose cells no single source row states is dropped (`row_mixed` logged); in multi-row record groups the best row's cells are kept (`_stray_cells`), keyed cards exempt. Prompts `field_extract`, `slot_values`, `table_rows`, `slot_records` got one line on row ids. `/ponytail-review`: fixed keyed cards losing values from two source rows, `[B12]`-style references stripped from quotes, a text-only labelled row counted as the true row; wrapped Docling continuation rows looking mixed left as a `# shortcut:`. `/ecc:quality-gate` clean on the 14 changed Python files. Gate: `uv run ruff check .` all checks passed; `uv run ruff format --check .` 211 files already formatted; `uv run alembic check` no new upgrade operations; `uv run pytest tests/unit tests/integration tests/security` 2203 passed. Not run: the full mapping eval, the bench, a live re-run of the finance deck (needs a re-ingest for rows); label checks cover table rows only, a text-block header (not parsed as a table) is still unchecked.
