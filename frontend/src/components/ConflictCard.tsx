import { useState } from "react";
import { resolveConflicts, type Conflict, type ResolveResult, type Resolution } from "../api";

type Choice = { index: number } | { custom: string };

export function showValue(value: unknown): string {
  if (Array.isArray(value)) {
    return value.map((v) => (Array.isArray(v) ? v.join(" | ") : String(v))).join("\n");
  }
  return String(value);
}

function where(c: Conflict): string {
  return [c.slide_number != null && `Slide ${c.slide_number}`, c.section_title, c.field_label]
    .filter(Boolean)
    .join(" · ");
}

type Props = {
  projectId: string;
  runId: string;
  conflicts: Conflict[];
  onResolved: (r: ResolveResult) => void;
};

export default function ConflictCard({ projectId, runId, conflicts, onResolved }: Props) {
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const resolutions = Object.entries(choices).flatMap(([field_key, c]): Resolution[] =>
    "index" in c
      ? [{ field_key, candidate_index: c.index }]
      : c.custom.trim()
        ? [{ field_key, custom_value: c.custom.trim() }]
        : [],
  );

  async function apply() {
    setBusy(true);
    setError(null);
    try {
      const result = await resolveConflicts(projectId, runId, resolutions);
      setChoices({});
      onResolved(result);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-4">
      <div className="mb-3 text-sm font-semibold text-amber-900">
        {conflicts.length} field{conflicts.length > 1 ? "s" : ""} with conflicting sources: pick
        the right value
      </div>
      <div className="space-y-4">
        {conflicts.map((c) => {
          const choice = choices[c.field_key];
          const set = (next: Choice) => setChoices({ ...choices, [c.field_key]: next });
          return (
            <fieldset key={c.field_key} className="rounded-md bg-white p-3 shadow-sm">
              <legend className="px-1 text-sm font-medium text-slate-800">{where(c)}</legend>
              <div className="grid gap-2 sm:grid-cols-2">
                {c.candidates.map((cand) => {
                  const on = choice && "index" in choice && choice.index === cand.index;
                  return (
                    <button
                      key={cand.index}
                      type="button"
                      aria-pressed={!!on}
                      onClick={() => set({ index: cand.index })}
                      className={`rounded-md border p-2 text-left text-sm transition ${
                        on
                          ? "border-indigo-600 bg-indigo-50 ring-2 ring-indigo-600"
                          : "border-slate-200 hover:border-indigo-400"
                      }`}
                    >
                      <div className="whitespace-pre-wrap font-semibold text-slate-900">
                        {showValue(cand.value)}
                      </div>
                      <div className="mt-1 text-xs text-slate-500">
                        {cand.source_filename}
                        {cand.page_number != null && `, page ${cand.page_number}`}
                      </div>
                      <blockquote className="mt-1 border-l-2 border-slate-300 pl-2 text-xs italic text-slate-600">
                        “{cand.verbatim_quote}”
                      </blockquote>
                    </button>
                  );
                })}
              </div>
              <input
                aria-label={`Custom value for ${c.field_label}`}
                placeholder="Or type a custom value"
                className="mt-2 w-full rounded border px-2 py-1 text-sm"
                value={choice && "custom" in choice ? choice.custom : ""}
                onChange={(e) => set({ custom: e.target.value })}
              />
            </fieldset>
          );
        })}
      </div>
      {error && <div className="mt-2 text-sm text-red-700">{error}</div>}
      <button
        type="button"
        disabled={busy || resolutions.length === 0}
        onClick={apply}
        className="mt-3 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
      >
        {busy ? "Re-rendering…" : "Apply & Re-render PPTX"}
      </button>
    </div>
  );
}
