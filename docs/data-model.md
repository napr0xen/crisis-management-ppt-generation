# Data Model

**This file is the source of truth for every table, column, Qdrant collection, payload key, and storage path.** Other docs link here instead of restating them. Change a name here first, then fix the docs that reference it in the same commit.

Stores and ownership: see [ADR-008](DECISIONS.md#adr-008--persistence-boundaries-project--chat-versioned-jsonb-results).

## Conventions

- Primary keys: `id uuid DEFAULT gen_random_uuid()`.
- `user_id text` holds the Open WebUI user id. It is validated against `^[A-Za-z0-9_-]{1,64}$` before use, because it also appears in storage paths.
- Timestamps are `timestamptz NOT NULL DEFAULT now()`.
- Enums are `text` + `CHECK` constraints: easier to migrate than PG enums.
- **Tenant rule.** Every table except `projects` has `user_id` and `project_id` plus the composite FK `(project_id, user_id) REFERENCES projects(id, user_id) ON DELETE CASCADE`. Every repository query filters `WHERE user_id = :user_id AND project_id = :project_id`, with two named exceptions:
  1. the signed download looks up `artifacts.id`, then checks `user_id` through the HMAC;
  2. the job queue (`jobs/worker.py`): claiming the next job and startup recovery span all tenants, and touch only queue state. Everything after the claim uses the job's `user_id`/`project_id`.
  3. `library_templates` is a global, read-only catalog with no tenant keys. The CLI, `/v1/library` and the `TEMPLATES_DIR` sync write it (ADR-014, ADR-017); chat reads it to list templates and to copy one into the caller's project (`adopt`). It is never joined to tenant rows, and nothing in a tenant row references it.
- Migrations: Alembic in `migrations/` (from Phase 1).
- **Non-tenant FKs and deletes.**
  - `templates.document_id` → `documents` and `template_irs.template_id` → `templates` are `ON DELETE CASCADE`.
  - `mapping_runs.template_ir_id`, `mapping_runs.parent_run_id`, and `artifacts.mapping_run_id` have no `ON DELETE`. Deleting something that still has runs or artifacts fails, and the API maps that to `409`.
  - Deleting a project removes everything through the composite tenant FKs.

## PostgreSQL 16 schema

### `projects`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `user_id` | text NOT NULL | |
| `openwebui_chat_id` | text NULL | NULL for projects created via `POST /v1/projects` |
| `name` | text NOT NULL | chat id or user-supplied name |
| `created_at` | timestamptz | |
| `updated_at` | timestamptz | |

Constraints and indexes: `UNIQUE (id, user_id)` (target of the composite FKs); `UNIQUE (user_id, openwebui_chat_id)`; index `(user_id, created_at DESC)`.

### `documents`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `user_id`, `project_id` | text, uuid NOT NULL | tenant keys |
| `role` | text NOT NULL DEFAULT `'source'` | `source` \| `template` |
| `filename` | text NOT NULL | original name, display only, never used in paths |
| `mime_type` | text NOT NULL | |
| `size_bytes` | bigint NOT NULL | |
| `sha256` | char(64) NOT NULL | |
| `storage_key` | text NOT NULL | see Storage layout |
| `openwebui_file_ids` | text[] NOT NULL DEFAULT `'{}'` | every Open WebUI file id that uploaded this content (re-attaching a file creates a new id) |
| `status` | text NOT NULL | `uploaded` \| `parsing` \| `indexed` \| `failed` |
| `page_count` | int NULL | |
| `chunk_count` | int NULL | |
| `error_code` | text NULL | machine code only, no document text |
| `created_at`, `updated_at` | timestamptz | |

Constraints and indexes:
- `UNIQUE (project_id, sha256)`: re-uploading the same file is deduplicated. Re-uploading a `failed` `role = 'source'` document resets it to `uploaded` and queues a fresh ingest job. A duplicate upload appends its Open WebUI file id to `openwebui_file_ids` of the existing row.
- Index `(user_id, project_id, created_at DESC)`.
- Lookup by Open WebUI file id: `WHERE user_id = :u AND project_id = :p AND :file_id = ANY(openwebui_file_ids)`. `shortcut: no GIN index, add one if a project exceeds a few hundred documents.`

Every upload starts as `role = 'source'` and is indexed. Marking a document as a template sets `role = 'template'` and deletes its points from `doc_chunks`, because template placeholders must never be returned as evidence. A `role = 'template'` document is never re-ingested or indexed, so a failed duplicate upload of it is not retried. `POST …/templates` refuses documents still `uploaded` or `parsing`.

### `jobs`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `user_id`, `project_id` | tenant keys | |
| `kind` | text NOT NULL | `ingest_document` \| `extract_template` \| `run_mapping` \| `render_artifact` |
| `status` | text NOT NULL DEFAULT `'queued'` | `queued` \| `running` \| `succeeded` \| `failed` |
| `payload` | jsonb NOT NULL | input ids, e.g. `{"document_id": "…"}` |
| `progress` | jsonb NOT NULL DEFAULT `'{}'` | `{"stage": "embedding", "done": 12, "total": 40}`; `extract_template`: `{"stage": "ir", "done": i, "total": n, "part": "slide 3"}` (`part` absent for docx); `run_mapping`: `{"stage": "mapping", "done": i, "total": n, "field": "<field_id>"}` (`field` = the field now running) |
| `result` | jsonb NULL | output ids, e.g. `{"template_id": "…"}` |
| `error_code` | text NULL | the failing exception's `code` (e.g. `parse_error`), `internal` otherwise, `interrupted` if the backend restarted during the last attempt; `ir_invalid` (TemplateIR still invalid after its retry); `validation_error` (template over a limit, see [system-design.md § 4](system-design.md#4-anchormap)) |
| `error_message` | text NULL | safe text only, no document content |
| `attempts` | int NOT NULL DEFAULT 0 | the worker fails a job once `attempts` reaches 3 (constant `MAX_JOB_ATTEMPTS` in `jobs/worker.py`), or after the first attempt for an error marked `retry = False` (e.g. `parse_error`, `ir_invalid`, an LLM reply invalid twice) |
| `locked_at` | timestamptz NULL | set when claimed |
| `created_at`, `started_at`, `finished_at` | timestamptz | `started_at`/`finished_at` nullable |

Indexes: partial `(created_at) WHERE status = 'queued'`; `(user_id, project_id, created_at DESC)`.

Payload/result per kind:

| `kind` | `payload` | `result` |
|---|---|---|
| `ingest_document` | `{document_id}` | `{chunk_count, page_count}` |
| `extract_template` | `{document_id}` | `{template_id, template_ir_id, ir_version}` |
| `run_mapping` | `{mapping_run_id}` | `{mapping_run_id, artifact_id}` (`artifact_id` is `null` only if the run failed or vanished) |
| `render_artifact` | `{mapping_run_id}` | `{artifact_id}` |

### `templates`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `user_id`, `project_id` | tenant keys | |
| `document_id` | uuid NOT NULL UNIQUE | FK `documents(id)`; that document has `role = 'template'` |
| `format` | text NOT NULL | `pptx` \| `docx` \| `xlsx` |
| `anchor_map` | jsonb NOT NULL | `AnchorMap` ([system-design.md](system-design.md#4-anchormap)) |
| `anchor_count` | int NOT NULL | |
| `created_at` | timestamptz | |

The row is written when `extract_template` succeeds. Before that, the job is the only handle. If a row already exists for the `document_id` (e.g. the user asks to fill the same template again), `extract_template` reuses it and its latest IR and does not insert anything.

`extract_template` has no failure hook. On failure the job keeps its `error_code`, and the document keeps `role = 'template'` (its points are already purged). The recovery is to POST again.

### `template_irs`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `user_id`, `project_id` | tenant keys | |
| `template_id` | uuid NOT NULL | FK `templates(id)` |
| `version` | int NOT NULL | starts at 1 |
| `ir` | jsonb NOT NULL | `TemplateIR` ([system-design.md](system-design.md#5-templateir)) |
| `created_at` | timestamptz | |

`UNIQUE (template_id, version)`. Rows are immutable. `shortcut: v1 flows only ever create version 1 (extract_template reuses the latest IR); add an explicit re-extract action that inserts max(version) + 1 when users need to regenerate an IR.`

### `library_templates`

Reusable templates shared by every user (Phase 9). Registered with the CLI (`seed-template`), `POST /v1/library/templates` (ADR-014) or by the `TEMPLATES_DIR` folder sync (ADR-017, [system-design.md § 5.1](system-design.md#51-templates-folder-sync)), which build the `AnchorMap` and `TemplateIR` once, so chat runs skip `extract_template`.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | also the `template_id` inside `ir` |
| `name` | text NOT NULL UNIQUE | shown in the chat picker (1..100 chars) |
| `description` | text NOT NULL DEFAULT `''` | shown in the chat picker (up to 500 chars) |
| `category` | text NULL | free label (up to 50 chars), set at registration |
| `format` | text NOT NULL | `pptx` \| `docx` \| `xlsx` |
| `filename` | text NOT NULL | original filename (for synced and uploaded templates: the file's name in `TEMPLATES_DIR`, which matches the row to its file); becomes the copied document's `filename` |
| `sha256` | char(64) NOT NULL UNIQUE | |
| `size_bytes` | bigint NOT NULL | |
| `storage_key` | text NOT NULL | `library/{id}/original.{ext}` |
| `anchor_map` | jsonb NOT NULL | `AnchorMap` |
| `anchor_count` | int NOT NULL | |
| `ir` | jsonb NOT NULL | `TemplateIR` (version 1) |
| `ir_builder_version` | int NOT NULL DEFAULT 0 | `IR_BUILDER_VERSION` when the IR was built; the sync re-extracts rows with an older value (existing rows are 0) |
| `created_at` | timestamptz | |

**Copy-on-use.** When a chat picks a library template, `adopt` copies it into the caller's project in one transaction:
1. the bytes go to `uploads/{document_id}/original.{ext}`;
2. a `documents` row is inserted (`role = 'template'`, `status = 'indexed'`, no Qdrant points);
3. a `templates` row and a `template_irs` v1 row are inserted, with `ir.template_id` rewritten to the new `templates.id`.

From then on everything is the caller's own data. Mapping runs, artifacts and later edits never reference `library_templates`, so removing a library template doesn't affect existing runs. A second pick in the same project finds the copy by `(project_id, sha256)` and reuses its IR. If the same bytes are already a *source* document in the project, the pick is `409`.

### `mapping_runs`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `user_id`, `project_id` | tenant keys | |
| `template_ir_id` | uuid NOT NULL | FK `template_irs(id)` |
| `parent_run_id` | uuid NULL | FK `mapping_runs(id)`; set for `trigger = 'edit'` |
| `version` | int NOT NULL | `max(version) + 1` over all runs with the same `template_ir_id` (1 for the first), for both triggers; on a `UNIQUE` violation the insert is retried once |
| `trigger` | text NOT NULL | `initial` \| `edit` |
| `status` | text NOT NULL | `queued` \| `retrieving` \| `extracting` \| `verifying` \| `rendering` \| `completed` \| `failed`. `run_mapping` ends at `completed` (Phase 7); `failed` is set by the `run_mapping` failure hook. |
| `document_ids` | uuid[] NOT NULL | source documents in scope (snapshot at run start) |
| `results` | jsonb NOT NULL DEFAULT `'{}'` | `{ "<field_id>": FieldMappingResult }` |
| `summary` | jsonb NOT NULL DEFAULT `'{}'` | `{"populated": n, "partial": n, "missing": n, "conflict": n, "warnings": [{"field_id": "…", "code": "max_chars_exceeded"}]}`; warning `code` is `max_chars_exceeded` \| `formula_cell` \| `table_overflow` |
| `edits` | jsonb NULL | the edit ops applied, for `trigger = 'edit'` |
| `created_at`, `finished_at` | timestamptz | `finished_at` nullable; set at `completed` or `failed` |

Constraints and indexes: `UNIQUE (template_ir_id, version)`; index `(user_id, project_id, created_at DESC)`. Rows are immutable once `completed`. `results` keeps quotes and filenames as a snapshot (each evidence entry may carry an optional `row_id`, the `P1-T2-R04` id of the table row a `derived` value used); deleting a source document does not rewrite earlier runs.

### `artifacts`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `user_id`, `project_id` | tenant keys | |
| `mapping_run_id` | uuid NOT NULL UNIQUE | FK `mapping_runs(id)`; one artifact per run |
| `format` | text NOT NULL | `pptx` \| `docx` \| `xlsx` |
| `filename` | text NOT NULL | download name: `{template stem}_v{mapping_runs.version}.{ext}` |
| `storage_key` | text NOT NULL | |
| `sha256` | char(64) NOT NULL | |
| `size_bytes` | bigint NOT NULL | |
| `created_at` | timestamptz | |

Index `(user_id, project_id, created_at DESC)`.

### JSONB payload shapes

| Column | Shape | Defined in |
|---|---|---|
| `templates.anchor_map`, `library_templates.anchor_map` | `AnchorMap` | [system-design.md § AnchorMap](system-design.md#4-anchormap) |
| `template_irs.ir`, `library_templates.ir` | `TemplateIR` | [system-design.md § TemplateIR](system-design.md#5-templateir) |
| `mapping_runs.results` | object of `FieldMappingResult` keyed by `field_id` | [system-design.md § FieldMappingResult](system-design.md#6-fieldmappingresult) |
| `mapping_runs.edits` | array of `EditOp` | [api-specification.md § Edits](api-specification.md#post-v1projectsproject_idmapping-runsrun_idedits) |

## Qdrant

Accessed only through `src/doc_intel/vector/qdrant_store.py`.

**Collection `doc_chunks`** (name from `QDRANT_COLLECTION`; tests use their own `doc_chunks_test`, recreated per test session).

| Setting | Value |
|---|---|
| Named dense vector | `dense`: size `EMBEDDING_DIMENSIONS` (1024), distance Cosine |
| Named sparse vector | `sparse`: hashed-token BM25 term weights, `modifier = IDF` (Qdrant computes IDF; ADR-011) |
| Point id | `chunk_id` = `uuid5(CHUNK_NAMESPACE, f"{document_id}:{chunk_index}")`, `CHUNK_NAMESPACE = 6f0f5a3e-2b1c-4d7e-9a8b-3c5d7e9f1a2b` (fixed forever: changing it re-keys every point) |

**Payload.**

| Key | Type | Indexed |
|---|---|---|
| `user_id` | keyword | yes, `is_tenant: true` |
| `project_id` | keyword | yes |
| `document_id` | keyword | yes |
| `element_type` | keyword (`text` \| `table`) | yes |
| `filename` | string | no |
| `page_number` | integer \| null | no |
| `section_heading` | string \| null | no |
| `chunk_index` | integer | no |
| `text` | string | no (chunk text, used by the verifier) |
| `block_ids` | array of string | no (text chunks: provenance ids of the blocks in `text`, e.g. `P1-B03`, in order; a split block repeats its id; `[]` otherwise) |
| `rows` | array of `{id, cells, row_role}` \| null | no (table chunks: the data rows whose Markdown lines are in `text`, in line order; `id` like `P1-T2-R04` (`T2-R04` when there is no page), `cells` keyed by column header, an empty or duplicate header as `col<j>` (1-based); `row_role` ∈ `data` \| `carry_forward` \| `total` (Phase 27; read as `data` on older points). `null` for text chunks and for table chunks split as plain text (oversized header or row)) |

`kv` elements (Phase 27) are indexed as `element_type = 'text'` chunks whose `text` is one `Label: value` line per pair; `footer` elements are not indexed.

Provenance ids are `P<page>-T<n>-R<nn>` (table `n` counted per page, data row `nn` 1-based) and `P<page>-B<nn>` (text block per page), assigned in document order. They are not part of `text`. Points written before Phase 23 lack `block_ids` and `rows` (read as `[]` and `null`); re-ingest the document to get them.

**Isolation rules.**
- Upserts carry the tenant filter as Qdrant's `update_filter`, so a tenant can never overwrite another tenant's existing point (even by reusing its `document_id`).
- Upserts go in batches of 256 points (`UPSERT_BATCH`): a 1024-dim point is ~26 KB of JSON, and Qdrant rejects requests over 32 MB (~1,250 points).
- Every search, scroll, retrieve, and delete has a `must` filter on `user_id` **and** `project_id`, optionally narrowed with `document_id` `match.any`.
- `qdrant_store` methods take a required `TenantContext` and build the filter themselves. Callers can't pass raw filters.
- Retrieving by id (for verification) also applies the tenant filter: an id from another tenant returns nothing.

## Storage layout (`StorageBackend`, local root `./data/storage/`)

`storage_key` is the path relative to `STORAGE_ROOT`. Keys are built only from validated ids, never from user filenames.

```
data/
├── storage/                                   # STORAGE_ROOT
│   ├── users/{user_id}/projects/{project_id}/
│   │   ├── uploads/{document_id}/original.{ext}   # immutable, write-once
│   │   ├── parsed/{document_id}.json              # parser output, derived, overwritable on retry
│   │   └── artifacts/{artifact_id}.{ext}          # immutable rendered output
│   └── library/{library_template_id}/original.{ext}  # library templates, written by the CLI, `/v1/library` and the sync
```

PostgreSQL, Qdrant, and Open WebUI data live in Docker **named volumes** (`pgdata`, `qdrant_data`, `openwebui_data` in `docker-compose.yml`), not under `./data/`. Bind mounts on macOS hit Postgres permission/fsync issues. `docker compose down -v` deletes them.

- `{ext}` ∈ `pdf docx pptx xlsx txt csv md`.
- `StorageBackend.put_bytes(key, data, overwrite=False)` refuses to overwrite an existing key. `uploads/` and `artifacts/` are always written with `overwrite=False`. `parsed/` is derived data and is written with `overwrite=True`, so a retried ingest job can rewrite it.
- `delete` is used only when a document or project is deleted, or a library template is removed (`remove-template`).
- `data/` is git-ignored.

### Parsed document (`parsed/{document_id}.json`)

`ParsedDocument = {elements: [ParsedElement], page_count: int | null}`, elements in reading order (`ingestion/schemas.py`).

| `ParsedElement` key | Type | Meaning |
|---|---|---|
| `element_type` | `text` \| `table` \| `kv` | `kv` (Phase 27): one label→value pair found by layout |
| `text` | string | paragraph text; `Label: value` for `kv`; `""` for tables |
| `rows` | array of array of string \| null | tables only; `rows[0]` is the column header row |
| `page_number` | integer \| null | PDF page / PPTX slide; null for docx/xlsx/csv/txt/md |
| `section_heading` | string \| null | nearest heading (sheet name for xlsx) |
| `region` | `header` \| `kv` \| `table` \| `summary` \| `footer` \| `body` | Phase 27, default `body`. `table` for tables; `kv` for a single label→value pair; `summary` for pairs from a multi-column label line over an aligned value line; `header` / `footer` for PDF text labelled page header / footer by Docling, or in the top / bottom 8% band of the page when it is a page-number line or its digit-stripped text is in that band on 2+ pages (a one-off band line stays `body`) |
| `bbox` | `[l, t, r, b]` \| null | PDF points, top-left origin; null without Docling provenance |
| `label`, `value` | string \| null | `kv` only: the label (trailing `:` stripped) and the value text |
| `caption` | string \| null | tables only: a banner/title row 0 removed from `rows` (one non-empty cell that is the only Docling cell of row 0 or spans 2+ columns, table of 2+ columns and 3+ rows); when the next row holds an amount the table is headerless and `rows[0]` is all empty |
| `row_pages` | integer[] \| null | tables joined across pages only (Phase 31): the page of each row in `rows[1:]`; table chunks cite the page of their first row; `null` for a table on one page |

`TableRow` (built by the chunker, stored in the Qdrant `rows` payload): `{id, cells, row_role}`; `row_role` is `carry_forward` when a cell is a carry-forward phrase (`Balance brought forward`, `Carried forward`, `Saldo anterior`, …), `total` when a cell is a total phrase (`Total`, `Total charged`, `Subtotal`, `Suma`, …) and every other non-empty cell is an amount (`01/02 | TOTAL | 45,00` stays `data`), else `data`.
