import { useCallback, useEffect, useState } from "react";
import {
  deleteTemplate,
  getScan,
  listTemplates,
  uploadTemplate,
  type LibraryTemplate,
  type Scan,
} from "../api";

const POLL_MS = 1500;

function scanText(s: Scan): string {
  if (s.status === "failed") return `Scan failed (${s.error_code ?? "error"})`;
  if (s.status === "ready") return "Ready";
  const p = s.progress;
  if (!p || !p.total) return "Scanning…";
  return `Scanning ${p.part ?? ""} (${Math.min(p.done + 1, p.total)}/${p.total})`.replace("  ", " ");
}

export default function TemplatesPage() {
  const [items, setItems] = useState<LibraryTemplate[]>([]);
  const [scans, setScans] = useState<Scan[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [drag, setDrag] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);

  const refresh = useCallback(
    () =>
      listTemplates()
        .then((r) => {
          setItems(r.items);
          setScans(r.scans);
        })
        .catch((e) => setError((e as Error).message)),
    [],
  );

  useEffect(() => {
    refresh();
  }, [refresh]);

  // live scan status: poll every running scan, refresh the list when one finishes
  const runningKey = scans
    .filter((s) => s.status === "scanning")
    .map((s) => s.scan_id)
    .join(",");
  useEffect(() => {
    if (!runningKey) return;
    const running = runningKey.split(",");
    const timer = setInterval(async () => {
      const latest = await Promise.all(running.map((id) => getScan(id).catch(() => null)));
      if (latest.some((s) => s && s.status !== "scanning")) refresh();
      else setScans((all) => all.map((s) => latest.find((l) => l?.scan_id === s.scan_id) ?? s));
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [runningKey, refresh]);

  function pick(f: File | undefined) {
    setError(null);
    if (!f) return;
    if (!f.name.toLowerCase().endsWith(".pptx")) {
      setError("Only .pptx templates can be uploaded here.");
      return;
    }
    setFile(f);
    setName(name || f.name.replace(/\.pptx$/i, ""));
  }

  async function upload() {
    if (!file) return;
    setError(null);
    try {
      const { scan_id } = await uploadTemplate(file, name.trim());
      setFile(null);
      setName("");
      setScans((s) => [
        { scan_id, name, status: "scanning", progress: null, template_id: null, error_code: null },
        ...s,
      ]);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function remove(id: string) {
    setConfirm(null);
    try {
      await deleteTemplate(id);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="mx-auto max-w-4xl space-y-8 p-8">
      <h1 className="text-2xl font-semibold text-slate-900">Templates</h1>

      <section className="space-y-3 rounded-xl bg-white p-6 shadow-sm">
        <label
          onDragOver={(e) => {
            e.preventDefault();
            setDrag(true);
          }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDrag(false);
            pick(e.dataTransfer.files[0]);
          }}
          className={`flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed p-8 text-sm ${
            drag ? "border-indigo-500 bg-indigo-50" : "border-slate-300 text-slate-500"
          }`}
        >
          <input
            type="file"
            accept=".pptx"
            hidden
            onChange={(e) => {
              pick(e.target.files?.[0]);
              e.target.value = "";
            }}
          />
          {file ? (
            <span className="font-medium text-slate-800">{file.name}</span>
          ) : (
            <span>Drop a .pptx template here, or click to choose one</span>
          )}
        </label>
        <div className="flex gap-2">
          <input
            aria-label="Template name"
            placeholder="Template name"
            maxLength={100}
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="flex-1 rounded-md border px-3 py-2 text-sm"
          />
          <button
            disabled={!file || !name.trim()}
            onClick={upload}
            className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            Upload & scan
          </button>
        </div>
        {error && <div className="text-sm text-red-700">{error}</div>}
      </section>

      {scans.length > 0 && (
        <section className="space-y-2">
          {scans.map((s) => (
            <div
              key={s.scan_id}
              className="flex items-center justify-between rounded-lg bg-white px-4 py-3 text-sm shadow-sm"
            >
              <span className="font-medium">{s.name}</span>
              <span
                className={
                  s.status === "failed" ? "text-red-700" : "animate-pulse text-indigo-700"
                }
              >
                {scanText(s)}
              </span>
            </div>
          ))}
        </section>
      )}

      <section className="overflow-hidden rounded-xl bg-white shadow-sm">
        <table className="w-full text-sm">
          <thead className="bg-slate-100 text-left text-xs uppercase text-slate-500">
            <tr>
              <th className="px-4 py-2">Name</th>
              <th className="px-4 py-2">Slides</th>
              <th className="px-4 py-2">Fields</th>
              <th className="px-4 py-2">Tables</th>
              <th className="px-4 py-2">Charts</th>
              <th className="px-4 py-2">Status</th>
              <th className="px-4 py-2" />
            </tr>
          </thead>
          <tbody>
            {items.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-6 text-center text-slate-500">
                  No templates yet.
                </td>
              </tr>
            )}
            {items.map((t) => (
              <tr key={t.id} className="border-t">
                <td className="px-4 py-2">
                  <div className="font-medium text-slate-900">{t.name}</div>
                  <div className="text-xs text-slate-500">{t.filename}</div>
                </td>
                <td className="px-4 py-2">{t.slide_count ?? "-"}</td>
                <td className="px-4 py-2">{t.field_count}</td>
                <td className="px-4 py-2">{t.table_count}</td>
                <td className="px-4 py-2">{t.chart_count}</td>
                <td className="px-4 py-2 text-emerald-700">{t.scan_status}</td>
                <td className="px-4 py-2 text-right">
                  {confirm === t.id ? (
                    <span className="space-x-2">
                      <span className="text-slate-600">Delete?</span>
                      <button onClick={() => remove(t.id)} className="font-medium text-red-700">
                        Yes
                      </button>
                      <button onClick={() => setConfirm(null)} className="text-slate-500">
                        No
                      </button>
                    </span>
                  ) : (
                    <button
                      onClick={() => setConfirm(t.id)}
                      className="rounded border border-red-200 px-2 py-1 text-red-700 hover:bg-red-50"
                    >
                      Delete
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
