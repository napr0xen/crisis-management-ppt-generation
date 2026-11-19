# System Design

Table, column, and path names come from [data-model.md](data-model.md); endpoints come from [api-specification.md](api-specification.md).

## 1. Module breakdown (`src/doc_intel/`)

| Module | Responsibility | Key files |
|---|---|---|
| `core/` | settings, logging, errors, auth, tenant context, URL signing | `config.py` (`Settings`), `logging.py`, `errors.py` (`AppError`), `security.py` (bearer check, HMAC signing), `tenancy.py` (`TenantContext(user_id, project_id)`) |
| `db/` | async SQLAlchemy 2.0 models and tenant-scoped repositories | `session.py`, `models.py`, `repo.py` |
| `storage/` | `StorageBackend` protocol + local implementation | `base.py`, `local.py` |
| `ingestion/` | upload validation, parse → chunk | `schemas.py` (`ParsedElement`, `ParsedDocument`, `ChunkPayload`), `upload.py` (format/magic/size checks, sha256, storage keys), `docling_parser.py`, `native_parsers.py`, `chunker.py`, `pipeline.py` |
| `embeddings/` | dense via Ollama, BM25 sparse, optional reranker | `schemas.py`, `ollama.py`, `sparse.py`, `reranker.py` |
| `vector/` | **only** Qdrant access point | `qdrant_store.py` |
| `llm/` | provider-agnostic gateway, prompts | `gateway.py`, `prompts/*.md` |
| `templates/` | Stage 1 anchor extraction, Stage 2 IR build + validation | `anchors_pptx.py`, `anchors_docx.py`, `anchors_xlsx.py`, `ir_builder.py`, `schemas.py` |
| `mapping/` | Stage 3 retrieval, extraction, verification, status resolution | `retriever.py`, `extractor.py`, `verifier.py`, `engine.py`, `schemas.py` |
| `writers/` | Stage 4 surgical writers + markers | `pptx_writer.py`, `docx_writer.py`, `xlsx_writer.py`, `markers.py`, `runs.py` (shared `w:`/`a:` paragraph and run rules) |
| `chat/` | Pipe chat orchestration: intent routing, Q&A, summaries, edits | `router.py`, `qa.py`, `summary.py`, `edits.py` |
| `jobs/` | Postgres queue worker + handlers per `jobs.kind` | `worker.py`, `handlers.py` |
| `api/v1/` | FastAPI routers, request/response models | `health.py`, `pipe.py`, `projects.py`, `documents.py`, `jobs.py`, `query.py`, `templates.py`, `mapping_runs.py`, `artifacts.py` |
| `main.py` | app factory, lifespan (DB pool, Qdrant client, worker start) | |

Outside the package: `integrations/openwebui/doc_intel_pipe.py` (the Pipe Function, pasted into Open WebUI) and `migrations/` (Alembic).

**Dependency direction.** `api` → `chat`/`jobs` → `mapping`/`templates`/`ingestion`/`writers` → `llm`/`embeddings`/`vector`/`storage`/`db` → `core`. Lower layers never import upper ones.

## 2. Ingestion

1. `POST …/documents` or `POST /v1/pipe/files`:
   - validate the extension against the format allow-list, check the magic bytes, enforce `MAX_UPLOAD_MB`;
   - compute `sha256` and deduplicate on `(project_id, sha256)`;
   - `StorageBackend.put_bytes(uploads/{document_id}/original.{ext})`;
   - insert `documents` with `status = 'uploaded'`;
   - enqueue `ingest_document`.
2. The worker sets `parsing`, then picks a parser by extension:

| Ext | Parser | `page_number` | `section_heading` |
|---|---|---|---|
| pdf | Docling (layout, TableFormer, RapidOCR) | PDF page (1-based) | nearest heading item |
| docx | Docling | null (DOCX has no pages) | nearest heading |
| pptx | Docling | slide number | slide title |
| xlsx | `openpyxl` read-only, `data_only=True` | null | sheet name |
| csv | stdlib `csv` | null | null |
| txt | plain text, split on blank lines | null | null |
| md | plain text, split on `#` headings | null | heading text |

3. Write the parser output to `parsed/{document_id}.json` with `overwrite=True`, so a retry after a crash can rewrite it.
   Headings (Docling section headers/titles, Markdown `#` lines) set `section_heading` **and** are emitted as text, because the layout model sometimes labels body text as a header.
