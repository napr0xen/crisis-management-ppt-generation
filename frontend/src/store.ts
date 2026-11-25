// Chat sessions live in the browser: the API keeps one project per chat id, not the messages.
// shortcut: history is per browser (localStorage), store it server-side if users switch devices.
import { useEffect, useState } from "react";
import type { Artifact, Conflict, MappingRun } from "./api";

export type Stage = "ingesting" | "analyzing" | "mapping" | "rendering" | "done";

export type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  files?: string[];
  pending?: boolean;
  status?: string;
  stage?: Stage;
  error?: string;
  projectId?: string;
  runId?: string;
  version?: number;
  summary?: MappingRun["summary"];
  artifact?: Artifact | null;
  conflicts?: Conflict[];
};

export type Session = { id: string; title: string; createdAt: number; messages: Message[] };

const KEY = "docintel.sessions";

export function newSession(): Session {
  return { id: crypto.randomUUID(), title: "New chat", createdAt: Date.now(), messages: [] };
}

export function useSessions() {
  const [sessions, setSessions] = useState<Session[]>(() => {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Session[]) : [];
  });
  useEffect(() => {
    localStorage.setItem(KEY, JSON.stringify(sessions));
  }, [sessions]);
  return [sessions, setSessions] as const;
}

/** The pipeline stage a Pipe status line describes (api/v1/pipe.py status texts). */
export function stageOf(status: string): Stage | undefined {
  if (/^(Parsing|Embedding)/.test(status)) return "ingesting";
  if (/^(Reading template|Analyzing template)/.test(status)) return "analyzing";
  if (/^Mapping/.test(status)) return "mapping";
  if (/^Rendering/.test(status)) return "rendering";
  return undefined;
}
