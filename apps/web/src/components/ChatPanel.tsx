"use client";

import { useEffect, useState } from "react";
import {
  createSession,
  listSessions,
  openSessionSocket,
  type Session,
} from "@/lib/runtime";

interface Msg {
  role: "user" | "assistant" | "system";
  content: string;
}

export function ChatPanel() {
  const [session, setSession] = useState<Session | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [connected, setConnected] = useState(false);
  const [ws, setWs] = useState<WebSocket | null>(null);

  // Auto-pick the most recent session, else create one.
  useEffect(() => {
    (async () => {
      try {
        const sessions = await listSessions();
        const s =
          sessions[0] ??
          (await createSession({
            project_slug: "default",
            workspace: `${process.env.HOME ?? ""}/workspace/default`,
          }));
        setSession(s);
      } catch {
        // sidecar not up yet — UI shows disconnected state.
      }
    })();
  }, []);

  // Open WS when session changes.
  useEffect(() => {
    if (!session) return;
    const sock = openSessionSocket(session.session_id);
    sock.onopen = () => setConnected(true);
    sock.onclose = () => setConnected(false);
    sock.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data);
        if (ev.event === "echo" || ev.event === "info") {
          setMessages((m) => [...m, { role: "system", content: ev.message ?? "" }]);
        }
      } catch {
        /* ignore */
      }
    };
    setWs(sock);
    return () => sock.close();
  }, [session]);

  function send() {
    const text = input.trim();
    if (!text || !ws) return;
    setMessages((m) => [...m, { role: "user", content: text }]);
    ws.send(text);
    setInput("");
  }

  return (
    <div className="flex flex-1 flex-col">
      <div className="flex items-center justify-between border-b border-black/10 px-4 py-2 text-xs text-muted">
        <span>
          {session ? `session: ${session.session_id.slice(0, 8)}` : "no session"}
        </span>
        <span>{connected ? "● connected" : "○ disconnected"}</span>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-3 font-mono text-sm">
        {messages.length === 0 ? (
          <div className="text-muted">
            Send a message to start. WS streaming lands in P1.
          </div>
        ) : (
          messages.map((m, i) => (
            <div key={i} className="mb-2">
              <span className="mr-2 text-muted">{m.role}:</span>
              <span>{m.content}</span>
            </div>
          ))
        )}
      </div>

      <form
        className="flex gap-2 border-t border-black/10 p-3"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <input
          className="flex-1 rounded border border-black/15 bg-transparent px-3 py-2 outline-none focus:border-accent"
          placeholder="Message Ginno…  (use /<skill> for skills)"
          value={input}
          onChange={(e) => setInput(e.target.value)}
        />
        <button
          type="submit"
          className="rounded bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
          disabled={!ws || !connected}
        >
          Send
        </button>
      </form>
    </div>
  );
}