4. **Chunker** (`chunker.py`):
   - text items under the same heading are packed up to `CHUNK_MAX_TOKENS=512` tokens (`CHUNK_TOKENIZER`) (target ~400) and never cross a section boundary;
   - each table is its own chunk (`element_type = 'table'`), serialized as Markdown; tables longer than 512 tokens are split by rows, with the header row repeated;
   - `chunk_index` is 0-based and contiguous per document.
5. Delete the document's existing points (a retry may yield fewer chunks). Then, 256 chunks at a time (`INDEX_BATCH` in `jobs/handlers.py`, so only one batch of vectors is in memory), embed (dense from Ollama `qwen3-embedding` on raw chunk text, 32 texts per request; sparse BM25 locally), upsert into `doc_chunks`, and set `progress = {"stage": "embedding", "done", "total"}`. Finally set `status = 'indexed'` with `page_count` and `chunk_count`. File reads and writes run in `asyncio.to_thread`, because the worker shares the API's event loop. A document deleted meanwhile has its points and parsed JSON removed, and the job ends without retrying.

Any failure sets `status = 'failed'` and `error_code` (e.g. `parse_error`, `ocr_error`, `embed_error`). No document text is logged.

## 3. Retrieval (shared by Q&A and mapping)

`Retriever(embedder, store, reranker | None).search(ctx: TenantContext, query: str, document_ids: list[UUID] | None = None, k=20)` (`mapping/retriever.py`):
1. Embed the query: dense from Ollama with Qwen3's retrieval instruction prefixed, sparse BM25 from the raw query words.
2. `qdrant_store.hybrid_search`: prefetch `dense` limit 40 and `sparse` limit 40, both with the tenant filter, fused with RRF, limit `k`.
3. If `RERANKER_ENABLED`, rerank to top 8; otherwise take the top 8 fused hits.
4. Return a `Hit(chunk_id, document_id, filename, page_number, section_heading, element_type, text, score)` per result.

`format_hit` (`mapping/retriever.py`) renders each hit for both Q&A and mapping; header fields are flattened to one line with `[`, `]`, `|` removed.

## 4. AnchorMap

Stage 1, deterministic, no LLM. Stored in `templates.anchor_map`.

```json
{
  "schema_version": "1.0",
  "format": "pptx",
  "anchors": [
    {
      "anchor_id": "pptx:s3:sp7",
      "kind": "shape_text",
      "path": ["Slide 3", "Situation Overview", "Content Placeholder 2"],
      "text": "Click to add situation summary",
      "tokens": [],
      "is_placeholder": true,
      "table": null
    },
    {
      "anchor_id": "pptx:s5:sp4:r1c2",
      "kind": "table_cell",
      "path": ["Slide 5", "Stakeholders", "Table 3"],
      "text": "",
      "tokens": [],
      "is_placeholder": true,
      "table": {"row": 1, "col": 2, "header": "Owner"}
    }
  ]
}
```

**`anchor_id` grammar.** Positional, stable for one immutable template file. Rows and columns are 0-based; slides and sections are 1-based.

| Format | Kinds | Pattern |
|---|---|---|
| pptx | `shape_text`, `table_cell` | `pptx:s{slide}:sp{shape_id}`, `pptx:s{slide}:sp{shape_id}:r{row}c{col}` |
| docx | `paragraph`, `table_cell`, `header_paragraph`, `footer_paragraph`, `content_control` | `docx:body:p{i}`, `docx:body:t{t}:r{row}c{col}`, `docx:sec{s}:header:p{i}`, `docx:sec{s}:footer:p{i}`, `docx:sdt:{tag}` |
| xlsx | `sheet_cell` | `xlsx:{sheet_name}!{cell_ref}` (split on the **last** `!`) |

