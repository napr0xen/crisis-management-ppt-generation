import { useEffect, useRef, useState } from "react";
import {
  chat,
  getConflicts,
  getRun,
  listTemplates,
  uploadFile,
  type Artifact,
  type ChatBody,
  type LibraryTemplate,
  type ResolveResult,
} from "../api";
import { stageOf, type Message, type Session, type Stage } from "../store";
import ConflictCard from "./ConflictCard";
import Markdown from "./Markdown";

const ACCEPT = ".pdf,.docx,.xlsx,.pptx,.csv,.txt";
const STEPS: [Stage, string][] = [
  ["ingesting", "Ingesting"],
  ["analyzing", "Analyzing template"],
  ["mapping", "Mapping"],
  ["rendering", "Rendering"],
];
const MAX_MESSAGES = 50; // api: pipe/chat takes 1..50 messages

type Props = {
  session: Session;
  update: (fn: (s: Session) => Session) => void;
};

function Progress({ stage, status }: { stage?: Stage; status?: string }) {
  const at = stage === "done" ? STEPS.length : STEPS.findIndex(([s]) => s === stage);
  return (
    <div className="mt-2">
      <ol className="flex flex-wrap gap-2 text-xs">
        {STEPS.map(([s, label], i) => (
          <li
            key={s}
            className={`rounded-full px-2 py-0.5 ${
              i < at
                ? "bg-emerald-100 text-emerald-800"
                : i === at
                  ? "animate-pulse bg-indigo-100 text-indigo-800"
                  : "bg-slate-100 text-slate-400"
            }`}
          >
            {label}
          </li>
        ))}
      </ol>
      {status && <div className="mt-1 text-xs text-slate-500">{status}</div>}
    </div>
  );
}

function DownloadCard({ artifact, version }: { artifact: Artifact; version?: number }) {
  return (
    <a
      href={artifact.download_url}
      className="mt-3 flex items-center gap-3 rounded-lg border border-emerald-300 bg-emerald-50 p-3 hover:bg-emerald-100"
    >
      <span className="rounded bg-emerald-600 px-2 py-1 text-xs font-bold uppercase text-white">
        {artifact.format}
      </span>
      <span className="flex-1">
        <span className="block text-sm font-medium text-slate-900">{artifact.filename}</span>
        <span className="block text-xs text-slate-500">
          {(artifact.size_bytes / 1024).toFixed(0)} KB{version ? ` · version ${version}` : ""}
        </span>
      </span>
      <span className="text-sm font-medium text-emerald-700">Download</span>
    </a>
  );
}

