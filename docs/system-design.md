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
| `writers/` | Stage 4 surgical writers + markers | `pptx_writer.py`, `docx_writer.py`, `xlsx_writer.py`, `markers.py` |
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
5. Embed in batches of 32: dense from Ollama `qwen3-embedding` (raw chunk text), sparse BM25 locally. Then delete the document's existing points (a retry may yield fewer chunks), upsert into `doc_chunks`, and set `status = 'indexed'` with `page_count` and `chunk_count`. A document deleted meanwhile has its points and parsed JSON removed, and the job ends without retrying.

Any failure sets `status = 'failed'` and `error_code` (e.g. `parse_error`, `ocr_error`, `embed_error`). No document text is logged.

## 3. Retrieval (shared by Q&A and mapping)

`Retriever(embedder, store, reranker | None).search(ctx: TenantContext, query: str, document_ids: list[UUID] | None = None, k=20)` (`mapping/retriever.py`):
1. Embed the query: dense from Ollama with Qwen3's retrieval instruction prefixed, sparse BM25 from the raw query words.
2. `qdrant_store.hybrid_search`: prefetch `dense` limit 40 and `sparse` limit 40, both with the tenant filter, fused with RRF, limit `k`.
3. If `RERANKER_ENABLED`, rerank to top 8; otherwise take the top 8 fused hits.
4. Return a `Hit(chunk_id, document_id, filename, page_number, section_heading, element_type, text, score)` per result.

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
      "is_placeholder": false,
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

`tokens` lists placeholder tokens found in the text: `{{…}}`, `[…]`, `<…>`, `XXX`/`TBD`. XLSX anchors include only cells that are empty, hold a token, or sit next to a label cell. Formula cells are never anchors.

## 5. TemplateIR

Stage 2. The LLM proposes fields from the AnchorMap, and `ir_builder.validate()` enforces:
- every `anchor_id` exists in the AnchorMap;
- no `anchor_id` is used twice;
- `field_id`s are unique;
- `write_mode` is allowed for the anchor kind (table below);
- `replace_token` fields have `constraints.token` present in the anchor's `tokens`.

Invalid output gets 1 LLM retry, with the validation errors fed back. If it fails again, the `extract_template` job fails with `ir_invalid`.

| `write_mode` | Allowed anchor kinds | Behavior |
|---|---|---|
| `replace_text` | `shape_text`, `paragraph`, `header_paragraph`, `footer_paragraph`, `content_control`, `table_cell` | replace all text in the anchor |
| `replace_token` | any text kind with non-empty `tokens` | replace `constraints.token` only |
| `fill_cell` | `table_cell`, `sheet_cell` | write one cell value (typed for xlsx) |
| `fill_table_rows` | `table_cell` (the first body cell of a table) | write rows into **existing** empty body rows |

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
- `populated` text synthesized from verified quotes → `0.8`
- `partial` → `0.5`
- `missing` / `conflict` → `0.0`
- `provenance = user` → `1.0`

`shortcut: heuristic confidence tiers, calibrate against the eval set in Phase 10.`

## 7. Mapping engine (Stage 3)

### Run state machine (`mapping_runs.status`)

```
queued ─▶ retrieving ─▶ extracting ─▶ verifying ─▶ rendering ─▶ completed
   │           │             │             │            │
   └───────────┴─────────────┴─────────────┴────────────┴──▶ failed
```

`failed` is reserved for infrastructure errors (DB, Qdrant, storage, writer). A field-level LLM failure does **not** fail the run; that field becomes `missing` with `missing_reason = "llm_error"`.

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
2. **Extract.** The LLM gets the field spec and the hits (each labeled with its `chunk_id`), with JSON-schema output:
   `{"candidates": [{"value": …, "quotes": [{"chunk_id": "…", "quote": "…"}]}], "not_found": bool, "incomplete": bool}`.
   The prompt says to copy quotes verbatim, use only the given chunks, and return `not_found` rather than guess.