**Selection rules.** Indexes are positions in the library's native collection, not counts of emitted anchors, so writers resolve them directly: `docx:body:p{i}` is `document.paragraphs[i]`, `t{t}` is `document.tables[t]`, and pptx uses `shape.shape_id`. Paragraph indexes are 0-based.
- pptx: every shape with a text frame (group shapes are recursed) and every non-spanned table cell. An empty placeholder carries its layout prompt text as `text`.
- docx: headings are not anchors (they form `path`). A body paragraph is an anchor if it has text, or is empty directly under a heading. Table cells are deduplicated across merges (one anchor at the merge origin). Header and footer paragraphs with text count only for sections not linked to the previous one (first-page and even-page headers are skipped). Tagged `w:sdt` content controls in the body are anchors; untagged ones are skipped, and the first of a duplicate tag is kept. An inline content control overlaps its paragraph's anchor. Content controls around tables, rows or cells are not anchors, and a table whose rows all sit in row-level content controls has no anchors (its index still counts).
- xlsx: candidates are non-formula cells holding a token, plus empty right and below neighbours of text label cells. Merged non-origin cells are never anchors.

`is_placeholder` means unfilled: tokens present, own text empty, or a content control.

`tokens` lists placeholder tokens found in the text: `{{…}}`, `[…]` and `<…>` (the last two start with a letter), `XXX` and `TBD` as whole words, and blanks of 4+ underscores (`____`). Each token is at most 100 characters inside its brackets.

**Limits.** `MAX_ANCHORS = 400`: more fails extraction with `validation_error`, never truncated; zero anchors also fails with `validation_error`. Before parsing, a package with more than 5000 zip parts, any part over 32 MB declared (whatever its name: `[Content_Types].xml` decides what is parsed as XML), or over 128 MB declared in total, fails with `validation_error`. An unreadable package fails with `parse_error`.

## 5. TemplateIR

Stage 2. The LLM proposes fields from the AnchorMap, and `ir_builder.validate()` enforces:
- every `anchor_id` exists in the AnchorMap;
- no `anchor_id` is used twice;
- `field_id`s are unique;
- `write_mode` is allowed for the anchor kind (table below);
- `replace_token` fields have `constraints.token` present in the anchor's `tokens`;
- `fill_table_rows` anchors on table row 1, col 0;
- a table filled by `fill_table_rows` has no other field on its cells;
- a `replace_text` or `fill_cell` field on a text anchor whose anchor has `tokens` uses `replace_token`, unless the anchor text is exactly one token (so the text around a token is never deleted). xlsx `sheet_cell` is exempt.

Invalid output gets 1 LLM retry, with the validation errors fed back. If it fails again, the `extract_template` job fails with `ir_invalid`. The fed-back errors name only field ids, anchor ids, write modes and anchor kinds, never document text.

| `write_mode` | Allowed anchor kinds | Behavior |
|---|---|---|
| `replace_text` | `shape_text`, `paragraph`, `header_paragraph`, `footer_paragraph`, `content_control`, `table_cell` | replace all text in the anchor |
| `replace_token` | any text kind (not `sheet_cell`) with non-empty `tokens` | replace `constraints.token` only |
| `fill_cell` | `table_cell`, `sheet_cell` | write one cell value (typed for xlsx) |
| `fill_table_rows` | `table_cell` at row 1, col 0 (the first body cell of a table) | write rows into **existing** empty body rows |

`sheet_cell` allows only `fill_cell`.

