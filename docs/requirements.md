# Requirements

A local-first, domain-agnostic, evidence-grounded **Document Intelligence & Template Mapping Platform**. Users upload source documents and a target template in an Open WebUI chat. The system answers questions with verified citations and fills the template in place, marking anything it can't prove.

Names: [data-model.md](data-model.md), [api-specification.md](api-specification.md).

## 1. Functional requirements

| ID | Requirement |
|---|---|
| FR-1 | Users interact only through Open WebUI chat. A thin Pipe Function forwards `user_id`, `chat_id`, `messages`, and file references to the backend and shows live status via `__event_emitter__`. |
| FR-2 | Internal Open WebUI task calls (`### Task:` title/tag/follow-up generation) are ignored by the Pipe. |
| FR-3 | Each chat maps to one project (`projects.openwebui_chat_id`). All documents, runs, and artifacts are scoped to that project. |
| FR-4 | Uploaded files are stored immutably, parsed, structurally chunked, embedded (Ollama `qwen3-embedding` dense + BM25 sparse), and indexed in Qdrant with `document_id`, `filename`, `page_number`, `section_heading`, `element_type`, `chunk_index`. |
| FR-5 | Re-uploading identical content in a project is deduplicated (`sha256`). |
| FR-6 | Users ask questions in chat. Answers cite `filename` + `page_number` with verbatim quotes. Any citation that fails verification is removed. With no verified citation, the answer is "not found". |
| FR-7 | The user names the target template in natural language. If zero or several documents match, the backend lists the candidates and asks for confirmation before doing anything. |
| FR-8 | Template extraction produces a deterministic Anchor Map, then an LLM-assisted, validated `TemplateIR` (versioned). |
| FR-9 | Mapping produces one `FieldMappingResult` per IR field with `status` ∈ `populated` \| `partial` \| `missing` \| `conflict`, evidence, `checked_documents`, and a reason for anything not populated. |
| FR-10 | `populated`/`partial` require at least 1 deterministically verified quote (unless the user set the value). Conflicting verified values are reported as `conflict` and **never auto-resolved**. |
| FR-11 | The filled output is a clone of the template, edited only at mapped anchors. Styles, layouts, masters, tables, headers/footers, and untouched content are preserved. |
| FR-12 | Missing and conflict fields are written as configurable markers. Defaults: `[MISSING: {label}]` and `[CONFLICT: See Chat Summary]`. |
| FR-13 | After each run, the chat shows a summary: status counts, missing fields with reasons, conflicts with each candidate and its citations, warnings, and a download link. |
| FR-14 | Users can edit results in chat (set a value, choose a conflict candidate, clear a field). Each edit creates a new versioned run and artifact; earlier versions are kept. |
| FR-15 | Artifacts download via signed, user-scoped, expiring URLs (default 15 min). |
| FR-16 | Users can delete a document; its vectors and stored files are removed. |
| FR-17 | The LLM provider is swappable via `.env` (`ollama` default, `openai_compat` = a local OpenAI-compatible server such as `mlx_lm.server`, LM Studio or vLLM) with no code change. `anthropic` is planned, not built (ADR-005). |
| FR-18 | Operators register reusable PPTX/DOCX/XLSX library templates with the CLI or over HTTP (`/v1/library`, ADR-014); registration extracts the IR once. With no suitable template in the chat, asking for a presentation, document or spreadsheet offers the library as a numbered list (name, format, description). The user picks by number or name, or the only template of the requested format runs directly. Chat users only read library templates; the copy, runs and artifacts belong to the user's project (ADR-012). |

## 2. Supported formats

| Format | As source | As template (in-place output) |
|---|---|---|
| PDF (incl. scanned, via OCR) | ✅ Docling + RapidOCR | ❌ source-only (non-fillable PDFs can't be edited in place) |
| DOCX | ✅ Docling | ✅ |
| PPTX | ✅ Docling | ✅ |
| XLSX | ✅ `openpyxl` | ✅ (zip-level XML patch) |
| TXT, MD | ✅ plain text | ❌ |
| CSV | ✅ stdlib `csv` | ❌ |

Max upload: `MAX_UPLOAD_MB=100`. Legacy binary formats (`.doc`, `.ppt`, `.xls`) are rejected with `415`.

## 3. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-1 | **Local-first.** With default config no document content leaves the machine. Cloud LLM providers must be set explicitly in `.env`, and `/v1/health` reports the active provider. |
| NFR-2 | Runs on a MacBook Pro M5 Pro with 24 GB unified memory within the budget in [architecture.md § 6](architecture.md#6-memory-budget-24-gb-unified-memory). |
| NFR-3 | Performance targets (measured in Phase 12, M5 Pro, 14B model):<br>• ingest a 50-page text PDF ≤ 3 min<br>• Q&A answer ≤ 20 s<br>• map a 30-field template over 5 sources ≤ 10 min |
| NFR-4 | Jobs survive a backend restart: all `running` jobs are re-queued at startup (up to 3 attempts), and retries are idempotent. |
| NFR-5 | Uploads and artifacts are immutable. Every run and IR version is reproducible from stored rows and files. |
| NFR-6 | Logs are structured JSON and never contain document text, chunk text, prompts, completions, filenames, or secrets. |
| NFR-7 | Python 3.12, `uv`, `ruff` clean, test coverage ≥ 80% on `src/doc_intel`. |

## 4. Security & isolation rules

1. Every PostgreSQL query filters on `user_id` **and** `project_id`. Child tables use a composite FK to `projects(id, user_id)`.
2. Every Qdrant operation filters on `user_id` **and** `project_id`, and goes only through `src/doc_intel/vector/qdrant_store.py`, which requires a `TenantContext`.
3. A cross-tenant id returns `404`, the same as a non-existent id.
4. The backend binds `127.0.0.1` only. All non-public routes require the shared bearer secret, compared in constant time.
5. `user_id` is validated against `^[A-Za-z0-9_-]{1,64}$`. Storage paths are built only from validated ids, never from filenames.
6. Uploads are checked by extension allow-list + magic bytes + size limit before storing.
7. Signed download URLs bind `artifact_id`, owner `user_id`, and expiry with HMAC-SHA256.
8. Secrets live only in `.env` (git-ignored). `.env.example` contains no real values.
9. Citations are verified deterministically. The system never presents an unverified quote as evidence.
10. Template packages are checked for part count, per-part size (32 MB) and total size (128 MB) before parsing. XML is parsed without entity resolution (python-docx/python-pptx) or through defusedxml (openpyxl).

## 5. Out of scope (v1)

- Non-fillable PDF output, or converting PDFs into fillable forms.
- Adding slides, shapes, table rows, or sheet rows to templates (`fill_table_rows` uses existing rows only). Exception since Phase 18: a pptx chart placeholder is replaced by one native chart (`insert_chart`).
- Generating images; charts outside pptx (docx/xlsx charts are left untouched); XY scatter and bubble charts. Since Phase 18, pptx category charts are filled (`fill_chart`) or inserted (`insert_chart`) from verified data points only (system-design § 7-8).
- Multi-user collaboration on one project; sharing projects between users.
- Network-exposed or multi-host deployment, SSO beyond Open WebUI login, per-user API tokens.
- Handwriting OCR, audio/video sources, legacy `.doc/.ppt/.xls`.
- Model fine-tuning; automatic conflict resolution of any kind.

## 6. Assumptions

- Design assumptions and their trade-offs are recorded as ADRs in [DECISIONS.md](DECISIONS.md).
- The repository name refers to the first use case (crisis-management decks). The platform itself is domain-agnostic.
