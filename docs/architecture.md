# Architecture

Why each choice was made: [DECISIONS.md](DECISIONS.md). Names: [data-model.md](data-model.md), [api-specification.md](api-specification.md). Module internals: [system-design.md](system-design.md).

## 1. Components

```
┌──────────────────────── Docker Compose (Linux VM, ≤ 3 GB) ─────────────────────────┐
│                                                                                     │
│  ┌─────────────────────┐      ┌──────────────────┐      ┌────────────────────────┐  │
│  │ Open WebUI          │      │ PostgreSQL 16    │      │ Qdrant                 │  │
│  │ :3000 → 8080        │      │ 127.0.0.1:5432   │      │ 127.0.0.1:6333 / 6334  │  │
│  │ users, chats, auth  │      │ app tables       │      │ doc_chunks             │  │
│  │ + doc_intel Pipe    │      │                  │      │                        │  │
│  └─────────┬───────────┘      └────────▲─────────┘      └───────────▲────────────┘  │
└────────────┼───────────────────────────┼────────────────────────────┼───────────────┘
             │ HTTP + SSE                │ asyncpg                    │ qdrant-client
             │ host.docker.internal:8000 │                            │
┌────────────▼───────────────────────────┴────────────────────────────┴───────────────┐
│ Native macOS (uv, Python 3.12) — FastAPI doc_intel, 127.0.0.1:8000                   │
│                                                                                      │
│  api/v1 ─▶ chat/ ─▶ jobs/worker (in-process, concurrency 1)                          │
│              │          │                                                            │
│              ▼          ▼                                                            │
│  ingestion/ (Docling + RapidOCR, native fallbacks)   templates/ (anchors, IR)        │
│  embeddings/ (Ollama dense + BM25 sparse; reranker)  mapping/ (retrieve, verify)     │
│  vector/qdrant_store.py (only Qdrant access)         writers/ (pptx, docx, xlsx)     │
│  storage/ (StorageBackend → ./data/storage/)         llm/gateway ──────────┐         │
└────────────────────────────────────────────────────────────────────────────┼─────────┘
                                                                             │ native /api/chat (ADR-005)
                                                     ┌───────────────────────▼─────────┐
                                                     │ Ollama (native, Metal)          │
                                                     │ 127.0.0.1:11434                 │
                                                     │ swappable: OpenAI-compatible    │
                                                     │ e.g. mlx_lm.server :8080/v1     │
                                                     └─────────────────────────────────┘
```

## 2. Ingestion flow

```
User drops file in Open WebUI chat
  │
  ▼
Pipe: each __files__ entry ──▶ POST /v1/pipe/files (bytes, openwebui_file_id)
                                          │
                                          ▼
                          validate ext + magic + size → sha256 dedup
                                          │
                          StorageBackend.put_bytes(uploads/{document_id}/original.{ext})
                                          │
                          INSERT documents(status='uploaded') + jobs(kind='ingest_document')
                                          │
                          worker: status='parsing'
                                          │
            ┌─────────────────────────────┴───────────────────────────────┐
            ▼                                                             ▼
   pdf/docx/pptx → Docling (layout, tables, RapidOCR)       xlsx/csv/txt/md → native parsers
            └─────────────────────────────┬───────────────────────────────┘
                                          ▼
                     parsed/{document_id}.json → structural chunker
                                          │  (text ≤ 512 tok per section; tables separate)
                                          ▼
                     Ollama qwen3-embedding (dense 1024) + BM25 (sparse), batch 32
                                          │
                     qdrant_store.upsert(ctx, points)  ── payload: user_id, project_id,
                                          │                document_id, page_number, …
                                          ▼
                     documents.status='indexed', jobs.status='succeeded'
```

## 3. Q&A flow

```
User question ─▶ Pipe ─▶ POST /v1/pipe/chat (SSE)
                              │
                              ▼
                 chat/router: intent = "qa"
                              │
                 retriever.search(ctx, q): qwen3 query + BM25 → Qdrant prefetch dense ∥ sparse
                              │              (filter user_id AND project_id) → RRF → [rerank]
                              ▼
                 LLM: answer_markdown + citations[{marker, chunk_id, quote}]
                              │
                 verifier: chunk_id ∈ hits ∧ normalize(quote) ⊂ normalize(chunk.text)
                              │
                 drop unverified markers; none left → "not found in uploaded documents"
                              │
                 SSE delta: verified answer + sources (filename p.N) ─▶ Pipe ─▶ chat
```

## 4. Template mapping flow