**JSON Schema** (`templates/schemas.py` mirrors it as Pydantic v2 models):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "TemplateIR",
  "type": "object",
  "required": ["schema_version", "template_id", "version", "format", "fields"],
  "additionalProperties": false,
  "properties": {
    "schema_version": {"const": "1.0"},
    "template_id": {"type": "string", "format": "uuid"},
    "version": {"type": "integer", "minimum": 1},
    "format": {"enum": ["pptx", "docx", "xlsx"]},
    "fields": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["field_id", "anchor_id", "section_path", "label", "description",
                     "expected_type", "required", "write_mode", "constraints"],
        "additionalProperties": false,
        "properties": {
          "field_id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$"},
          "anchor_id": {"type": "string"},
          "section_path": {"type": "array", "items": {"type": "string"}},
          "label": {"type": "string", "minLength": 1, "maxLength": 120},
          "description": {"type": "string", "maxLength": 500},
          "expected_type": {"enum": ["text", "paragraphs", "bullet_list", "number",
                                     "currency", "percent", "date", "table"]},
          "required": {"type": "boolean"},
          "write_mode": {"enum": ["replace_text", "replace_token", "fill_cell", "fill_table_rows"]},
          "constraints": {
            "type": "object",
            "additionalProperties": false,
            "properties": {
              "max_chars": {"type": "integer", "minimum": 1},
              "min_items": {"type": "integer", "minimum": 0},
              "max_items": {"type": "integer", "minimum": 1},
              "date_format": {"type": "string"},
              "unit": {"type": "string"},
              "allowed_values": {"type": "array", "items": {"type": "string"}},
              "token": {"type": "string"},
              "columns": {"type": "array", "items": {"type": "string"}}
            }
          }
        }
      }
    }
  }
}
```

Example:

```json
{
  "schema_version": "1.0",
  "template_id": "3f6c1a0e-8b1d-4b55-9a57-2f1c0d7e9b10",
  "version": 1,
  "format": "pptx",
  "fields": [
    {
      "field_id": "incident_summary",
      "anchor_id": "pptx:s3:sp7",
      "section_path": ["Situation Overview"],
      "label": "Incident summary",
      "description": "Two to three sentence summary of what happened, when, and where.",
      "expected_type": "paragraphs",
      "required": true,
      "write_mode": "replace_text",
      "constraints": {"max_chars": 600}
    },
    {
      "field_id": "affected_customers",
      "anchor_id": "pptx:s4:sp9",
      "section_path": ["Impact"],
      "label": "Affected customers",
      "description": "Number of customers affected by the incident.",
      "expected_type": "number",
      "required": true,
      "write_mode": "replace_token",
      "constraints": {"token": "{{affected_customers}}"}
    }
  ]
}
```

## 6. FieldMappingResult

Stage 3 output, one per field, stored in `mapping_runs.results[field_id]`.

**JSON Schema** (`mapping/schemas.py` mirrors it; the invariants in `allOf` are enforced by a Pydantic `model_validator`):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "FieldMappingResult",
  "type": "object",
  "required": ["field_id", "status", "value", "evidence", "confidence", "missing_reason",
               "checked_documents", "conflict_details", "provenance"],
  "additionalProperties": false,
  "$defs": {
    "Evidence": {
      "type": "object",
      "required": ["document_id", "chunk_id", "filename", "page_number", "section_heading", "quote"],
      "additionalProperties": false,
      "properties": {
        "document_id": {"type": "string", "format": "uuid"},
        "chunk_id": {"type": "string", "format": "uuid"},
        "filename": {"type": "string"},
        "page_number": {"type": ["integer", "null"]},
        "section_heading": {"type": ["string", "null"]},
        "quote": {"type": "string", "minLength": 8, "maxLength": 1000}
      }
    },
    "Value": {"type": ["string", "number", "array", "null"]}
  },
  "properties": {
    "field_id": {"type": "string"},
    "status": {"enum": ["populated", "partial", "missing", "conflict"]},
    "value": {"$ref": "#/$defs/Value"},
    "evidence": {"type": "array", "items": {"$ref": "#/$defs/Evidence"}},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "missing_reason": {
      "enum": [null, "no_relevant_chunks", "not_found_by_llm", "evidence_failed_verification",
               "insufficient_evidence", "llm_error", "cleared_by_user"]
    },
    "checked_documents": {"type": "array", "items": {"type": "string", "format": "uuid"}},
    "conflict_details": {
      "type": ["object", "null"],
      "required": ["candidates"],
      "properties": {
        "candidates": {
          "type": "array",
          "minItems": 2,
          "items": {
            "type": "object",
            "required": ["value", "evidence"],
            "properties": {
              "value": {"$ref": "#/$defs/Value"},
              "evidence": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/Evidence"}}
            }
          }
        }
      }
    },
    "provenance": {"enum": ["rag", "user"]}
  },
  "allOf": [
    {"if": {"properties": {"status": {"const": "populated"}, "provenance": {"const": "rag"}}},
     "then": {"properties": {"evidence": {"minItems": 1}, "value": {"not": {"type": "null"}}}}},
    {"if": {"properties": {"status": {"const": "partial"}}},
     "then": {"properties": {"evidence": {"minItems": 1}, "value": {"not": {"type": "null"}}}}},
    {"if": {"properties": {"status": {"const": "missing"}}},
     "then": {"properties": {"value": {"type": "null"}, "evidence": {"maxItems": 0},
                             "missing_reason": {"type": "string"}}}},
    {"if": {"properties": {"status": {"const": "conflict"}}},
     "then": {"properties": {"value": {"type": "null"}, "conflict_details": {"type": "object"}}}}
  ]
}
```

