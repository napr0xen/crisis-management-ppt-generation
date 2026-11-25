import { useState } from "react";
import type { Session } from "../store";

type Props = {
  sessions: Session[];
  activeId: string | null;
  view: "chat" | "templates";
  onSelect: (id: string) => void;
  onNewChat: () => void;
  onTemplates: () => void;
  onSettings: () => void;
};

export default function Sidebar(p: Props) {
  const [menu, setMenu] = useState(false);
  const items: [string, () => void][] = [
    ["New chat", p.onNewChat],
    ["Templates", p.onTemplates],
  ];
  return (
    <aside className="flex w-64 shrink-0 flex-col border-r bg-slate-900 text-slate-100">
      <div className="px-4 py-4 text-lg font-semibold">Doc Intel</div>
      <div className="relative flex items-center justify-between px-4 pb-2 text-xs uppercase tracking-wide text-slate-400">
        <span>Chats</span>
        <button
          aria-label="New chat or templates"
          aria-expanded={menu}
          className="rounded px-2 text-lg leading-none text-slate-200 hover:bg-slate-700"
          onClick={() => setMenu(!menu)}
        >
          +
        </button>
        {menu && (
          <div className="absolute right-4 top-6 z-10 w-40 rounded-md bg-white py-1 text-sm normal-case tracking-normal text-slate-800 shadow-lg">
            {items.map(([label, action]) => (
              <button
                key={label}
                className="block w-full px-3 py-1.5 text-left hover:bg-slate-100"
                onClick={() => {
                  setMenu(false);
                  action();
                }}
              >
                {label}
              </button>
            ))}
          </div>
        )}
      </div>
      <nav className="flex-1 space-y-0.5 overflow-y-auto px-2">
        {p.sessions.map((s) => (
          <button
            key={s.id}
            onClick={() => p.onSelect(s.id)}
            className={`block w-full truncate rounded px-3 py-2 text-left text-sm ${
              p.view === "chat" && s.id === p.activeId ? "bg-slate-700" : "hover:bg-slate-800"
            }`}
          >
            {s.title}
          </button>
        ))}
      </nav>
      <div className="space-y-1 border-t border-slate-700 p-2 text-sm">
        <button
          onClick={p.onTemplates}
          className={`block w-full rounded px-3 py-2 text-left ${
            p.view === "templates" ? "bg-slate-700" : "hover:bg-slate-800"
          }`}
        >
          Templates
        </button>
        <button
          onClick={p.onSettings}
          className="block w-full rounded px-3 py-2 text-left hover:bg-slate-800"
        >
          Settings
        </button>
      </div>
    </aside>
  );
}
