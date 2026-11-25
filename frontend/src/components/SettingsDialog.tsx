import { useState } from "react";
import { loadSettings, saveSettings } from "../api";

export default function SettingsDialog({ onClose }: { onClose: () => void }) {
  const [s, setS] = useState(loadSettings);
  return (
    <div className="fixed inset-0 z-20 flex items-center justify-center bg-slate-900/40">
      <form
        className="w-96 space-y-4 rounded-xl bg-white p-6 shadow-xl"
        onSubmit={(e) => {
          e.preventDefault();
          saveSettings(s);
          onClose();
        }}
      >
        <h2 className="text-lg font-semibold">Connection</h2>
        <label className="block text-sm">
          API token (<code>PIPE_SHARED_SECRET</code>)
          <input
            type="password"
            required
            className="mt-1 w-full rounded border px-2 py-1"
            value={s.token}
            onChange={(e) => setS({ ...s, token: e.target.value })}
          />
        </label>
        <label className="block text-sm">
          User id
          <input
            required
            pattern="[A-Za-z0-9_\-]{1,64}"
            className="mt-1 w-full rounded border px-2 py-1"
            value={s.userId}
            onChange={(e) => setS({ ...s, userId: e.target.value })}
          />
        </label>
        <button className="w-full rounded bg-indigo-600 py-2 text-white hover:bg-indigo-700">
          Save
        </button>
      </form>
    </div>
  );
}