Example (conflict):

```json
{
  "field_id": "affected_customers",
  "status": "conflict",
  "value": null,
  "evidence": [],
  "confidence": 0.0,
  "missing_reason": null,
  "checked_documents": ["a1f0…", "b2e1…"],
  "conflict_details": {
    "candidates": [
      {"value": 12000, "evidence": [{"document_id": "a1f0…", "chunk_id": "c9…", "filename": "ops_report.pdf",
        "page_number": 4, "section_heading": "Impact", "quote": "approximately 12,000 customers were affected"}]},
      {"value": 15400, "evidence": [{"document_id": "b2e1…", "chunk_id": "d7…", "filename": "press_release.docx",
        "page_number": null, "section_heading": "Statement", "quote": "15,400 customers experienced disruption"}]}
    ]
  },
  "provenance": "rag"
}
```

**`confidence`** is a deterministic tier for display only; it never drives `status`:
- `populated` + typed value matched literally in a quote → `0.9`
- `populated` text, list or table whose every item (cell) appears in a verified quote → `0.8`
- `partial` → `0.5`
- `missing` / `conflict` → `0.0`
- `provenance = user` → `1.0`

Engine results always have `provenance = rag`.

`shortcut: heuristic confidence tiers, calibrate against the eval set in Phase 10.`

## 7. Mapping engine (Stage 3)

### Run state machine (`mapping_runs.status`)

```
queued ─▶ retrieving ─▶ extracting ─▶ verifying ─▶ rendering ─▶ completed
   │           │             │             │            │
   └───────────┴─────────────┴─────────────┴────────────┴──▶ failed
```

`run_mapping` sets `extracting` when the field loop starts, then commits `results`, `summary` and `verifying` in one transaction. It then renders: it sets `rendering` (commit), writes the file, and commits the `artifacts` row together with `completed`, `finished_at` and the writer warnings appended to `summary.warnings`. `retrieving` is not used (each field is retrieved, extracted and verified in turn). A retried job whose run is `verifying`/`rendering` only renders; a `completed` run returns its artifact. `render_artifact` (payload `{mapping_run_id}`) runs the same render for a `verifying`/`rendering` run (Phase 9 edits enqueue it). Failure hooks: `run_mapping` marks any run that is not `completed`/`failed` as `failed`; `render_artifact` only fails `verifying`/`rendering` runs. `finished_at` is set at `completed` or `failed`.

A retried job recomputes every field; a run that is no longer `queued`/`retrieving`/`extracting` is returned unchanged. When the job fails for good, the `run_mapping` failure hook marks the run `failed`.

`failed` is reserved for infrastructure errors (DB, Qdrant, storage, writer). A field-level LLM failure (invalid output after the corrective retry) does **not** fail the run: the field becomes `missing(llm_error)`. An unreachable LLM (connection error, timeout, 5xx) is an infrastructure error: the job is retried and the run ends `failed`.

### Per-field pipeline

```
PENDING ─retrieve─▶ RETRIEVED ─extract─▶ EXTRACTED ─verify─▶ RESOLVED(populated|partial|missing|conflict)
   │ 0 hits              │ not_found / llm_error
   ▼                     ▼
 missing(no_relevant_chunks)   missing(not_found_by_llm | llm_error)
```

1. **Retrieve.**
   - Query = `label + " — " + description + " (" + " > ".join(section_path) + ")"`.
   - `retriever.search(ctx, query, document_ids=run.document_ids)`.
   - `checked_documents` = the distinct `document_id`s in `run.document_ids`, i.e. everything that was searched, not just the documents with hits.
   - Up to 6 hits (`MAX_FIELD_HITS`) are shown to the LLM, and only those can be cited.
   - The query and the prompt use a bounded copy of the field: the last 4 `section_path` elements (120 chars each), at most 30 `columns`/`allowed_values` (80 chars each).
