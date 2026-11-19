# Architecture Decision Records

Status values: `Accepted`, `Superseded by ADR-NNN`. Add new ADRs at the bottom. Never rewrite an accepted ADR: supersede it.

Names used below (tables, endpoints, paths) are defined in [data-model.md](data-model.md) and [api-specification.md](api-specification.md).

---

## ADR-001 — Hybrid execution topology on Apple Silicon

**Status:** Accepted

**Context.** Target machine: MacBook Pro M5 Pro, 24 GB unified memory, macOS. ML workloads (Ollama, BGE-M3, reranker, Docling) need Metal/MPS. Docker on macOS runs a Linux VM with no GPU access.

**Decision.**
- Docker Compose runs the stateful, non-ML services: **Open WebUI**, **PostgreSQL 16**, **Qdrant**.
- Native macOS runs everything that needs acceleration: the **FastAPI backend** (`uv`, Python 3.12) with Docling + RapidOCR, BGE-M3, and the optional BGE reranker in-process, plus **Ollama** (Metal).
- Containers reach native services through `host.docker.internal`. Host ports for Postgres and Qdrant bind to `127.0.0.1` only.

**Trade-offs.** Two runtimes to manage (Compose + `uv run`). In return, ML runs 5–10× faster than CPU-in-VM, and the Docker VM can stay small (≤ 3 GB).

---

## ADR-002 — Open WebUI as the UI via a thin Pipe Function

**Status:** Accepted

**Context.** We need a chat UI with auth, chat history, and file upload, without building one. Open WebUI's built-in RAG would compete with ours and cannot enforce our citation rules.

**Decision.**
- Use one **Open WebUI Pipe Function** (`integrations/openwebui/doc_intel_pipe.py`) as the only bridge. Its job:
  1. Ignore internal task calls: return early if `__metadata__["task"]` is set or the last user message starts with `### Task:` (title/tag/follow-up generation).
  2. Upload any new chat files to `POST /v1/pipe/files`.
  3. Forward `user_id`, `chat_id`, `messages`, and file references to `POST /v1/pipe/chat`.
  4. Relay backend SSE `status` events to `__event_emitter__` and yield `delta` text.