```
"Fill crisis_update.pptx from the reports" ─▶ /v1/pipe/chat
   │
   ▼
router: intent=map_template, candidates=[doc]   (0 or >1 → ask user to confirm, stop)
   │
   ▼
documents.role='template'; delete its Qdrant points
   │
   ▼  job extract_template
Stage 1  Anchor Map (python-pptx / python-docx+lxml / openpyxl)  → templates.anchor_map
Stage 2  LLM → TemplateIR → validator (anchors exist, write_mode valid) → template_irs v1
   │
   ▼  job run_mapping   (mapping_runs: queued→retrieving→extracting→verifying→rendering)
Stage 3  for each field:
           retrieve(label+description+section_path, run.document_ids)
           LLM extract candidates + verbatim quotes
           verify quotes; typed values must appear in a quote
           0 cand → missing │ 1 → populated/partial │ ≥2 distinct → conflict (never auto-resolved)
         → mapping_runs.results
Stage 4  clone template → surgical writer → MISSING/CONFLICT markers
         → artifacts/{artifact_id}.{ext} + artifacts row
   │
   ▼
chat summary (counts, missing, conflicts w/ citations, warnings) + signed download_url
   │
   ▼
user: "use the ops report number for customers" → intent=edit → new mapping_runs (v+1)
      → render_artifact → new artifact + link
```

## 5. Open WebUI Pipe design

File: `integrations/openwebui/doc_intel_pipe.py`, under 200 lines, no business logic.

| Concern | Behavior |
|---|---|
| Valves | `BACKEND_URL` (default `http://host.docker.internal:8000`), `PIPE_SHARED_SECRET`, `TIMEOUT_S` (default 1800) |
| Identity | `__user__["id"]` → `X-OWUI-User-Id`; `__metadata__["chat_id"]` → `X-OWUI-Chat-Id` |
| Task calls | if `__metadata__.get("task")` or the last user message starts with `### Task:` → return `""` without calling the backend. ✅ verified (Open WebUI v0.13.4): `POST /api/v1/tasks/title/completions` reaches the Pipe with `__metadata__["task"] == "title_generation"` (also passed as `__task__`) and a user message starting `### Task:`; every task route sets `metadata.task` (`routers/tasks.py`). Caveat: Open WebUI's RAG template also starts with `### Task:`, so the prefix check is only safe while file context is off (see Built-in RAG). |
| Files | for every `__files__` entry, read its bytes through Open WebUI's file storage and `POST /v1/pipe/files`. Entries that are not dicts, or lack an id or name, are skipped. `Storage.get_file` plus the read run in `asyncio.to_thread`, off the event loop. The Pipe keeps no state; the backend dedups by `sha256`. `shortcut: every chat turn re-uploads all files of the branch; the backend dedups by sha256. Upgrade by skipping ids the backend already knows if large files make turns slow.` ✅ verified (Open WebUI v0.13.4): an entry is the frontend's item `{type: "file", id, name, url, content_type, size, status, file: {id, user_id, filename, path, meta: {name, content_type, size, file_hash}, …}}`; `file.meta.file_hash` is the sha256 of the raw bytes. Read bytes with `rec = await Files.get_file_by_id_and_user_id(id, __user__["id"])` (`open_webui.models.files`, async) → `open(Storage.get_file(rec.path), "rb")` (`open_webui.storage.provider`, sync; returns a local path). Local layout: `/app/backend/data/uploads/{file_id}_{filename}`. The probe read the bytes with `Files.get_file_by_id(id)`, and the sha256 and length matched the uploaded file. The `_and_user_id` variant has the same return type (source only, `models/files.py:168`) and adds an ownership check. |
| `__files__` scope | ✅ verified (Open WebUI v0.13.4): `__files__` is the request's `files` list (`body["files"]` is removed before the Pipe runs). The UI sends the non-image files of every message in the current branch plus the new message's files, so the list accumulates: msg 1 + A → `[A]`; msg 2, no attachment → `[A]`; msg 3 + B → `[A, B]`. The backend's sha256 dedup makes re-sending harmless. |
| Chat | `POST /v1/pipe/chat` with `messages` + `files[{openwebui_file_id, filename}]`; parse SSE |
| Status | `status` events → `await __event_emitter__({"type": "status", "data": {"description": …, "done": …}})` |
| Output | `delta` events → `yield content`; `error` → yield a readable message |
| Stream errors | malformed SSE frames, invalid JSON, or a stream that closes before `done` → emit status `done: true` and yield a readable message. Transport errors (unreachable, timeout) get their own message. |
| Messages | at most 20 messages forwarded (`MAX_MESSAGES`), user and assistant roles only, text parts flattened |
| Keep-alive | `:` comment lines from the backend are ignored |
| Backend reachability | ✅ verified in Phase 1: `docker compose exec open-webui curl http://host.docker.internal:8000/v1/health` → `200` while the backend binds `127.0.0.1:8000`. Re-confirmed in Phase 2 (v0.13.4, `--add-host host.docker.internal:host-gateway`): a host server on `127.0.0.1:8765` → `200` |
| Built-in RAG | ✅ verified (Open WebUI v0.13.4). `BYPASS_EMBEDDING_AND_RETRIEVAL` is the right name, but it does **not** stop context injection. With it `true`, uploads still have their text extracted into `file.data.content`; only embedding is skipped. On requests without a socket `session_id`, the whole file text is injected into the last user message through the RAG template (which starts with `### Task:`). It is also persistent config: the env var only seeds the DB on first start, and later env changes are ignored. The working switch is the Pipe model's capabilities: `meta.capabilities = {"file_context": false, "builtin_tools": false}` (Workspace → Models, or `POST /api/v1/models/create` with `id` = the Pipe id). With both set to `false`, the Pipe receives the user's text unchanged, with no file text and no `<attached_files>` tags, and `__files__` is still populated. With `builtin_tools` left on, UI chats prefix each user message that had attachments with `<attached_files><file … id=… name=…/></attached_files>`. Keep `BYPASS_EMBEDDING_AND_RETRIEVAL=true` anyway: it skips the embedding-model download. |

