// Thin client for the doc_intel API (docs/api-specification.md).

// Dev (Vite on :5173) calls the API directly (CORS); the built UI is served by the API itself.
const BASE = import.meta.env.VITE_API_BASE ?? (import.meta.env.DEV ? "http://127.0.0.1:8000" : "");

export type Settings = { token: string; userId: string };

const SETTINGS_KEY = "docintel.settings";

export function loadSettings(): Settings {
  const raw = localStorage.getItem(SETTINGS_KEY);
  // shortcut: the API bearer secret is kept in localStorage (local single-operator app),
  // move to a session cookie if the UI is ever exposed beyond localhost.
  return raw ? (JSON.parse(raw) as Settings) : { token: "", userId: "local-user" };
}

export function saveSettings(s: Settings): void {
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

function headers(chatId?: string): Record<string, string> {
  const s = loadSettings();
  const h: Record<string, string> = {
    Authorization: `Bearer ${s.token}`,
    "X-OWUI-User-Id": s.userId,
  };
  if (chatId) h["X-OWUI-Chat-Id"] = chatId;
  return h;
}

async function failure(res: Response): Promise<ApiError> {
  const err = (await res.json().catch(() => null))?.error ?? {};
  return new ApiError(res.status, err.code ?? "http_error", err.message ?? res.statusText);
}

async function call<T>(path: string, init: RequestInit = {}, chatId?: string): Promise<T> {
  const res = await fetch(BASE + path, {
    ...init,
    headers: { ...headers(chatId), ...(init.headers as Record<string, string> | undefined) },
  });
  if (!res.ok) throw await failure(res);
  return (res.status === 204 ? undefined : await res.json()) as T;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  body: JSON.stringify(body),
  headers: { "Content-Type": "application/json" },
});

// ---- chat (the Pipe endpoints; a UI chat id plays the Open WebUI chat id) ----

export type UploadedFile = {
  project_id: string;
  document_id: string;
  job_id: string | null;
  deduplicated: boolean;
};

export function uploadFile(chatId: string, file: File, fileId: string): Promise<UploadedFile> {
  const form = new FormData();
  form.append("file", file);
  form.append("openwebui_file_id", fileId);
  return call("/v1/pipe/files", { method: "POST", body: form }, chatId);
}

export type ChatBody = {
  messages: { role: "user" | "assistant"; content: string }[];
  files: { openwebui_file_id: string; filename: string }[];
  library_template_id?: string;
  template_document_id?: string;
};

export type DoneEvent = {
  project_id: string;
  mapping_run_id: string | null;
  artifact_id: string | null;
};

export type ChatHandlers = {
  status: (description: string) => void;
  delta: (content: string) => void;
  error: (code: string, message: string) => void;
  done: (d: DoneEvent) => void;
};

/** POST /v1/pipe/chat and dispatch its SSE frames (EventSource cannot POST or send headers). */
export async function chat(chatId: string, body: ChatBody, on: ChatHandlers): Promise<void> {
  const res = await fetch(BASE + "/v1/pipe/chat", {
    ...json(body),
    headers: { ...headers(chatId), "Content-Type": "application/json" },
  });
  if (!res.ok || !res.body) throw await failure(res);
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let end: number;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let event = "";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (!event || !data) continue; // ": keep-alive" comments
      const d = JSON.parse(data);
      if (event === "status") on.status(d.description);
      else if (event === "delta") on.delta(d.content);
      else if (event === "error") on.error(d.code, d.message);
      else if (event === "done") on.done(d);
    }
  }
}

// ---- mapping runs, conflicts ----

export type Artifact = {
  id: string;
  mapping_run_id: string;
  format: string;
  filename: string;
  size_bytes: number;
  download_url: string;
  created_at: string;
};

export type MappingRun = {
  id: string;
  version: number;
  status: string;
  summary: { populated: number; partial: number; missing: number; conflict: number };
  artifact: Artifact | null;
};

export type Candidate = {
  index: number;
  value: unknown;
  source_filename: string;
  page_number: number | null;
  verbatim_quote: string;
};

export type Conflict = {
  field_key: string;
  field_label: string;
  slide_number: number | null;
  section_title: string | null;
  candidates: Candidate[];
};

export type Resolution =
  | { field_key: string; candidate_index: number }
  | { field_key: string; custom_value: string };

export type ResolveResult = {
  mapping_run_id: string;
  version: number;
  job_id: string;
  status: string;
  artifact: Artifact | null;
  remaining_conflicts: Conflict[];
};

const runPath = (projectId: string, runId: string) =>
  `/v1/projects/${projectId}/mapping-runs/${runId}`;

export const getRun = (projectId: string, runId: string) =>
  call<MappingRun>(runPath(projectId, runId));

export const getConflicts = (projectId: string, runId: string) =>
  call<{ items: Conflict[] }>(`${runPath(projectId, runId)}/conflicts`).then((r) => r.items);

export const resolveConflicts = (projectId: string, runId: string, resolutions: Resolution[]) =>
  call<ResolveResult>(`${runPath(projectId, runId)}/resolve-conflicts`, json({ resolutions }));

// ---- template library ----

export type LibraryTemplate = {
  id: string;
  name: string;
  description: string;
  category: string | null;
  format: string;
  filename: string;
  size_bytes: number;
  created_at: string;
  slide_count: number | null;
  field_count: number;
  table_count: number;
  chart_count: number;
  anchor_count: number;
  scan_status: string;
};

export type Scan = {
  scan_id: string;
  name: string;
  status: "scanning" | "ready" | "failed";
  progress: { done: number; total: number; part: string | null } | null;
  template_id: string | null;
  error_code: string | null;
};

export const listTemplates = () =>
  call<{ items: LibraryTemplate[]; scans: Scan[] }>("/v1/library/templates");

export function uploadTemplate(file: File, name: string): Promise<{ scan_id: string }> {
  const form = new FormData();
  form.append("file", file);
  form.append("name", name);
  return call("/v1/library/templates", { method: "POST", body: form });
}

export const getScan = (scanId: string) => call<Scan>(`/v1/library/scans/${scanId}`);

export const deleteTemplate = (id: string) =>
  call<void>(`/v1/library/templates/${id}`, { method: "DELETE" });