3. **Verify** (`verifier.py`, deterministic).
   - `normalize(s)` = NFKC → casefold → map curly quotes and dashes to ASCII → replace `|` with a space (so a quoted Markdown table row matches its chunk) → collapse whitespace → strip.
   - A quote is verified iff `chunk_id ∈ hits` **and** `normalize(quote) in normalize(hit.text)` **and** `len(normalize(quote)) >= 8`.
   - Typed values (`number`, `currency`, `percent`, `date`): the value is normalized to a canonical form (`12,000` → `12000`; dates → ISO) and must appear, canonicalized, in at least one verified quote of that candidate.
   - Evidence `filename`, `page_number`, and `section_heading` are copied from the **hit payload**, never from LLM output.
4. **Resolve status.**
   1. Drop unverified quotes, then drop candidates with no verified quote.
   2. Merge candidates whose canonical values are equal (union of evidence).
   3. 0 candidates → `missing`. The reason is `evidence_failed_verification` if the LLM proposed something, otherwise `not_found_by_llm`.
   4. ≥ 2 candidates → `conflict` (`value = null`, `conflict_details.candidates`). **Never auto-resolve.**
   5. 1 candidate → `populated`. It becomes `partial` (`missing_reason = "insufficient_evidence"`) when:
      - the LLM flagged `incomplete`, or
      - `min_items` is not met, or
      - `columns` are not all filled for `table`.
   6. A value longer than `max_chars` is still written; a warning goes into `mapping_runs.summary.warnings`. Values are never silently truncated.

Fields run sequentially (`JOB_CONCURRENCY=1`). `jobs.progress` is updated after each field.

## 8. Surgical writers (Stage 4)

Common rules:
- Copy the template `original.{ext}` into memory, mutate it, and write it as a new `artifacts/{artifact_id}.{ext}`. The upload is never changed.
- Idempotent on retry: if an `artifacts` row already exists for the run and its file is on disk, reuse it. If the row exists but the file is missing, write the file under that row's `storage_key`.
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

### PPTX (`python-pptx` + `lxml`)

- `replace_text`:
  - keep the first paragraph's `a:pPr` and its first run's `a:rPr`;
  - put the value in that run and delete the other runs;
  - extra items get a cloned paragraph (same `pPr`/`rPr`), and leftover original paragraphs are removed.
- `replace_token`: first merge runs that the token spans (keeping the first run's `rPr`), then replace the token text in place.
- Table cells work the same way inside `a:tc/a:txBody`.
- Shapes, layouts, masters, notes, and media are never touched. No autofit changes. Overflow becomes a warning.

### DOCX (`python-docx` + `lxml`)

- Same run/paragraph rules on `w:p`/`w:r`/`w:rPr`.
- Header and footer anchors are resolved through `section.header` / `section.footer`.
- `content_control` writes into the `w:sdtContent` of `w:sdt` with a matching `w:tag`, and removes `w:showingPlcHdr`.
- Numbering (`w:numPr`) is kept on cloned paragraphs, so bullet lists stay bullets.

### XLSX (zip-level XML patch, `lxml`)

`openpyxl` is used only to read anchors (ADR-006). The writer:
- Opens the zip and locates each worksheet XML via `workbook.xml` + `workbook.xml.rels`.
- For each target cell `<c r="B4">`, keeps the `s` (style) attribute and writes:
  - strings: `t="inlineStr"` + `<is><t>…</t></is>`; `sharedStrings.xml` is untouched;
  - numbers, currency, and percent: no `t` attribute, `<v>` with the canonical number;
  - dates: an Excel serial number in `<v>`.
- Creates the `<c>` element in column order if the cell doesn't exist.
- Never writes into a cell with an `<f>` formula; that field becomes a warning.
- Sets `<calcPr fullCalcOnLoad="1"/>` in `workbook.xml` so dependent formulas recalculate.
- Copies every other zip entry byte-for-byte.

### Known limits

`shortcut: no slide/shape/table-row/sheet-row insertion — fill_table_rows writes only into existing empty rows and marks the field partial when rows run out; upgrade when templates need dynamic row counts.`

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
   1. Retrieve.
   2. Make one LLM call with JSON output `{"answer_markdown": "… [1] …", "citations": [{"marker": 1, "chunk_id": "…", "quote": "…"}]}`.
   3. Verify the citations. Remove unverified markers. If no verified citations remain, the answer is "I could not find this in the uploaded documents."
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
