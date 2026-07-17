"use client";

import { useEffect, useRef, useState } from "react";
import {
  createSession,
  listSessions,
  openSessionSocket,
  type Session,
} from "@/lib/runtime";

interface ChatMsg {
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  toolName?: string;
  pending?: boolean;
}

interface PermissionPrompt {
  tool: string;
  args: unknown;
}

export function ChatPanel() {
  const [session, setSession] = useState<Session | null>(null);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [streamingText, setStreamingText] = useState("");
  const [input, setInput] = useState("");
  const [connected, setConnected] = useState(false);
  const [permission, setPermission] = useState<PermissionPrompt | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  // Auto-pick the most recent session, else create one.
  useEffect(() => {
    (async () => {
      try {
        const sessions = await listSessions();
        let s = sessions[0];
        if (!s) {
          const created = await createSession({
            project_slug: "default",
            workspace:
              (process.env.NEXT_PUBLIC_WORKSPACE as string | undefined) ??
              "/tmp/gw",
          });
          if ((created as { ok?: boolean }).ok === false) {
            setSessionError((created as { error?: string }).error ?? "failed to create session");
            return;
          }
          s = created as Session;
        }
        setSession(s);
      } catch (e) {
        setSessionError(`cannot reach runtime: ${(e as Error).message}`);
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
        handleEvent(ev);
      } catch {
        /* ignore */
      }
    };
    wsRef.current = sock;
    return () => sock.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, streamingText]);

  function handleEvent(ev: { event: string; [k: string]: unknown }) {
    switch (ev.event) {
      case "token.delta":
        setStreamingText((t) => t + (ev.content as string));
        break;
      case "tool.start":
        flushStreaming();
        setMessages((m) => [
          ...m,
          { role: "tool", content: "…", toolName: ev.name as string, pending: true },
        ]);
        break;
      case "tool.end":
        setMessages((m) =>
          m.map((msg, i) =>
            i === m.length - 1 && msg.role === "tool" && msg.pending
              ? { ...msg, content: ev.content as string, pending: false }
              : msg,
          ),
        );
        break;
      case "permission.request":
        setPermission({ tool: ev.tool as string, args: ev.args });
        break;
      case "message.end":
        flushStreaming();
        break;
      case "error":
        flushStreaming();
        setMessages((m) => [...m, { role: "system", content: `[error] ${ev.message}` }]);
        break;
      default:
        break;
    }
  }

  function flushStreaming() {
    setStreamingText((t) => {
      if (t) setMessages((m) => [...m, { role: "assistant", content: t }]);
      return "";
    });
  }

  function send() {
    const text = input.trim();
    const ws = wsRef.current;
    if (!text || !ws || !session) return;
    setMessages((m) => [...m, { role: "user", content: text }]);
    ws.send(JSON.stringify({ type: "invoke", message: text }));
    setInput("");
  }

  function respondPermission(decision: "allow" | "deny") {
    const ws = wsRef.current;
    if (!ws) return;
    ws.send(JSON.stringify({ type: "permission_response", decision }));
    setPermission(null);
  }

  return (
    <div className="flex flex-1 flex-col">
      <div className="flex items-center justify-between border-b border-black/10 px-4 py-2 text-xs text-muted">
        <span>
          {session ? `session: ${session.session_id.slice(0, 8)}` : "no session"}
          {sessionError && <span className="ml-2 text-red-500">— {sessionError}</span>}
        </span>
        <span>{connected ? "● connected" : "○ disconnected"}</span>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-3 font-mono text-sm">
        {messages.length === 0 && !streamingText ? (
          <div className="text-muted">
            Send a message to start. The agent will call tools and may ask for
            permission on destructive operations.
          </div>
        ) : (
          messages.map((m, i) => (
            <div key={i} className="mb-3">
              {m.role === "tool" ? (
                <div className="rounded border border-black/15 bg-black/[0.03] p-2">
                  <div className="mb-1 text-xs text-muted">
                    tool: {m.toolName} {m.pending ? "(running)" : "✓"}
                  </div>
                  <pre className="whitespace-pre-wrap text-xs">{m.content}</pre>
                </div>
              ) : (
                <div>
                  <span className="mr-2 text-muted">{m.role}:</span>
                  <span className="whitespace-pre-wrap">{m.content}</span>
                </div>
              )}
            </div>
          ))
        )}
        {streamingText && (
          <div>
            <span className="mr-2 text-muted">assistant:</span>
            <span className="whitespace-pre-wrap">{streamingText}</span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {permission && (
        <div className="border-t border-yellow-400/40 bg-yellow-50 p-3 text-sm dark:bg-yellow-900/20">
          <div className="mb-2 font-medium">Permission required</div>
          <div className="mb-2 text-xs text-muted">
            tool: <code className="font-mono">{permission.tool}</code>
          </div>
          <pre className="mb-3 max-h-32 overflow-auto rounded bg-black/5 p-2 text-xs">
            {JSON.stringify(permission.args, null, 2)}
          </pre>
          <div className="flex gap-2">
            <button
              onClick={() => respondPermission("allow")}
              className="rounded bg-accent px-3 py-1.5 text-xs font-medium text-white"
            >
              Allow
            </button>
            <button
              onClick={() => respondPermission("deny")}
              className="rounded border border-black/20 px-3 py-1.5 text-xs"
            >
              Deny
            </button>
          </div>
        </div>
      )}

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
          disabled={!wsRef.current || !connected || !!permission}
        >
          Send
        </button>
      </form>
    </div>
  );
}