2. **Extract.** The LLM gets the field spec and the hits (each labeled with its `chunk_id`), with JSON-schema output:
   `{"candidates": [{"value": …, "quotes": [{"chunk_id": "…", "quote": "…"}]}], "not_found": bool, "incomplete": bool}`.
   The prompt says to copy quotes verbatim, use only the given chunks, and return `not_found` rather than guess.
   The LLM `value` is a string for text/number/currency/percent/date, a list of strings for paragraphs/bullet_list, and a list of rows (lists of cell strings, body rows only, in `constraints.columns` order) for table. `not_found = true` wins over any candidates.
3. **Verify** (`verifier.py`, deterministic).
   - `normalize(s)` = NFKC → casefold → map curly quotes and dashes to ASCII → replace `|` with a space (so a quoted Markdown table row matches its chunk) → collapse whitespace → strip.
   - A quote is verified iff `chunk_id ∈ hits` **and** `normalize(quote) in normalize(hit.text)` **and** `len(normalize(quote)) >= 8`.
   - Quotes shorter than 8 or longer than 1,000 raw characters are dropped. The stored quote is the LLM quote verbatim.
   - Typed values (`number`, `currency`, `percent`, `date`): the value is normalized to a canonical form (`12,000` → `12000`; dates → ISO) and must appear, canonicalized, in at least one verified quote of that candidate.
   - Typed canonical form: the value must state exactly one number (sign kept, thousand/million/billion applied, currency symbols and `%` ignored, digit groups of 3 after a comma) or one date (ISO). It must appear among the numbers/dates of a verified quote.
   - Stored values: text → string; paragraphs/bullet_list → list of strings (bullet markers stripped); number/currency/percent → JSON number; date → ISO string; table → list of rows.
   - Evidence `filename`, `page_number`, and `section_heading` are copied from the **hit payload**, never from LLM output.
4. **Resolve status.**
   1. Drop unverified quotes, then drop candidates with no verified quote.
   2. Merge candidates whose canonical values are equal (union of evidence). Text merges on `normalize()`; lists on their normalized items.
   3. 0 candidates → `missing`. The reason is `evidence_failed_verification` if the LLM proposed something, otherwise `not_found_by_llm`.
   4. ≥ 2 candidates → `conflict` (`value = null`, `conflict_details.candidates`). **Never auto-resolve.**
   5. 1 candidate → `populated`. It becomes `partial` (`missing_reason = "insufficient_evidence"`) when:
      - the LLM flagged `incomplete`, or
      - `min_items` is not met, or
      - a text, list or table value has an item (cell) that appears in no verified quote of that candidate (synthesized or paraphrased text is never `populated`), or
      - a `table` row has fewer than `len(columns)` non-empty cells.
   6. A value longer than `max_chars` is still written; a warning (`{"field_id", "code": "max_chars_exceeded"}`) goes into `mapping_runs.summary.warnings`. Values are never silently truncated.

Fields run sequentially (`JOB_CONCURRENCY=1`). `jobs.progress` is updated after each field. Progress is `{"stage": "mapping", "done": i, "total": n}`. Stored results are a historical snapshot: deleting a source later does not change them.

## 8. Surgical writers (Stage 4)

Common rules:
- Copy the template `original.{ext}` into memory, mutate it, and write it as a new `artifacts/{artifact_id}.{ext}`. The upload is never changed.
- `artifact_id = uuid5(fixed namespace, run_id)`, so a retry targets the same `artifacts/{artifact_id}.{ext}`. The file is written with `put_bytes(overwrite=False)` (atomic); `FileExistsError` means an earlier attempt crashed after writing, and that file is reused. The row, `completed` and `finished_at` commit in one transaction. If the project was deleted meanwhile, the file is removed and the job finishes. Writer errors (unresolvable anchor, bad content) are not retried.
- download `filename` = `{template stem}_v{version}.{ext}`; stem characters outside `[A-Za-z0-9._-]` become `_`, cut to 100, fallback `artifact` (`shortcut: non-ASCII names become "_"; add RFC 5987 filename* when users need them.`).
- every target is resolved before the first write, so cloned paragraphs never shift later anchors.
- markers: `{label}` is replaced literally (never `str.format`); XML-illegal characters are stripped from every value.
- Only anchors named in the IR are touched. All other package parts must be **C14N-equal** to the template, and are byte-equal for XLSX.
- What gets written per status:

| Status | Written |
|---|---|
| `populated`, `partial` | `value` |
| `missing` | `MISSING_MARKER` (default `[MISSING: {label}]`) |
| `conflict` | `CONFLICT_MARKER` (default `[CONFLICT: See Chat Summary]`) |

