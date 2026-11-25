import { useState } from "react";
import { loadSettings } from "./api";
import ChatView from "./components/ChatView";
import SettingsDialog from "./components/SettingsDialog";
import Sidebar from "./components/Sidebar";
import TemplatesPage from "./components/TemplatesPage";
import { newSession, useSessions, type Session } from "./store";

export default function App() {
  const [sessions, setSessions] = useSessions();
  const [activeId, setActiveId] = useState<string | null>(sessions[0]?.id ?? null);
  const [view, setView] = useState<"chat" | "templates">("chat");
  const [settings, setSettings] = useState(() => !loadSettings().token);
  const active = sessions.find((s) => s.id === activeId);

  function startChat() {
    const s = newSession();
    setSessions((all) => [s, ...all]);
    setActiveId(s.id);
    setView("chat");
  }

  const update = (id: string) => (fn: (s: Session) => Session) =>
    setSessions((all) => all.map((s) => (s.id === id ? fn(s) : s)));

  return (
    <div className="flex h-screen text-slate-800">
      <Sidebar
        sessions={sessions}
        activeId={activeId}
        view={view}
        onSelect={(id) => {
          setActiveId(id);
          setView("chat");
        }}
        onNewChat={startChat}
        onTemplates={() => setView("templates")}
        onSettings={() => setSettings(true)}
      />
      <main className="flex-1 overflow-y-auto">
        {view === "templates" ? (
          <TemplatesPage />
        ) : active ? (
          <ChatView key={active.id} session={active} update={update(active.id)} />
        ) : (
          <div className="flex h-full items-center justify-center">
            <button
              onClick={startChat}
              className="rounded-md bg-indigo-600 px-4 py-2 text-white hover:bg-indigo-700"
            >
              Start a new chat
            </button>
          </div>
        )}
      </main>
      {settings && <SettingsDialog onClose={() => setSettings(false)} />}
    </div>
  );
}
