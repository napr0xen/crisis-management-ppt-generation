# API Specification

**This file is the source of truth for every endpoint, header, and request/response shape.** Table and payload names come from [data-model.md](data-model.md).

- Base URL: `http://127.0.0.1:8000`. All routes are under `/v1`.
- From inside Docker (the Pipe) the base URL is `http://host.docker.internal:8000`.

## Authentication & tenancy

| Header | Required on | Meaning |
|---|---|---|
| `Authorization: Bearer <PIPE_SHARED_SECRET>` | all routes except `GET /v1/health` and `GET /v1/artifacts/{artifact_id}/download` | constant-time compared; mismatch → `401` |
| `X-OWUI-User-Id` | same as above | Open WebUI user id; must match `^[A-Za-z0-9_-]{1,64}$`, else `422` |
| `X-OWUI-Chat-Id` | `/v1/pipe/*` only | Open WebUI chat id; selects or creates the project |
| `X-Request-Id` | response (all) | echoed if sent, otherwise generated |

**Tenancy.**
- `user_id` comes from `X-OWUI-User-Id`; `project_id` comes from the path or the chat id.
- Any `{project_id}`, `{document_id}`, `{job_id}`, `{template_id}`, `{run_id}`, or `{artifact_id}` that doesn't belong to the caller returns **`404 not_found`**, never 403. That includes ids that exist for another user.

## Errors

```json
{"error": {"code": "not_found", "message": "Resource not found.", "request_id": "…"}}
```

| HTTP | `code` |
|---|---|
| 401 | `unauthorized` |
| 403 | `signature_invalid` (signed download only) |
| 404 | `not_found` |
| 405 | `method_not_allowed` |
| 409 | `conflict` (e.g. invalid state transition) |
| 410 | `link_expired` (signed download only) |
| 413 | `payload_too_large` |
| 415 | `unsupported_media_type` |
| 422 | `validation_error` |
| 500 | `internal` |
| 503 | `upstream_unavailable` (Postgres, Qdrant, or LLM down) |

Messages never contain document text.

## Common response models

`Document`:

```json
{
  "id": "uuid", "project_id": "uuid", "role": "source", "filename": "ops_report.pdf",
  "mime_type": "application/pdf", "size_bytes": 482113, "sha256": "…",
  "status": "indexed", "page_count": 12, "chunk_count": 41, "error_code": null,
  "created_at": "2026-11-18T21:00:00Z"
}
```

`Job`:

```json
{
  "id": "uuid", "project_id": "uuid", "kind": "ingest_document", "status": "running",
  "progress": {"stage": "embedding", "done": 12, "total": 41},
  "result": null, "error_code": null, "error_message": null,
  "created_at": "…", "started_at": "…", "finished_at": null
}
```

`Artifact`:

```json
{
  "id": "uuid", "mapping_run_id": "uuid", "format": "pptx",
  "filename": "crisis_update_v2.pptx", "size_bytes": 912345,
  "download_url": "http://127.0.0.1:8000/v1/artifacts/{id}/download?exp=1763500000&sig=…",
  "created_at": "…"
}
```

Lists return `{"items": [...]}`, newest first, with `?limit=` (default 50, max 200). `shortcut: no cursor pagination, add when a project exceeds 200 items of one kind.`

---

## Health

### `GET /v1/health`

No auth. Returns `200` if the API is up, and reports each dependency.

```json
{"status": "ok", "postgres": "ok", "qdrant": "ok", "llm": "ok", "llm_provider": "ollama", "version": "0.1.0"}
```

- Each dependency is `"ok"` or `"unavailable"`.
- `status` is `"degraded"` if any dependency is not `"ok"`. The response code is still `200`, so the Pipe can report the problem to the user.
- `llm` is the active provider's reachability (Ollama `GET /api/version`, OpenAI-compatible `GET /models`; no generation). `llm_provider` names the active provider (NFR-1), so a cloud provider is always visible.

---

## Open WebUI Pipe

### `POST /v1/pipe/files`

`multipart/form-data`: `file` (binary), `openwebui_file_id` (string). Headers: auth + `X-OWUI-User-Id` + `X-OWUI-Chat-Id`.