- Turn off Open WebUI's built-in RAG for these files by setting the Pipe model's capabilities `file_context: false` and `builtin_tools: false`. `BYPASS_EMBEDDING_AND_RETRIEVAL=true` only skips embedding: Open WebUI still injects the full file text when file context is on. Verified against Open WebUI v0.13.4 in Phase 2; details and the file-reading API are in [architecture.md § 5](architecture.md#5-open-webui-pipe-design).
- The Pipe holds no business logic.

**Trade-offs.** The Pipe depends on Open WebUI's function API (`__user__`, `__metadata__`, `__files__`, `__event_emitter__`), which can change between versions. Mitigation: pin the Open WebUI image tag and keep the Pipe under 200 lines.

---

## ADR-003 — Docling + RapidOCR with native fallbacks; structural chunking

**Status:** Accepted

**Decision.**
- **PDF, DOCX, PPTX** are parsed by Docling (layout model, TableFormer, RapidOCR for scanned pages).
- **XLSX** uses `openpyxl` (read-only, `data_only=True`), **CSV** uses the stdlib `csv` module, and **TXT/MD** are read as plain text (MD split on headings). These formats don't need a layout model, and the fallbacks are fast and use little RAM.
- Chunking follows document structure (section → paragraphs; tables are separate chunks) and never uses fixed windows across sections. Every chunk carries `document_id`, `filename`, `page_number`, `section_heading`, `element_type` (`text` | `table`), `chunk_index`.
- The parsed document is stored at `parsed/{document_id}.json`, so re-chunking or re-embedding doesn't need a re-parse.

**Trade-offs.** Docling's models add about 1.5–2 GB of peak RAM during ingestion, so ingestion runs one document at a time (ADR-010).

---

## ADR-004 — BGE-M3 dense + sparse in Qdrant with RRF; optional lazy reranker

**Status:** Superseded by ADR-011

**Decision.**
- `BAAI/bge-m3` (via `FlagEmbedding`, fp16 on MPS) produces a 1024-dim dense vector and learned sparse lexical weights for each chunk in one pass. Verified in Phase 3 with FlagEmbedding 1.4.2 on transformers 5.23 / torch 2.15. Models are fetched with `huggingface_hub.snapshot_download` skipping the unused ONNX export (~2.3 GB), and work offline from the HF cache afterwards.
- Qdrant collection `doc_chunks` has named vectors `dense` (cosine) and `sparse`. Hybrid search runs two prefetches (dense and sparse), each tenant-filtered, fused with **Reciprocal Rank Fusion**.
- `BAAI/bge-reranker-v2-m3` is **off by default** (`RERANKER_ENABLED=false`). When enabled it is lazy-loaded on first use and reranks the fused top-k.
- Qdrant is accessed **only** through `src/doc_intel/vector/qdrant_store.py`, whose methods require a `TenantContext`.

**Trade-offs.** One model gives both retrieval modes, so we don't need a separate BM25 index. The reranker improves precision but costs about 1.5 GB of RAM and latency, so it's opt-in.

---

## ADR-005 — Provider-agnostic LLM gateway, default local Ollama

**Status:** Accepted

**Decision.**
- `src/doc_intel/llm/gateway.py` defines an `LLMClient` protocol with one method: `complete(messages, json_schema | None) -> str`. A `stream()` method is added only when a phase needs token streaming (Q&A isn't token-streamed: answers are verified first).
- Implementations, chosen by `LLM_PROVIDER` in `.env`:
  - `ollama` (default) → `OllamaClient` on Ollama's **native** `POST /api/chat` with `think`, `format` (the JSON schema) and `options.num_ctx`. Not Ollama's OpenAI-compatible `/v1`: measured in Phase 4, `/v1` ignores `num_ctx` and runs at 4096 tokens, which would silently cut the start (the instructions) of a Q&A prompt.
  - `openai` → `OpenAICompatClient` on `{LLM_BASE_URL}/chat/completions` with `response_format: json_schema` and a bearer `LLM_API_KEY`; any OpenAI-compatible endpoint works.
  - `anthropic` is planned (FR-17), not built; an unknown provider fails at startup.
- Default model: `qwen3:14b` (Q4_K_M, measured 10.0 GB resident at `num_ctx = 8192`). Low-RAM fallback: `qwen3:4b` (measured 3.9 GB at `num_ctx = 8192`).
- Thinking is off by default (`LLM_ENABLE_THINKING=false` sends `think: false`). A leading `<think>…</think>` block is still removed before JSON parsing or citation checks; a `</think>` quoted from a document further on is kept.
- Structured outputs (Q&A, Template IR, field extraction, intent routing) always use JSON-schema-constrained output and are validated with Pydantic (`complete_json`). Invalid output gets 1 retry with a corrective message, then the step fails (`LLMError` → `503 upstream_unavailable`). The retry carries the original messages plus the failed reply cut to 1,500 characters, so attempts never stack. In mapping, invalid output twice makes that field `missing(llm_error)` (system-design § 7); an unreachable LLM still fails the step.
- Temperature is 0 for every call.

**Trade-offs.** No LangChain/LlamaIndex: the adapter is roughly 150 lines and we control every prompt. Cloud providers send document text off-machine, so they need explicit opt-in through `.env` (see requirements NFR-1).

---

## ADR-006 — Four-stage template engine; PPTX/DOCX/XLSX in-place, PDF source-only

**Status:** Accepted

**Decision.**
1. **Anchor Map (deterministic).** `python-pptx`, `python-docx` + `lxml`, and `openpyxl` enumerate every writable location with a stable positional `anchor_id`.
2. **Template IR (LLM-assisted).** The LLM turns the Anchor Map into `TemplateIR` fields. A validator rejects unknown or duplicate `anchor_id`s and any field without a `write_mode` that is valid for its anchor kind.
3. **Field mapping (evidence-grounded RAG).** Each field is retrieved and extracted separately, producing a `FieldMappingResult`. Quotes are verified deterministically (ADR-007).
4. **Surgical write.** The template file is cloned and only the XML of anchors present in the IR is mutated. PPTX/DOCX go through python-pptx/python-docx + lxml. **XLSX is patched at the zip/XML level** (worksheet `<c>` elements, inline strings, `fullCalcOnLoad`), because `openpyxl` drops charts and images when it re-saves.
- Missing and conflict fields are written as configurable markers (`MISSING_MARKER`, `CONFLICT_MARKER`).
- Non-fillable PDFs can be **sources only**. They are never accepted as templates.

**Trade-offs.** Positional anchors are stable only for the exact template file, which is fine because template uploads are immutable. v1 never adds slides, shapes, or table rows (`shortcut:` documented in system-design.md).

---

## ADR-007 — Zero fabrication: deterministic citation verification, no auto-resolution

**Status:** Accepted

**Decision.**
- The LLM must return verbatim `quote`s tied to `chunk_id`s it was shown.
- The verifier (`src/doc_intel/mapping/verifier.py`, shared with Q&A) accepts a quote only if:
  - its `chunk_id` was in that field's tenant-scoped retrieval set, and
  - the normalized quote is a substring of the normalized chunk text.
- Typed values (number, currency, date, percent) must also appear in a verified quote.
- `status = populated` requires at least 1 verified evidence item (or `provenance = user`). Everything else becomes `missing`, `partial`, or `conflict`.
- Text, list and table values are `populated` only when every item appears in a verified quote; otherwise `partial`.
- Two or more verified candidates with different normalized values give `conflict`. The system **never** picks one; only a user edit resolves it.

**Trade-offs.** Some fields that a human would accept as paraphrases end up `missing`. That is the intended bias: a false `missing` is recoverable, a fabricated value is not.

---

## ADR-008 — Persistence boundaries; project = chat; versioned JSONB results

**Status:** Accepted

**Decision.**

| Store | Owns |
|---|---|
| Open WebUI DB (container volume) | users, auth sessions, chats, messages |
| App PostgreSQL 16 | `projects`, `documents`, `jobs`, `templates`, `template_irs`, `mapping_runs`, `artifacts` |
| Qdrant | `doc_chunks` vectors + tenant payload |
| `StorageBackend` (`./data/storage/`) | immutable uploads, parsed JSON, versioned output artifacts |

- A project is created on demand per `openwebui_chat_id`. Direct API clients can also create projects without a chat.
- `TemplateIR` versions (`template_irs.ir`) and mapping results (`mapping_runs.results`) are JSONB. Each conversational edit creates a new `mapping_runs` row (`parent_run_id`) rather than mutating the old one.
- The backend never reads the Open WebUI DB.

**Trade-offs.** JSONB results can't be queried per field efficiently. That's acceptable at our scale (≤ a few hundred fields per run).

---

## ADR-009 — Tenant isolation and authentication

**Status:** Accepted

**Decision.**
- The FastAPI backend binds `127.0.0.1:8000`.
- Every `/v1` call except `/v1/health` and the signed download requires `Authorization: Bearer <PIPE_SHARED_SECRET>` (constant-time compare) and `X-OWUI-User-Id`. `/v1/pipe/*` also requires `X-OWUI-Chat-Id`.
- The identity headers are trusted **only** because the shared secret matched.
- Every table row and every Qdrant point carries `user_id` + `project_id`. Every query and search filters on both.
- Child tables reference `projects(id, user_id)` with a composite FK, so a row can't point at another user's project.
- A resource owned by someone else returns **404**, never 403.
- Artifact downloads use HMAC-SHA256 signed URLs over `artifact_id|user_id|exp` with `SIGNED_URL_TTL_S=900`.

**Trade-offs.** Single shared secret: anyone with the secret and local access can claim any user id. Acceptable for a single-machine deployment. `shortcut: shared-secret + identity headers, upgrade to per-user signed tokens (JWT from Open WebUI) before any multi-host or network-exposed deployment.`

---

## ADR-010 — Postgres-backed job queue with in-process worker

**Status:** Accepted

**Decision.**
- Long work (`ingest_document`, `extract_template`, `run_mapping`, `render_artifact`) goes into the `jobs` table.
- One asyncio worker runs inside the FastAPI process (started in the lifespan hook). It claims work with `SELECT … FOR UPDATE SKIP LOCKED` and runs with `JOB_CONCURRENCY=1`, because the 24 GB RAM budget can't hold two Docling or LLM runs at once.
- CPU/GPU-bound steps run in `asyncio.to_thread`.
- On startup, **every** `running` job goes back to `queued`, or to `failed` once `attempts` reaches `MAX_JOB_ATTEMPTS = 3`. The only worker lives in this process, so at startup nothing can still be running.
- Handlers are idempotent, because a retry can find partial output from the crashed attempt:
  - Qdrant upserts use deterministic point ids;
  - `parsed/` is overwritable;
  - rendering reuses an existing `artifacts` row for the run;
  - `extract_template` reuses an existing `templates` row;
  - a job whose row was deleted meanwhile (e.g. the document) finishes without retrying and removes any output it wrote;
  - `run_mapping` recomputes every field and writes all results in one transaction; a run past `extracting` is returned unchanged.
- A failed attempt is retried after `RETRY_DELAY_S × attempts` (30 s, then 60 s), so a short Ollama outage doesn't use up every attempt; stopping the worker cuts the wait short. Errors that can't succeed on retry set `retry = False` (e.g. `ParseError`) and fail at once.
- When a job fails for good (last attempt, non-retryable error, or interrupted at startup), the worker calls that kind's failure hook so the row it owns (e.g. the document, or the mapping run for `run_mapping`) is marked `failed` too, never left in progress.
- Progress is written to `jobs.progress`; `/v1/pipe/chat` polls it to emit SSE `status` events.

**Trade-offs.** No Redis/Celery to run. Jobs stop while the API is down and resume on restart. `shortcut: single in-process worker, move to a separate worker process if API latency suffers during ingestion.`

---

## ADR-011 — Ollama `qwen3-embedding` (dense) + local BM25 (sparse); supersedes ADR-004

**Status:** Accepted

**Context.** BGE-M3 ran inside the Python process through FlagEmbedding, which pulled in sentence-transformers, peft, datasets and accelerate, and kept about 1.2 GB resident once loaded. Ollama already runs natively on Metal for the LLM and can serve embedding models too.

**Decision.**
- **Dense:** Ollama `qwen3-embedding:0.6b` via `POST /api/embed` (`embeddings/ollama.py`, `OllamaQwen3Embedder`):
  - batches of 32, `dimensions = EMBEDDING_DIMENSIONS` (1024), `options.num_ctx = 1024`;
  - only queries get Qwen3's retrieval instruction (`Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: `); chunks are embedded as raw text;
  - `dimensions` is Matryoshka truncation, so switching to `qwen3-embedding:4b` in `.env` keeps 1024-dim vectors. Any model change still needs a re-ingest: vectors from different models are not comparable.
- **Sparse:** hashed-token BM25 (`embeddings/sparse.py`, no ML model). Documents carry the BM25 term-frequency part (`k1 = 1.2`, `b = 0.75`, fixed average length 256 tokens); queries carry weight 1 per distinct term. The collection's `sparse` vector uses `modifier = IDF`, so Qdrant computes IDF from the indexed corpus.
- Qdrant, RRF fusion, tenant filters and the `doc_chunks` layout are unchanged from ADR-004. `ensure_collection` refuses a collection created with another dense size or without IDF (the fix: delete the collection, then delete and upload each document again, because re-uploading an indexed file is deduplicated). `hybrid_search` runs the same check, so search never ranks against old vectors. At startup the refusal stops the app with that message, instead of being logged as "Qdrant unavailable".
- **Chunk sizing** uses the Qwen3 tokenizer from Hugging Face (`CHUNK_TOKENIZER = Qwen/Qwen3-Embedding-0.6B`, tokenizer files only).
- **Reranker:** `BAAI/bge-reranker-v2-m3` stays optional and off by default, now on plain `transformers` (`RERANKER_DEVICE`). FlagEmbedding is removed.
- This also amends ADR-001's list of in-process ML: embeddings now run in Ollama, not in the FastAPI process.

**Trade-offs.**
- **Memory:** the model moves out of Python into Ollama rather than disappearing. Measured: 0.81 GB in Ollama at `num_ctx = 1024` (1.24 GB at the default context), against BGE-M3's 1.18 GB on MPS. Ollama also unloads it after `OLLAMA_KEEP_ALIVE` when idle. The Python process loses FlagEmbedding's dependency tree. torch stays, because Docling needs it.
- **Sparse quality:** hashed BM25 is a lexical baseline, not BGE-M3's learned weights. The Phase 3 smoke test still finds the expected chunk in the top 8 for 10 of 10 queries.
- **Dependency:** ingestion and search now need Ollama running, which they need anyway for the LLM (Phase 4).