- Value rendering by `expected_type`:
  - `paragraphs` / `bullet_list`: one paragraph per item;
  - `number` / `currency` / `percent` / `date`: formatted with `constraints.unit` / `date_format` for text targets, typed for XLSX.
  - `text` targets: `number`/`currency` → `"{v} {unit}"` (or `"{v}"`), `percent` → `"{v}%"`, `date` → ISO `YYYY-MM-DD` (`shortcut: date_format is free text from the LLM, not a strftime pattern; upgrade when the IR emits one.`);
  - lists into a single run/cell (`replace_token`, xlsx) are joined with `"; "`;
  - a `table` value into a non-`fill_table_rows` anchor is one paragraph per row, cells joined by `" | "`;
  - XLSX: `percent` is written as `v / 100`; a `date` is an Excel serial only when the target cell's style has a date number format (built-in 14-22, 45-47, or a custom format with d/m/y), otherwise ISO text as `inlineStr` (`shortcut: 1900 date system only`);
  - `max_items` is not enforced (values are never truncated).
- Write modes:
  - `fill_table_rows` writes value rows into the existing body rows from row 1, one value per unique cell (merged docx cells and spanned pptx cells are written once);
  - rows or cells that do not fit are not written and the field gets one `table_overflow` warning (status unchanged);
  - a missing/conflict marker goes into cell r1c0.
  - Warnings go to `mapping_runs.summary.warnings` as `{"field_id", "code"}`.

### PPTX (`python-pptx` + `lxml`)

- `replace_text`:
  - keep the first paragraph's `a:pPr` and its first run's `a:rPr`;
  - put the value in that run and delete the other runs;
  - extra items get a cloned paragraph (same `pPr`/`rPr`), and leftover original paragraphs are removed.