- Resolves or creates the project for the chat.
- Deduplicates on `sha256`. On a duplicate, appends `openwebui_file_id` to the existing row's `openwebui_file_ids` and returns that document.
- Stores the file, inserts `documents`, and enqueues `ingest_document`.

`201`:

```json
{"project_id": "uuid", "document_id": "uuid", "job_id": "uuid|null", "deduplicated": false}
```

`job_id` is null when the upload was deduplicated, unless the existing document is a `role='source'` document that had `failed`: it is then reset to `uploaded` and a fresh `ingest_document` job is queued (`deduplicated: true` with that `job_id`). Errors: `413`, `415`.

### `POST /v1/pipe/chat`

Headers: auth + `X-OWUI-User-Id` + `X-OWUI-Chat-Id`. Request:

```json
{
  "messages": [{"role": "user", "content": "Fill the crisis_update.pptx template using the reports"}],
  "files": [{"openwebui_file_id": "owui-123", "filename": "crisis_update.pptx"}]
}
```

Response: `text/event-stream`.

| `event` | `data` | Pipe action |
|---|---|---|
| `status` | `{"description": "Embedding ops_report.pdf (12/41)", "done": false}` | `__event_emitter__({"type": "status", "data": …})` |
| `delta` | `{"content": "…markdown…"}` | `yield content` |
| `error` | `{"code": "…", "message": "…"}` | emit status `done: true`, yield message |
| `done` | `{"project_id": "uuid", "mapping_run_id": "uuid|null", "artifact_id": "uuid|null"}` | emit final status `done: true` |

