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
  - `ollama` (default) → `OllamaClient` on Ollama's **native** `POST /api/chat` with `think`, `format` (the JSON schema), `options.num_ctx` and `options.num_predict = min(num_ctx // 2, 4096)` (with `think` on: `max(that, num_ctx - prompt_tokens(num_ctx))`, `prompt_tokens(n) = max(6144, n // 2)`, because reasoning tokens count against the same cap and a long reasoning would cut the JSON off; the field-extraction chunk budget is sized from `prompt_tokens`, the IR builder and Q&A stay sized for an 8k window). Without that cap, a schema-constrained reply that degenerates (e.g. endless whitespace) runs until `LLM_TIMEOUT_SECONDS`. A reply that is still invalid after the one JSON retry raises `LLMInvalidOutput` with `retry = False`: at temperature 0, re-running the job fails the same way. Not Ollama's OpenAI-compatible `/v1`: measured in Phase 4, `/v1` ignores `num_ctx` and runs at 4096 tokens, which would silently cut the start (the instructions) of a Q&A prompt.
  - `openai_compat` (named `openai` before Phase 17) → `OpenAICompatClient` on `{LLM_BASE_URL}/chat/completions` with `temperature: 0`, `max_tokens` (the same cap as Ollama's `num_predict`; `mlx_lm.server` defaults to only 512), `chat_template_kwargs: {"enable_thinking": LLM_ENABLE_THINKING}` only when `LLM_CHAT_TEMPLATE_KWARGS=true` (switches Qwen3 reasoning off on `mlx_lm.server`, LM Studio and vLLM; off by default because strict OpenAI-compatible servers reject unknown fields with 400), `response_format: json_schema` and a bearer `LLM_API_KEY`. Servers that ignore `response_format` (`mlx_lm.server` does, and Qwen3 then fences its JSON in markdown or wraps it in prose) are covered three ways: the JSON schema is also appended to the last user turn (`with_schema`), the first JSON object is cut out of the reply (`extract_json`: leading `<think>` block, markdown fences, prose), and the Pydantic validation's one retry names each failing field (cause: Phase 18 live test, `extract_template` failed `LLMInvalidOutput` twice on `mlx_lm.server`). Meant for local servers such as Apple's `mlx_lm.server` (Phase 17: faster than Ollama on Apple silicon); embeddings stay on Ollama (`EMBEDDING_BASE_URL`). `LLM_NUM_CTX` is not sent: it must match the server's context, because prompts are sized from it.
  - `anthropic` is planned (FR-17), not built; an unknown provider fails at startup.
- Default model: `qwen3:14b` (Q4_K_M, measured 10.0 GB resident at `num_ctx = 8192`). Low-RAM fallback: `qwen3:4b` (measured 3.9 GB at `num_ctx = 8192`).
- Thinking is off by default (`LLM_ENABLE_THINKING=false` sends `think: false`, or `enable_thinking: false` on `openai_compat` when `LLM_CHAT_TEMPLATE_KWARGS=true`). A leading `<think>…</think>` block is still removed before JSON parsing or citation checks; a `</think>` quoted from a document further on is kept.
- Structured outputs (Q&A, Template IR, field extraction, intent routing) always use JSON-schema-constrained output and are validated with Pydantic (`complete_json`). Invalid output gets 1 retry with a corrective message that lists each Pydantic error as field path and message (at most 10, never the input values, which can be document text), then the step fails (`LLMError` → `503 upstream_unavailable`). The retry carries the original messages plus the failed reply cut to 1,500 characters, so attempts never stack. In mapping, invalid output twice makes that field `missing(llm_error)` (system-design § 7); an unreachable LLM still fails the step.
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

## ADR-012 — Global template library with copy-on-use

**Status:** Superseded by ADR-014 (only the CLI-only registration rule; copy-on-use and the chat resolution order stand)

**Context.** Users want a deck or document generated from source files alone ("make a powerpoint out of this"), without uploading a template each time. Extracting a 10-slide template's IR takes about 6 minutes on qwen3:14b. Every tenant table carries `(user_id, project_id)` with a composite FK to `projects` (ADR-009), so a shared template can't live in those tables without a fake owner.

**Decision.**
- A global `library_templates` table holds the file reference, `AnchorMap` and `TemplateIR`, built once when the operator registers it with the CLI (`seed-template`). It is the third named exception to the tenant rule. It is read-only to chat and never joined to tenant rows.
- **Copy-on-use:** picking a library template in chat copies its bytes, `documents`, `templates` and `template_irs` rows into the caller's project in one transaction (`templates/library.py`, `adopt`), then starts `run_mapping` on the copied IR. Job handlers, writers, artifacts and edits stay unchanged and fully tenant-scoped.
- Registration is CLI-only. There is no admin role, and the Pipe's shared secret is not an admin credential, so no API endpoint can add or remove shared templates.
- Chat resolution order: an explicit library name or a number reply to a library list, then a confirmed uploaded template, then uploaded candidates of the requested format, then the library (one format match runs, otherwise a numbered list). The router's new `format` field narrows uploaded candidates, so a source DOCX is not taken as the template for "make a powerpoint".

**Trade-offs.**
- The template bytes and IR are duplicated per project that uses them (a few MB and a JSON row). In exchange, removing or replacing a library template never breaks a user's runs, and no handler reads across tenants.
- An IR copied into a project doesn't pick up later improvements to the library entry: re-registering under a new name is the upgrade path.
- Picking a library template whose exact bytes are already a source document in the chat returns `409`. The user fills that document by its filename instead.

## ADR-013 — Chat edits take values only from the user's message

**Status:** Accepted

**Context.** FR-14 lets users edit a filled template in chat. A `set_value` result is `populated` with `provenance = user` and no evidence, so it bypasses quote verification. The ops are parsed from the chat by the local LLM, which could put a value the user never wrote into the document.

**Decision.**
- `parse_edits` (`chat/edits.py`) rejects a `set_value` whose value does not appear in the last user message as whole words (after `normalize`), so a cut-down `1200` does not pass inside `12000`. The edit is not applied and the reply says the new value must be written in the message.
- `choose_candidate` only picks one of the verified conflict candidates, and `clear` writes the missing marker, so neither can introduce a value.
- `POST …/mapping-runs/{run_id}/edits` takes the ops directly (an API client is the user), so the check applies only to chat.
- An edit always targets the chat's latest `completed` run and creates a new run and artifact. The parent run is never modified.

**Trade-offs.**
- A paraphrased value ("make it two million" for `2000000`) is rejected. The user writes the value itself instead.
- The edit LLM sees the field list, current values and conflict candidates, which is document-derived text. It stays inside the local LLM call and is never logged.

## ADR-014 — Library templates can also be added and removed over HTTP; supersedes ADR-012's CLI-only rule

**Status:** Accepted

**Context.** The custom web UI (`frontend/`) needs a Templates page with upload, scan status and delete. ADR-012 allowed registration through the CLI only, because there was no admin role and the Pipe's shared secret was not an admin credential. In this single-operator, local deployment the holder of `PIPE_SHARED_SECRET` is the operator: the same person runs the CLI, the API and the UI.

**Decision.**
- `GET/POST /v1/library/templates`, `DELETE /v1/library/templates/{id}` and `GET /v1/library/scans/{scan_id}` are available to the bearer-secret holder (plus the usual `X-OWUI-User-Id`). The library stays global: no project in the path. The CLI keeps working and shares `templates/library.py` (`prepare`, `store`) with the HTTP path.
- Upload checks (size, type, name and file uniqueness, AnchorMap) run in the request. The IR build, which takes minutes on a local LLM, runs as a background task and is tracked by an in-memory scan record. Only an error code is stored for a failed scan, never exception text.
- Copy-on-use (ADR-012) is unchanged: picking a template copies it into the caller's project, and deleting a library template never touches adopted copies.

**Trade-offs.**
- Any user who holds the shared secret can add or remove library templates. That is acceptable only while the deployment is single-operator; a multi-user deployment needs a real admin role first.
- Scan state is lost on restart (a scan in flight is gone and the file is uploaded again); persist it if scans must survive a restart.

## ADR-015 — Hybrid bounded thinking and curated retrieval context

**Status:** Accepted

**Context.** With thinking off, extraction of rows, records and synthesized lists misses structure that a short reasoning step finds. With thinking on for every call, `qwen3` reasoning loops until the timeout (see `LLM_ENABLE_THINKING`). Retrieval handed the LLM every neighbor chunk, including near-identical ones, with no token budget.

**Decision.**
- `LLM_HYBRID_THINKING` (`openai_compat` only): the four reasoning-heavy calls ask for thinking, streamed and capped at `LLM_THINKING_BUDGET_TOKENS` reasoning events. Over the cap the stream is closed and one non-streaming, non-thinking second pass gets the partial reasoning appended to the last user turn. `mlx_lm` cannot continue an assistant turn, so the overrun becomes a second pass instead of a continuation. The second pass never re-enters the bounded path, so it cannot loop.
- Retrieval: chunks are embedded with `filename > section` context, near-duplicates (Jaccard >= 0.9) are dropped, the context is cut at `RETRIEVAL_CONTEXT_TOKENS`, query embeddings are cached (LRU 512), list items keep a `- ` prefix and running headers and footers are dropped at ingestion except their first copy (a vendor or project name repeated on every slide stays findable). Mapping sizes its chunk count from the token budget before fetching neighbors, half ranked hits and half neighbors, so the trim never drops a top hit.
- A `retrieval` and an `llm_call` log event carry ids, scores and counts only.

**Trade-offs.**
- One SSE event counts as one token; a server that batches deltas under-counts, so the cap is loose there. Ollama ignores the per-call flag.
- A cut-off reasoning costs a second request on the overrun path.
- Existing collections keep the old (bare text) embeddings until documents are re-ingested.

**Skipped.**
- Chunk overlap: heading-aware chunking plus `with_neighbors` already restores context, and overlap would add near-duplicates that the new dedup then drops.
- A date payload key: no query path filters on dates.
- A retrieval result cache: results depend on the index state, and the embedding cache removes the dominant repeated cost.

## ADR-016 — One extraction call per slide, sheet or section; thinking only for multi-row structure

**Status:** Accepted (amends ADR-015's list of thinking calls)

**Context.** A live run of a 10-slide deck (89 fields) took about 14 minutes. Every loose slot (not a table, chart, card set or numbered grid) was mapped alone: one retrieval, one extraction, often a corrective retry and a heading check, so two or three sequential LLM calls per slot. Consecutive slots on one slide repeated near-identical retrievals. Synthesis, the slot-role check and single-card records asked for bounded thinking, hit the 160-token cap and paid a second pass. Some slots also failed for structural reasons: cover-slide metadata (title, author, date) had no heading to retrieve by; a `#` count was accepted with its number from one chunk and its label from another; a tile caption token typed as a currency could never verify its own label text.

**Decision.**
- The loose fields of one unit (`_unit`: pptx slide, xlsx sheet, docx heading section) are mapped by one retrieval and one structured call, at most `MAX_BATCH_SLOTS` (12) per call. Each slot is still verified on its own by `resolve`, so nothing is populated without a verified quote. A lone loose field off the opening unit keeps the single-field path.
- Escalation only when needed: a slot that breaks a slot rule is re-mapped alone with its one corrective retry; a list that needs synthesis is synthesized; the heading check runs only for slots with different headings that received the same value; an unusable batch reply falls back to the single-field path.
- Batch retrieval merges one search per slot and one per distinct heading, cut by the `RETRIEVAL_CONTEXT_TOKENS` token budget. The opening unit always gets each source document's first two chunks (cover metadata).
- Thinking (hybrid mode) is requested only by `fill_table_rows`/chart rows and by record groups of two or more rows. Synthesis, the slot-role check, one-row records and the batch call run without it.
- A `#` count needs its number and its own label in the same chunk.
- In a card, a typed token with no ordinal beside a numbered value token is the card's caption and is `text`. Table header cells without tokens are dropped from the IR by code. A pptx text box carries `fit_chars`, a soft length hint for the prompt only: never a constraint, a warning or a cut.

**Trade-offs.**
- A batch prompt shares one context: a slot whose evidence is outside the merged context is missing where a single-field retrieval might have found it. The per-slot searches feed the merge to limit this.
- Column positions in a sideways source table are still read by the model; code checks only that the label and the value share a chunk.
- Existing template IRs keep the old types and no `fit_chars` until the template is extracted again.

**Skipped.**
- Batching card sets and numbered grids with the loose slots: they already map in one call per group.
- Writing `fit_chars` into `constraints.max_chars`: it would trigger synthesis and `max_chars_exceeded` warnings for estimates.

## ADR-017 — templates/ is the library's source of truth

**Status:** Accepted. Amends ADR-014: uploads and deletes go through the folder.

**Context.** The library lived only in the database and blob storage, so a template could be added through the UI or the CLI with no durable, reviewable source, and a changed extraction (a new IR builder) could not be applied without re-uploading every template by hand.

**Decision.**
- `TEMPLATES_DIR` (default `templates/`) holds the library files. At startup and on `POST /v1/templates/sync` a background sync adds new files, re-extracts files whose bytes changed or whose row has an older `ir_builder_version`, and prunes rows whose file is gone (system-design § 5.1).
- `POST /v1/library/templates` saves the upload into the folder (`409` if the name exists) before the scan; `DELETE` removes the row, the blob and the file. A later sync therefore finds file and row equal and keeps them.
- A missing `TEMPLATES_DIR` disables the sync instead of pruning.
- One `asyncio.Lock` serializes the sync and upload scans.
- The CLI `reset-library` empties the table and blobs; it leaves the folder alone.

**Trade-offs.**
- Scan state is in memory and the lock is per process: a restart loses a running sync (the next start retries) and two backend processes would race.
- Copies already adopted into a chat keep the IR they were adopted with; use a new chat to pick the re-extracted template.
- Files skipped for duplicate bytes or a name clash show only in the log counts, not in the UI.

## ADR-018 — `derived` fields: aggregates computed in Python over structured rows

**Status:** Accepted (Phase 24). Narrows ADR-007 for computed figures: a value computed by code from complete, verified rows may be `populated`; an LLM-computed figure stays `partial`.

**Context.** Totals, net movements, counts and category breakdowns are printed nowhere in a source, so a verbatim quote cannot state them. Before Phase 24 such a slot was `synthesis`: the LLM did the arithmetic and `derived_in_quotes` accepted only `a = b ± c` over two quoted numbers, always `partial`. A sum over 40 statement rows could not be filled at all, and LLM arithmetic is a fabrication risk. Phase 23 gave every table chunk its structured rows (`ChunkPayload.rows`, ids `P1-T2-R04`).

**Decision.**
- `IRField.mode` gains `derived`; `IRField.derivation` (optional) holds `{op, column, minus, where, tag, group_by, top_n, exclude_rows}` (`tag` keeps only `CR` or only `DR` cells) with `op` in `sum | diff | count | min | max | group_sum`. The IR builder only classifies `mode`; it cannot see source headers.
- At mapping time a `derived` slot reads **every** table chunk of the run's documents (`QdrantStore.table_chunks`, tenant-filtered), not the retrieval top-k, so no row is lost to ranking. When the IR has no `derivation`, one schema-constrained LLM call binds it: it picks a table id and exact column headers from the real header list (with the first and last rows shown, row ids included). The LLM never sees or returns a computed number.
- Python validates the binding (table and headers exist, `op` fits `expected_type`), parses each cell with the column's number convention (European `1.829.914,84 $`, US `1,829,914.84`; one ambiguous convention fails closed), treats `DR`/`CR` as tags, not signs (`diff` without `minus` is `Σ CR − Σ DR` and fails closed on an untagged cell; a column mixing `CR` and `DR` without a `tag` filter, and an accounting negative in parentheses, fail closed), and computes with `Decimal`; the exact `Decimal` is stored, never the rendered text re-parsed.
- Fail closed to `missing` (existing reasons, log codes only): no table or column (`no_relevant_chunks`), binding not found (`not_found_by_llm`), row ids not contiguous `R01..Rn`, a cell with two numbers or unparseable digits, a plain-text-split table chunk adjacent to the bound table (`evidence_failed_verification`).
- Every row used is cited: one `Evidence` per row, `quote` = the row's verbatim line in the chunk, new optional `Evidence.row_id` (`P1-T2-R04`). The result is `populated` with typed confidence.
- `IR_BUILDER_VERSION` 4 → 5.

**Trade-offs.**
- The binding is an LLM choice (which table, which columns), checked only for existence, the same trust level as choosing which quote to extract. A wrong-but-real column gives a wrong-but-real total; the cited rows make it reviewable.
- Printed total, subtotal and carried-forward rows are detected arithmetically (`derived.summary_rows`, no keywords, any language): over each value sequence an op aggregates, a row equal to the sum of the values since the previous such row, or to the sum of all values before it (each over at least two values), is a summary row. `compute` then fails closed with `summary_row`; it never auto-excludes (excluding a genuine row would give a wrong but verified total). An LLM binding gets one corrective re-binding naming the flagged row ids so it lists them in `exclude_rows`; a second `summary_row` or any failure is `missing(evidence_failed_verification)`, and so is a preset derivation (it cannot name row ids). Trade-off: a genuine row that coincidentally equals the running or cumulative sum makes the slot missing; a count over a text column is not checked. An `exclude_rows` id is accepted only if `summary_rows` flags it when run with no exclusions (else `exclude_unverified`, `missing`), so a binding cannot drop a genuine row; trade-off: a non-sum row the binding rightly skips (an opening balance printed in an amount column) makes the slot missing.
- One table per slot: a logical table split into tables with different headers (not joined by Phase 22) is aggregated per table only.
- Documents ingested before Phase 23 have no `rows`; their derived slots are `missing` until re-ingest.
- Phase 27 follow-up: the chunker tags each `TableRow` with `row_role` from an EN/ES phrase list (`carry_forward`: brought/carried forward, saldo anterior, …; `total`: total, total charged, subtotal, suma, …; a single-word phrase must be the whole cell; `total` only when every other non-empty cell is an amount, so a dated `TOTAL` merchant row stays `data`). `derived._kept` skips non-`data` rows before aggregating, and an `exclude_rows` id of such a row is accepted. Reason: a one-value table (`Fees` + `Total charged`) cannot be caught arithmetically and summed twice. Arithmetic `summary_rows` stays the fail-closed backstop for untagged totals in other languages or wordings. Trade-off: an undated genuine row whose only text cell is a total phrase is skipped. Documents ingested before Phase 27 read every row as `data` until re-ingest.

## ADR-019 — Ranked derived slots, deck-wide numeric role guard, amount-in-identifier-column rejection

**Status:** Accepted (Phase 25). Extends ADR-018 (`derived` ops and IR classification) and the Phase 21 slot-role check.

**Context.** A live run of a finance deck (`finance_test.pdf`) after Phase 24: slide 2's net movement took the closing balance and the closing balance went `missing` (the same number filled two roles on different slides, which the per-slide role check never compared); slide 4's Reference column was filled with amounts copied from the neighbouring amount column; slides 5 (category breakdown) and 6 (largest payments and receipts) were `missing` because every IR slot was classified `extractive`, though no source states a k-th largest row or a category total.

**Decision.**
- Role guard: typed slots (`number`, `currency`, `percent`) are grouped by canonical number across the whole deck (`"$ 402.031,45"`, `"402.031,45 $"` and `402031.45` are one group), then checked together as before. Dates stay per slide.
- IR builder: a number or currency slot whose label or id names a net movement (EN/ES term set) is forced to `derived`. A numbered slot (`receipt_date_2`, `category_1_amount`) whose label, id, section path or context holds a ranking or breakdown word is forced to `derived` when its numbered group (first id word, slot number) holds a number or currency slot (`_ranking_slots`); a text-only list is left alone.
- Derivation: new `op` `top` (the k-th largest row by magnitude of `column`; `tag` `CR`/`DR`; the slot takes the `pick` cell, default `column`); `group_sum` gains `group_sep` (the category is the cell text before a separator, for description-prefix categories) and `rank` (the k-th group's label or sum). `rank` is set by code from the field id's slot number for ranking slots only, never by the LLM. A signed column with no tag and both signs fails closed (`mixed_tags`); `rank` above the row or group count is `rank_out_of_range`.
- Rank consistency (`engine._check_ranks`): the cells of one record (slide, first field id word, number) must cite the same rows, and two ranks must not cite identical rows, else the cells are `missing` (code `rank_mixed`).
- Amount in an identifier column: a `text` slot or table/grid column whose header names an identifier, reference, description or date kind rejects a pure amount (`verifier.is_amount`, code `amount_in_text_column`). The group's escalation retry receives the rejection notes. Ambiguous words (account, card, details) are not in the word list.
- `IR_BUILDER_VERSION` 5 → 6.
- Phase 26 follow-up: `IR_BUILDER_VERSION` 6 → 7; directional totals, largest singletons and category grids with text-typed amounts are forced to `derived`; `derivation_intent` fixes op and tag from the names (the binder keeps the columns), the role check compares role keys, and the group query carries a CR/DR hint. See `docs/system-design.md` (Direction rules).

**Consequences.**
- More binding LLM calls: one per ranked cell (each cell of a ranked record is its own derived slot).
- The net-movement terms, ranking words and identifier header words are English/Spanish lists (`shortcut:` comments in `ir_builder.py` and `engine.py`); another language needs words added.
- Templates must be re-extracted at version 6 (the sync does it); IRs stored at 5 keep their `extractive` classification until then.
- Rank rejection is all-or-nothing per record: a binding that disagrees on one cell loses the whole row, never mixes rows.
