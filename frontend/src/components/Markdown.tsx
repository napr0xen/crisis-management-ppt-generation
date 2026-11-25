import type { ReactNode } from "react";

// shortcut: renders only links and **bold** (what the Pipe replies use), add a markdown library
// if replies grow tables or headings.
const TOKEN = /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)|\*\*([^*]+)\*\*/g;

export default function Markdown({ text }: { text: string }) {
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(TOKEN)) {
    out.push(text.slice(last, m.index));
    out.push(
      m[2] ? (
        <a
          key={m.index}
          href={m[2]}
          className="text-indigo-600 underline"
          target="_blank"
          rel="noreferrer"
        >
          {m[1]}
        </a>
      ) : (
        <strong key={m.index}>{m[3]}</strong>
      ),
    );
    last = m.index + m[0].length;
  }
  out.push(text.slice(last));
  return <div className="whitespace-pre-wrap break-words">{out}</div>;
}
