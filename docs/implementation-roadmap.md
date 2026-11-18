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
8. Phases 5 and 10 only: `/ponytail-audit` on the whole repo.

Common gate for every phase ≥ 1: `uv run ruff check . && uv run ruff format --check . && uv run pytest tests/unit` green.

## Status

| Phase | Name | Status |
|---|---|---|
| 0 | Architecture & source-of-truth docs | **Complete** |
| 1 | Foundation, infrastructure & database | **Complete** |
| 2 | Ingestion, storage & Open WebUI integration check | pending |
| 3 | Embeddings & vector store | pending |
| 4 | LLM gateway & grounded Q&A | pending |
| 5 | Template intelligence (Anchor Map + IR) | pending |
| 6 | Mapping engine | pending |
| 7 | Surgical writers & artifacts | pending |
| 8 | Open WebUI Pipe & chat orchestration | pending |
| 9 | Conversational edits & run versioning | pending |
| 10 | Hardening, evaluation & audit | pending |

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
  - fixture corpus `tests/fixtures/sources/` with one file per format, including one scanned PDF;
  - **Open WebUI integration check** against the pinned Open WebUI image (manual, using a throwaway Pipe in Open WebUI's function editor; no Pipe code is committed before Phase 8):
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
  - `embeddings/bge_m3.py` (dense + sparse, MPS fp16, batch 16), `embeddings/reranker.py` (lazy, flag-gated);
  - `vector/qdrant_store.py` (collection bootstrap with named vectors + payload indexes, upsert, hybrid RRF search, tenant-filtered retrieve/delete);
  - `mapping/retriever.py`;
  - ingestion completes to `indexed`; DELETE removes points.
- **Exit:**
  - `tests/security/test_qdrant_isolation.py`: user B's searches, retrieves, and deletes see 0 of user A's points, across the matrix (same project id / other user, etc.);
  - retrieval smoke test on fixtures: the expected chunk is in the top 8 for 10/10 seeded queries.

## Phase 4: LLM gateway & grounded Q&A

- **Entry:** Phase 3 done.
- **Deliverables:**
  - `llm/gateway.py` (`OpenAICompatClient`, `AnthropicClient`, JSON-schema output + 1 retry);
  - `mapping/verifier.py` (normalize, quote check, typed-value check);
  - `chat/qa.py`;
  - `POST /v1/projects/{project_id}/query`.
- **Exit:**
  - Verifier unit tests (exact, whitespace/quote normalization, wrong chunk, cross-tenant chunk, too-short quote, typed value absent);
  - `uv run pytest tests/eval -m llm -k qa` passes: 0 unverified citations returned, and "not found" for every unanswerable question in the set.

## Phase 5: Template intelligence (Stages 1–2)

- **Entry:** Phase 4 done.
- **Deliverables:**
  - `templates/anchors_{pptx,docx,xlsx}.py`, `templates/ir_builder.py` (LLM + validator), `templates/schemas.py`;
  - the `extract_template` job;
  - template endpoints;
  - fixture templates `tests/fixtures/templates/` (pptx with tables, docx with header/footer + content controls, xlsx with formulas).
- **Exit:**
  - Golden Anchor Map tests per fixture;
  - validator rejects unknown/duplicate anchors and invalid `write_mode`;
  - an IR built for each fixture passes the validator.
- **Checkpoints:** `/ponytail-audit` (whole repo).

## Phase 6: Mapping engine (Stage 3)

- **Entry:** Phase 5 done.
- **Deliverables:**
  - `mapping/extractor.py`, `mapping/engine.py` (per-field state machine, status resolution, confidence tiers, progress);
  - the `run_mapping` job up to `verifying`;
  - `POST/GET …/mapping-runs`.
- **Exit:**
  - Unit tests for every status rule (0/1/2+ candidates, merge-equal, typed-value rejection, partial conditions, `llm_error` → missing without failing the run);
  - eval: **0 fabricated citations** on the mapping eval set, the seeded conflict fixture yields `conflict`, and absent fields yield `missing` (≥ 95%).

## Phase 7: Surgical writers & artifacts (Stage 4)

- **Entry:** Phase 6 done.
- **Deliverables:**
  - `writers/pptx_writer.py`, `docx_writer.py`, `xlsx_writer.py`, `markers.py`;
  - the `rendering` stage of `run_mapping` and the `render_artifact` job;
  - artifact endpoints + signed download.
- **Exit:**
  - Fidelity tests per format: untouched parts C14N-equal (XLSX: byte-equal zip entries); written runs keep `rPr`/`s` style; markers written for missing/conflict; formula cells untouched; output opens in python-pptx/python-docx/openpyxl;
  - signed URL tests (tamper → 403, expired → 410, unknown `artifact_id` → 404); `GET …/artifacts` on another user's project → 404.

## Phase 8: Open WebUI Pipe & chat orchestration

- **Entry:** Phase 7 done.
- **Deliverables:**
  - `integrations/openwebui/doc_intel_pipe.py`;
  - `POST /v1/pipe/files`, `POST /v1/pipe/chat` (SSE);
  - `chat/router.py`, `chat/summary.py`;
  - template confirmation flow;
  - compose env to bypass Open WebUI RAG, using the setting verified in Phase 2.
- **Exit:**
  - Pipe unit tests (task-call skip, SSE → event_emitter mapping, file upload once per file);
  - API tests for the SSE event sequence;
  - manual end-to-end in Open WebUI recorded in the PR: upload 3 sources + 1 pptx, "fill the template" → summary + working download link.

## Phase 9: Conversational edits & run versioning

- **Entry:** Phase 8 done.
- **Deliverables:**
  - `chat/edits.py`;
  - `POST …/mapping-runs/{run_id}/edits`;
  - router `edit` intent;
  - new run + artifact per edit.
- **Exit:**
  - Tests: every `EditOp` and its validation errors (422, nothing applied); parent run unchanged; version increments; `choose_candidate` only on `conflict`;
  - end-to-end chat edit produces v2 artifact.

## Phase 10: Hardening, evaluation & audit

- **Entry:** Phase 9 done.
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