export default function ChatView({ session, update }: Props) {
  const [text, setText] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [template, setTemplate] = useState("");
  const [library, setLibrary] = useState<LibraryTemplate[]>([]);
  const fileInput = useRef<HTMLInputElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const busy = session.messages.some((m) => m.pending);

  useEffect(() => {
    listTemplates()
      .then((r) => setLibrary(r.items))
      .catch(() => setLibrary([]));
  }, []);
  useEffect(() => bottom.current?.scrollIntoView({ behavior: "smooth" }), [session.messages]);

  const patch = (id: string, p: Partial<Message> | ((m: Message) => Partial<Message>)) =>
    update((s) => ({
      ...s,
      messages: s.messages.map((m) =>
        m.id === id ? { ...m, ...(typeof p === "function" ? p(m) : p) } : m,
      ),
    }));

  async function send() {
    const chatId = session.id;
    const attached = files;
    const pptx = attached.filter((f) => f.name.toLowerCase().endsWith(".pptx"));
    const content =
      text.trim() ||
      (template || pptx.length
        ? "Fill the template using the attached sources."
        : "Summarize the attached documents.");
    const user: Message = {
      id: crypto.randomUUID(),
      role: "user",
      content,
      files: attached.map((f) => f.name),
    };
    const reply: Message = { id: crypto.randomUUID(), role: "assistant", content: "", pending: true };
    const history = [...session.messages, user]
      .filter((m) => m.content)
      .slice(-MAX_MESSAGES)
      .map(({ role, content }) => ({ role, content }));
    update((s) => ({
      ...s,
      title: s.messages.length ? s.title : content.slice(0, 60),
      messages: [...s.messages, user, reply],
    }));
    setText("");
    setFiles([]);
    const chosen = template;
    setTemplate("");

    try {
      const body: ChatBody = { messages: history, files: [] };
      const notes: string[] = [];
      for (const [i, f] of attached.entries()) {
        patch(reply.id, { status: `Uploading ${f.name} (${i + 1}/${attached.length})` });
        const fileId = crypto.randomUUID();
        try {
          const up = await uploadFile(chatId, f, fileId);
          body.files.push({ openwebui_file_id: fileId, filename: f.name });
          if (chosen === `att:${f.name}`) body.template_document_id = up.document_id;
        } catch (e) {
          notes.push(`Could not upload ${f.name}: ${(e as Error).message}`);
        }
      }
      if (chosen.startsWith("lib:")) body.library_template_id = chosen.slice(4);
      if (notes.length) patch(reply.id, { content: notes.join("\n") + "\n\n" });

      let done: { project_id: string; mapping_run_id: string | null; artifact_id: string | null } =
        { project_id: "", mapping_run_id: null, artifact_id: null };
      await chat(chatId, body, {
        status: (d) => patch(reply.id, (m) => ({ status: d, stage: stageOf(d) ?? m.stage })),
        delta: (c) => patch(reply.id, (m) => ({ content: m.content + c + "\n\n" })),
        error: (_code, message) => patch(reply.id, { error: message }),
        done: (d) => (done = d),
      });
      const final: Partial<Message> = { pending: false, status: undefined };
      if (done.mapping_run_id && done.artifact_id) {
        const [run, conflicts] = await Promise.all([
          getRun(done.project_id, done.mapping_run_id),
          getConflicts(done.project_id, done.mapping_run_id),
        ]);
        Object.assign(final, {
          stage: "done",
          projectId: done.project_id,
          runId: run.id,
          summary: run.summary,
          artifact: run.artifact,
          conflicts,
        });
      }
      patch(reply.id, final);
    } catch (e) {
      patch(reply.id, { pending: false, status: undefined, error: (e as Error).message });
    }
  }

  async function resolved(msg: Message, r: ResolveResult) {
    patch(msg.id, {
      runId: r.mapping_run_id,
      version: r.version,
      conflicts: r.remaining_conflicts,
      artifact: r.artifact,
    });
    // the render did not finish within the API's wait: poll the run until its artifact exists
    for (let i = 0; !r.artifact && i < 120; i++) {
      await new Promise((ok) => setTimeout(ok, 1000));
      const run = await getRun(msg.projectId!, r.mapping_run_id).catch(() => null);
      if (run?.status === "failed") {
        patch(msg.id, { error: "Re-rendering failed." });
        return;
      }
      if (run?.artifact) {
        patch(msg.id, { artifact: run.artifact });
        return;
      }
    }
  }

  const pptxAttached = files.filter((f) => f.name.toLowerCase().endsWith(".pptx"));

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 space-y-4 overflow-y-auto p-6">
        {session.messages.length === 0 && (
          <div className="mx-auto mt-24 max-w-md text-center text-slate-500">
            Attach source documents (PDF, DOCX, XLSX) and pick a template to generate a filled
            deck, or ask a question about your documents.
          </div>
        )}
        {session.messages.map((m) => (
          <div key={m.id} className={m.role === "user" ? "flex justify-end" : ""}>
            <div
              className={`max-w-3xl rounded-xl px-4 py-3 text-sm ${
                m.role === "user" ? "bg-indigo-600 text-white" : "bg-white shadow-sm"
              }`}
            >
              {m.content && <Markdown text={m.content.trim()} />}
              {!!m.files?.length && (
                <div className="mt-2 flex flex-wrap gap-1">
                  {m.files.map((f) => (
                    <span key={f} className="rounded bg-indigo-500 px-2 py-0.5 text-xs">
                      {f}
                    </span>
                  ))}
                </div>
              )}
              {m.pending && <Progress stage={m.stage} status={m.status} />}
              {m.error && <div className="mt-2 text-red-700">{m.error}</div>}
              {m.artifact && <DownloadCard artifact={m.artifact} version={m.version} />}
              {!!m.conflicts?.length && m.projectId && m.runId && (
                <ConflictCard
                  key={m.runId}
                  projectId={m.projectId}
                  runId={m.runId}
                  conflicts={m.conflicts}
                  onResolved={(r) => resolved(m, r)}
                />
              )}
            </div>
          </div>
        ))}
        <div ref={bottom} />
      </div>

      <form
        className="border-t bg-white p-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (!busy && (text.trim() || files.length || template)) send();
        }}
      >
        {!!files.length && (
          <div className="mb-2 flex flex-wrap gap-2">
            {files.map((f) => (
              <span
                key={f.name}
                className="flex items-center gap-1 rounded-full bg-slate-100 px-3 py-1 text-xs"
              >
                {f.name}
                <button
                  type="button"
                  aria-label={`Remove ${f.name}`}
                  className="text-slate-400 hover:text-slate-700"
                  onClick={() => {
                    setFiles(files.filter((x) => x !== f));
                    if (template === `att:${f.name}`) setTemplate("");
                  }}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}
        <div className="flex items-end gap-2">
          <input
            ref={fileInput}
            type="file"
            multiple
            accept={ACCEPT}
            hidden
            onChange={(e) => {
              const picked = Array.from(e.target.files ?? []);
              setFiles([...files, ...picked.filter((p) => !files.some((f) => f.name === p.name))]);
              e.target.value = "";
            }}
          />
          <button
            type="button"
            onClick={() => fileInput.current?.click()}
            className="rounded-md border px-3 py-2 text-sm hover:bg-slate-50"
          >
            Attach
          </button>
          <select
            aria-label="Template"
            value={template}
            onChange={(e) => setTemplate(e.target.value)}
            className="max-w-56 rounded-md border px-2 py-2 text-sm"
          >
            <option value="">Template: auto</option>
            {library.length > 0 && (
              <optgroup label="Library">
                {library.map((t) => (
                  <option key={t.id} value={`lib:${t.id}`}>
                    {t.name}
                  </option>
                ))}
              </optgroup>
            )}
            {pptxAttached.length > 0 && (
              <optgroup label="Attached">
                {pptxAttached.map((f) => (
                  <option key={f.name} value={`att:${f.name}`}>
                    {f.name}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
          <textarea
            rows={1}
            value={text}
            placeholder="Ask a question or describe the deck to generate"
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                e.currentTarget.form?.requestSubmit();
              }
            }}
            className="flex-1 resize-none rounded-md border px-3 py-2 text-sm"
          />
          <button
            disabled={busy}
            className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            Send
          </button>
        </div>
      </form>
    </div>
  );
}