## 6. Memory budget (24 GB unified memory)

Peak at steady state (one mapping run active, Docker up, default 14B model, reranker off):

| Consumer | Resident (GB) | Notes |
|---|---|---|
| macOS + desktop apps | 5.0 | browser with Open WebUI tab included |
| Docker VM (Open WebUI ~1.0, Postgres ~0.5, Qdrant ~0.5, VM overhead) | 3.0 | cap the VM at 3 GB in Docker Desktop; compose `mem_limit`s: open-webui 1.5g, postgres 512m, qdrant 1g |
| Ollama `qwen3:14b` Q4_K_M, weights + KV cache (32k ctx, one slot) | 14.0 | measured with `ollama ps` at `num_ctx=32768` (10.0 at `num_ctx=8192`: f16 KV cache is 160 KB per token). Set `LLM_NUM_CTX=8192` to get the 10.0 GB back; `OLLAMA_KEEP_ALIVE=10m`, `OLLAMA_NUM_PARALLEL=1`: each extra parallel slot adds its own KV cache |
| Ollama `qwen3-embedding:0.6b` (`num_ctx=1024`) | 0.8 | measured: 0.81 GB (1.24 GB at the default context); unloaded after `OLLAMA_KEEP_ALIVE` when idle. Ollama must be allowed ≥ 2 loaded models (LLM + embedder; its default allows more) |
| FastAPI process (Python, Docling idle) | 0.5 | |
| **Subtotal (mapping)** | **23.3** | ~0.7 GB headroom |
| + Docling layout/TableFormer/RapidOCR (only during ingestion) | +2.0 | loaded lazily, worker concurrency 1 |
| + BGE reranker v2-m3 (only if `RERANKER_ENABLED=true`) | +1.5 | lazy-loaded |
| **Worst case (ingest + LLM + reranker)** | **26.8** | over the limit: set `LLM_NUM_CTX=8192` (22.8 GB) or switch to `qwen3:4b` (measured 3.9 GB at 8k) |

Rules this budget implies:
- `JOB_CONCURRENCY=1` and `OLLAMA_NUM_PARALLEL=1` (chat Q&A and a running job then queue at Ollama instead of each reserving their own KV cache);
- Docling and the reranker are loaded lazily and released when idle;
- use the 7B model if the reranker is enabled.

### Measured (Phase 12, 2026-11-21)

Measured on the target machine with `qwen3:14b` (`LLM_NUM_CTX=8192`) and `qwen3-embedding:0.6b`, reranker off, `JOB_CONCURRENCY=1`, by `tests/eval/test_perf.py` (`uv run pytest -m perf -s tests/eval/test_perf.py`; numbers in the git-ignored `tests/eval/report.json["perf"]`).

| Consumer | Budget (GB) | Measured | Source |
|---|---|---|---|
| Ollama `qwen3:14b` (8k ctx) | 10.0 | 10 GB | `ollama ps`, 100% GPU |
| Ollama `qwen3-embedding:0.6b` (1k ctx) | 0.8 | 813 MB | `ollama ps` |
| Docker containers | 3.0 (VM) | Open WebUI 1.16 GiB, Qdrant 0.78 GiB, Postgres 0.12 GiB (2.06 GiB) | `docker stats`, after the run |
| Python process, peak (incl. Docling models) | 0.5 + 2.0 | 1.56 GB peak RSS | `resource.getrusage` |

So the mapping subtotal stays inside the 19.3 GB estimate, and Docling's real cost (~1.1 GB above idle) is below its 2.0 GB allowance.

NFR-3 latency targets ([requirements.md](requirements.md)), all met:

| Target | Limit | Measured |
|---|---|---|
| Ingest a 50-page text PDF (parse, chunk, embed, upsert; 150 chunks) | ≤ 3 min | 23.7 s, Docling model load included |
| Q&A answer (5 questions) | ≤ 20 s | p50 10.5 s, max 13.5 s |
| Map a 30-field template over 5 sources | ≤ 10 min | 250.6 s (about 8.4 s per field) |

`shortcut: the PDF is synthetic text (no OCR or tables) and the 30 fields come from the eval set; re-measure with a scanned PDF and a real deck if users report slow runs.`