- `replace_token`: first merge runs that the token spans (keeping the first run's `rPr`), then replace the token text in place.
- Table cells work the same way inside `a:tc/a:txBody`.
- Shapes, layouts, masters, notes, and media are never touched. No autofit changes. Text overflow is reported only through `max_chars_exceeded` (no font metrics).

### DOCX (`python-docx` + `lxml`)

- Same run/paragraph rules on `w:p`/`w:r`/`w:rPr`.
- Header and footer anchors are resolved through `section.header` / `section.footer`.
- `content_control` writes into the `w:sdtContent` of `w:sdt` with a matching `w:tag`, and removes `w:showingPlcHdr`.
- Numbering (`w:numPr`) is kept on cloned paragraphs, so bullet lists stay bullets.
- a paragraph `replace_text` keeps an inline content control that is itself a target of another field;
- filling a placeholder content control also drops `w:rStyle="PlaceholderText"`;
- cloned paragraphs drop `w:sectPr`.

### XLSX (zip-level XML patch, `lxml`)

`openpyxl` is used only to read anchors (ADR-006). The writer:
- Opens the zip and locates each worksheet XML via `workbook.xml` + `workbook.xml.rels`.
- For each target cell `<c r="B4">`, keeps the `s` (style) attribute and writes:
  - strings: `t="inlineStr"` + `<is><t>…</t></is>`; `sharedStrings.xml` is untouched;
  - numbers and currency: no `t` attribute, `<v>` with the canonical number; percent: `<v>` is `v / 100`;
  - dates: an Excel serial in `<v>` only for date-formatted cells, otherwise ISO text as `inlineStr` (see Value rendering).
- Creates the `<c>` element in column order if the cell doesn't exist.
- Never writes into a cell with an `<f>` formula; the field gets a `formula_cell` warning.
- Sets `<calcPr fullCalcOnLoad="1"/>` in `workbook.xml` so dependent formulas recalculate.
- Copies every other zip entry byte-for-byte.

### Known limits

`shortcut: no slide/shape/table-row/sheet-row insertion — fill_table_rows writes only into existing empty rows and adds a table_overflow warning when rows run out; upgrade when templates need dynamic row counts.`

`shortcut: touched runs keep only their text (tabs, breaks, fields inside a rewritten run are dropped); upgrade when templates carry them in anchored text.`

`shortcut: a repeated content-control tag writes the first control only; upgrade when templates reuse tags.`

`shortcut: xlsx rows/cells without an r attribute are not supported; upgrade when a template needs them.`

`shortcut: a custom number format counts as a date if it has d/m/y outside quotes and brackets; upgrade when a template misclassifies one.`

`shortcut: a final transaction that fails on every retry leaves the written artifact file unreferenced (run failed); upgrade with a sweep of unreferenced artifact files.`

## 9. Chat orchestration (`chat/`)

`POST /v1/pipe/chat`:
1. Resolve or create the project for `(user_id, openwebui_chat_id)`.
2. Map each `files[].openwebui_file_id` to a document via `openwebui_file_ids`. The Pipe uploads before chatting, so an unknown id means that upload failed: reply with an error `delta` naming the file.
3. If any referenced document isn't `indexed`, wait on its `ingest_document` job and stream progress as `status` events.
4. **Intent routing** (`router.py`): one LLM call with JSON output `{"intent": "qa"|"map_template"|"edit"|"chitchat", "template_document_id": uuid|null, "candidates": [uuid], "edit_ops": [...]}`. It sees the last 10 messages, each cut to its **last 1,500 characters**, so long run summaries can't push the prompt past `LLM_NUM_CTX` (Ollama silently drops the start of an over-long prompt, which is where the instructions are). It also sees the project's document list (ids, filenames, roles). No document text goes into this call.
   - `qa` → `qa.py`.
   - `map_template` with exactly one candidate:
     1. set `role = 'template'` and delete that document's points;
     2. run `extract_template` (it reuses the existing `templates` row and latest IR if this template was mapped before), then create a `mapping_runs` row (`trigger = 'initial'`, `document_ids` = all `indexed` sources) and run `run_mapping`;
     3. stream progress and finish with `summary.py`.
   - `map_template` with zero or several candidates → reply with a numbered list of template-capable documents (pptx/docx/xlsx) and ask the user to confirm.
     `shortcut: stateless confirmation via chat history, add projects.pending_action if routing proves unreliable.`
   - `edit` → validate the ops against the latest run's IR fields, then `edits.py` (same path as `POST …/edits`).
5. **Q&A** (`qa.py`):
   1. Retrieve. No hits → the "not found" answer below, without an LLM call. A search failure → `503 upstream_unavailable`.
   2. Make one LLM call (prompt `llm/prompts/qa.md`) with JSON output `{"answer_markdown": "… [1] …", "citations": [{"marker": 1, "chunk_id": "…", "quote": "…"}]}`. Each hit is shown as `[chunk_id | filename | p.N | section]` followed by its text.
   3. Verify the citations (`mapping/verifier.py`; metadata copied from the hit). Remove unverified markers. If no verified citations remain, the answer is "I could not find this in the uploaded documents."
      `shortcut: a sentence whose only marker failed verification stays uncited, drop such sentences if the eval shows unsupported claims surviving.`
   4. Stream the **verified** answer as `delta` events, followed by a source list (`filename p.N`).
   Generation isn't token-streamed, because the answer has to be verified before the user sees it.
6. **Summary** (`summary.py`):
   - counts by status;
   - every `missing` field (label + reason);
   - every `conflict` (each candidate value + citations);
   - `partial` fields and warnings;
   - the signed download link.

## 10. Conversational edits (`chat/edits.py`)

`EditOp` (schema in [api-specification.md](api-specification.md#post-v1projectsproject_idmapping-runsrun_idedits)):

| `op` | Effect on the copied `FieldMappingResult` |
|---|---|
| `set_value` | `status=populated`, `value=<user value>`, `evidence=[]`, `provenance=user`, `confidence=1.0` |
| `choose_candidate` | only valid on `conflict`: `status=populated`, `value`/`evidence` from `conflict_details.candidates[i]`, `provenance=rag`, `conflict_details=null` |
| `clear` | `status=missing`, `value=null`, `evidence=[]`, `missing_reason=cleared_by_user` |

Applying an edit:
1. Copy the parent run's `results`.
2. Apply the ops.
3. Insert a new `mapping_runs` row: `trigger='edit'`, `parent_run_id`, `version = max(version for this template_ir_id) + 1` (retried once on a `UNIQUE` violation), `edits`, `status='rendering'`.
4. Enqueue `render_artifact`.

The parent run is never modified.