Routing behavior is described in [system-design.md § 9](system-design.md#9-chat-orchestration-chat). The Pipe drops internal `### Task:` requests before calling this endpoint.

---

## Projects

### `POST /v1/projects`

Body: `{"name": "Q3 incident"}`. Returns `201` with the `Project` (`openwebui_chat_id: null`).

### `GET /v1/projects`

The caller's projects: `{"items": [Project]}`.

### `GET /v1/projects/{project_id}`

Returns `Project`:

```json
{"id": "uuid", "name": "…", "openwebui_chat_id": "…|null", "created_at": "…", "document_count": 4}
```

---

## Documents

### `POST /v1/projects/{project_id}/documents`

`multipart/form-data`: `file`. Behaves like `/v1/pipe/files` without the chat, including the retry of a `failed` duplicate. Returns `201` with `{"document_id", "job_id", "deduplicated"}`.

### `GET /v1/projects/{project_id}/documents`

Returns `{"items": [Document]}`.

### `GET /v1/projects/{project_id}/documents/{document_id}`

Returns `Document`.

### `DELETE /v1/projects/{project_id}/documents/{document_id}`

Deletes the Qdrant points (tenant-filtered), the stored upload and parsed JSON, and the row. Returns `204`.
- Order: the row delete is flushed (so `404`/`409` never touch Qdrant), then the points are deleted, then the transaction commits; files go in a background task after the commit.
- `409` if the document is the template of a `templates` row that has mapping runs.
- `503 upstream_unavailable` if Qdrant can't delete the points; nothing is deleted.

---

## Jobs

### `GET /v1/projects/{project_id}/jobs/{job_id}`

Returns `Job`.

---

## Q&A

### `POST /v1/projects/{project_id}/query`

Non-streaming grounded Q&A, used by the eval suite and direct clients.

```json
{"question": "How many customers were affected?", "document_ids": null}
```

`200`:

```json
{
  "answer_markdown": "About 12,000 customers were affected [1].",
  "citations": [{"marker": 1, "document_id": "uuid", "chunk_id": "uuid", "filename": "ops_report.pdf",
                 "page_number": 4, "section_heading": "Impact", "quote": "approximately 12,000 customers were affected"}],
  "found": true
}
```

Every returned citation has passed the verifier. `found: false` means no verified citation, and `answer_markdown` then says so.

- `question`: 1–2,000 characters (keeps the prompt within `LLM_NUM_CTX`), else `422`. Unknown body fields are `422`.
- No search hits → `found: false` without an LLM call.
- `503 upstream_unavailable` if the embedder, Qdrant or the LLM is down, or the LLM returns invalid JSON twice.

---

## Templates

### `POST /v1/projects/{project_id}/templates`

Body: `{"document_id": "uuid"}`.
- Errors: `404` unknown document or another tenant's; `409 conflict` if the document is still `uploaded` or `parsing`; `415` not `pptx|docx|xlsx`; `422` invalid body; `503 upstream_unavailable` if the Qdrant purge fails (nothing changes).
- Sets `role='template'`, deletes its points, and enqueues `extract_template`. If the document already has a `templates` row, the job reuses it and its latest IR.
- Returns `202` with `{"job_id": "uuid"}`; `job.result` will hold `{template_id, template_ir_id, ir_version}`.

### `GET /v1/projects/{project_id}/templates/{template_id}`

```json
{"id": "uuid", "document_id": "uuid", "format": "pptx", "anchor_count": 58,
 "latest_ir_version": 1, "anchor_map": { "…": "AnchorMap" }}
```

`404` unknown template.

### `GET /v1/projects/{project_id}/templates/{template_id}/irs/{version}`

`200`: `{"id": "uuid", "version": 1, "ir": { "…": "TemplateIR" }, "created_at": "…"}`. Errors: `404` unknown template or version; `422` `version` < 1.

---

## Mapping runs

### `POST /v1/projects/{project_id}/mapping-runs`

Body: `{"template_ir_id": "uuid", "document_ids": null}`. `null` means all `indexed` source documents in the project.
- Inserts `mapping_runs` (`trigger='initial'`, `version = max(version for this template_ir_id) + 1`, i.e. 1 for the first run) and enqueues `run_mapping`, which renders the artifact at the end.
- Returns `202` with `{"mapping_run_id": "uuid", "job_id": "uuid"}`.

### `GET /v1/projects/{project_id}/mapping-runs/{run_id}`

```json
{
  "id": "uuid", "template_ir_id": "uuid", "parent_run_id": null, "version": 1, "trigger": "initial",
  "status": "completed", "document_ids": ["uuid"],
  "summary": {"populated": 18, "partial": 2, "missing": 3, "conflict": 1, "warnings": []},
  "results": {"incident_summary": { "…": "FieldMappingResult" }},
  "artifact": { "…": "Artifact" },
  "created_at": "…", "finished_at": "…"
}
```

### `POST /v1/projects/{project_id}/mapping-runs/{run_id}/edits`

Applies edits **on top of `{run_id}`**, creating a new run (see [system-design.md § 10](system-design.md#10-conversational-edits-chateditspy)). `{run_id}` must be `completed`, otherwise `409`.

```json
{
  "edits": [
    {"op": "set_value", "field_id": "spokesperson", "value": "Jane Doe"},
    {"op": "choose_candidate", "field_id": "affected_customers", "candidate_index": 0},
    {"op": "clear", "field_id": "next_update_time"}
  ]
}
```

`EditOp` validation:
- `field_id` must exist in the run's IR;
- `choose_candidate` requires the field to be `conflict` and `0 <= candidate_index < len(candidates)`;
- `set_value` requires `value`.

Any violation → `422` and nothing is applied.

`202`: `{"mapping_run_id": "uuid (new)", "version": 2, "job_id": "uuid"}`.

---

## Artifacts

### `GET /v1/projects/{project_id}/artifacts`

Returns `{"items": [Artifact]}`; each `download_url` is freshly signed.

### `GET /v1/artifacts/{artifact_id}/download?exp={unix}&sig={hex}`

No bearer auth: the signature is the credential.
- `sig = HMAC_SHA256(SIGNING_SECRET, f"{artifact_id}|{user_id}|{exp}")`, where `user_id` is the artifact's owner from the DB.
- Bad or missing `sig` → `403 signature_invalid`.
- `exp` in the past → `410 link_expired`.
- Unknown `artifact_id` → `404`.

`200` streams the file with `Content-Disposition: attachment; filename="<artifacts.filename>"` and the correct OOXML `Content-Type`.
