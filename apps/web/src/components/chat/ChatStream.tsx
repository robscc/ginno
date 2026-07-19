"use client";

import { useEffect, useRef, useState } from "react";
import { Paperclip, Keyboard, ArrowUp } from "lucide-react";
import { useGinno } from "@/lib/store";
import { openSessionSocket } from "@/lib/runtime";
import { agentHex } from "@/lib/theme";
import { Icon } from "@/components/icons";
import type { AgentConfig, SessionMeta } from "@/lib/types";

interface ChatMsg {
  id: string;
  role: "user" | "assistant" | "tool";
  content: string;
  agentId?: string | null;
  toolName?: string;
  toolCallId?: string;
  pending?: boolean;
}

interface PermissionPrompt {
  tool: string;
  args: unknown;
}

let _mid = 0;
const mid = () => `m${++_mid}`;

export function ChatStream({
  session,
  onRunningChange,
}: {
  session: SessionMeta | null;
  onRunningChange?: (b: boolean) => void;
}) {
  const g = useGinno();
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [stream, setStream] = useState("");
  const [streamAgent, setStreamAgent] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [target, setTarget] = useState<string | null>(null);
  const [permission, setPermission] = useState<PermissionPrompt | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const streamRef = useRef("");
  const streamAgentRef = useRef<string | null>(null);
  useEffect(() => {
    streamRef.current = stream;
  }, [stream]);
  useEffect(() => {
    streamAgentRef.current = streamAgent;
  }, [streamAgent]);

  // reset on session change
  useEffect(() => {
    setMessages([]);
    setStream("");
    setStreamAgent(null);
    setPermission(null);
  }, [session?.id]);

  // websocket with auto-reconnect (so a sidecar restart or the packaged
  // app's startup race doesn't leave the chat permanently disconnected)
  useEffect(() => {
    if (!session) return;
    let closed = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let sock: WebSocket | null = null;

    const connect = () => {
      sock = openSessionSocket(session.id);
      wsRef.current = sock;
      sock.onopen = () => g.setConnected(true);
      sock.onmessage = (e) => {
        try {
          handle(JSON.parse(e.data));
        } catch {
          /* ignore */
        }
      };
      sock.onerror = () => {
        try {
          sock?.close();
        } catch {
          /* ignore */
        }
      };
      sock.onclose = () => {
        g.setConnected(false);
        if (!closed) timer = setTimeout(connect, 1500);
      };
    };

    connect();
    return () => {
      closed = true;
      if (timer) clearTimeout(timer);
      try {
        sock?.close();
      } catch {
        /* ignore */
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.id]);

  const running = !!stream || !!permission || messages.some((m) => m.pending);
  useEffect(() => {
    onRunningChange?.(running);
  }, [running, onRunningChange]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, stream]);

  function flushStream() {
    const text = streamRef.current;
    const ag = streamAgentRef.current;
    setStream("");
    if (text) setMessages((m) => [...m, { id: mid(), role: "assistant", content: text, agentId: ag }]);
  }

  function handle(ev: { event: string; [k: string]: unknown }) {
    switch (ev.event) {
      case "token.delta":
        setStream((t) => t + (ev.content as string));
        break;
      case "tool.start":
        flushStream();
        setMessages((m) => [
          ...m,
          {
            id: mid(),
            role: "tool",
            content: "…",
            toolName: ev.name as string,
            toolCallId: ev.id as string | undefined,
            pending: true,
          },
        ]);
        break;
      case "tool.end": {
        const id = ev.id as string | undefined;
        const name = ev.name as string | undefined;
        setMessages((m) => {
          let found = false;
          return m.map((msg) => {
            const matches =
              msg.role === "tool" &&
              msg.pending &&
              (id ? msg.toolCallId === id : name ? msg.toolName === name : true);
            if (!found && matches) {
              found = true;
              return { ...msg, content: ev.content as string, pending: false };
            }
            return msg;
          });
        });
        break;
      }
      case "permission.request":
        setPermission({ tool: ev.tool as string, args: ev.args });
        break;
      case "message.end":
        flushStream();
        setStreamAgent(null);
        break;
      case "error":
        flushStream();
        setMessages((m) => [...m, { id: mid(), role: "assistant", content: `[error] ${ev.message}` }]);
        break;
    }
  }

  function send() {
    const text = input.trim();
    const ws = wsRef.current;
    if (!text || !ws || !session) return;
    const agentId = target ?? session.agent_id ?? g.agents[0]?.id ?? null;
    if (agentId && agentId !== session.agent_id) g.setSessionAgent(session.id, agentId);
    setMessages((m) => [...m, { id: mid(), role: "user", content: text }]);
    setStreamAgent(agentId);
    ws.send(JSON.stringify({ type: "invoke", message: text, agent_id: agentId }));
    setInput("");
    setTarget(null);
  }

  function respond(decision: "allow" | "deny") {
    wsRef.current?.send(JSON.stringify({ type: "permission_response", decision }));
    setPermission(null);
  }

  const agentById = (id?: string | null) => g.agents.find((a) => a.id === id) ?? null;
  const targetAgent = agentById(target ?? session?.agent_id);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto flex max-w-3xl flex-col gap-5">
          {messages.length === 0 && !stream && (
            <div className="py-16 text-center text-sm text-faint">
              Start a conversation. The agent will use tools and may ask for permission.
            </div>
          )}

          {messages.map((m) =>
            m.role === "user" ? (
              <div key={m.id} className="flex justify-end">
                <div className="max-w-[78%] rounded-2xl bg-card2 px-4 py-2.5 text-sm leading-relaxed text-txt">
                  {m.content}
                </div>
              </div>
            ) : m.role === "tool" ? (
              <div key={m.id} className="flex">
                <div className="w-full rounded-xl border border-line bg-card/60 px-3 py-2 font-mono text-xs text-muted">
                  <span className="text-faint">tool · {m.toolName}</span>{" "}
                  {m.pending ? <span className="text-yellow">running…</span> : <span className="text-green">✓</span>}
                  {!m.pending && <pre className="mt-1 whitespace-pre-wrap text-faint">{m.content}</pre>}
                </div>
              </div>
            ) : (
              <AssistantBubble key={m.id} agent={agentById(m.agentId)} text={m.content} />
            ),
          )}

          {stream && <AssistantBubble agent={agentById(streamAgent)} text={stream} streaming />}
          {!stream && running && messages[messages.length - 1]?.role === "user" && (
            <AssistantBubble agent={targetAgent} text="" streaming />
          )}

          <div ref={bottomRef} />
        </div>
      </div>

      {permission && (
        <div className="mx-auto w-full max-w-3xl px-6">
          <div className="mb-2 rounded-xl border border-yellow/40 bg-yellow/10 p-3">
            <div className="mb-1 text-sm font-medium text-yellow">Permission required</div>
            <div className="mb-2 text-xs text-muted">
              tool: <code className="font-mono text-txt">{permission.tool}</code>
            </div>
            <pre className="mb-3 max-h-28 overflow-auto rounded-lg bg-base/60 p-2 text-[11px] text-muted">
              {JSON.stringify(permission.args, null, 2)}
            </pre>
            <div className="flex gap-2">
              <button
                onClick={() => respond("allow")}
                className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90"
              >
                Allow
              </button>
              <button
                onClick={() => respond("deny")}
                className="rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:text-txt"
              >
                Deny
              </button>
            </div>
          </div>
        </div>
      )}

      {/* composer */}
      <div className="px-6 pb-5 pt-2">
        <div className="mx-auto max-w-3xl">
          <div className="mb-2 flex flex-wrap gap-2">
            {g.agents.map((a) => {
              const sel = (target ?? session?.agent_id) === a.id;
              const hex = agentHex(a.color);
              return (
                <button
                  key={a.id}
                  onClick={() => setTarget(sel ? null : a.id)}
                  className="flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs transition-colors"
                  style={{
                    borderColor: sel ? hex : "#262632",
                    background: sel ? hex + "1a" : "#15151d",
                    color: sel ? hex : "#9a9aa6",
                  }}
                >
                  <Icon name={a.icon} className="h-3.5 w-3.5" />
                  Ask {a.name}
                </button>
              );
            })}
            <button
              onClick={() => g.newSession()}
              className="flex items-center gap-1.5 rounded-lg border border-line bg-card px-2.5 py-1 text-xs text-muted hover:text-txt"
            >
              + New Session
            </button>
          </div>

          <div className="rounded-2xl border border-line bg-card p-2.5 focus-within:border-line2">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
              rows={2}
              placeholder="Ask any Agent …"
              className="w-full resize-none bg-transparent px-1.5 py-1 text-sm text-txt outline-none placeholder:text-faint"
            />
            <div className="flex items-center justify-between px-1 pt-1">
              <div className="flex items-center gap-1 text-faint">
                <button className="rounded-md p-1.5 hover:bg-card2 hover:text-muted" title="Attach">
                  <Paperclip className="h-4 w-4" />
                </button>
                <button className="rounded-md p-1.5 hover:bg-card2 hover:text-muted" title="Shortcuts">
                  <Keyboard className="h-4 w-4" />
                </button>
                <span className="px-1 text-xs">/</span>
              </div>
              <button
                onClick={send}
                disabled={!g.connected || !!permission || !input.trim()}
                className="flex h-8 w-8 items-center justify-center rounded-lg bg-violet text-white transition-opacity hover:opacity-90 disabled:opacity-40"
              >
                <ArrowUp className="h-4 w-4" />
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function AssistantBubble({
  agent,
  text,
  streaming,
}: {
  agent: AgentConfig | null;
  text: string;
  streaming?: boolean;
}) {
  const hex = agentHex(agent?.color);
  return (
    <div className="flex gap-3">
      <div
        className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg"
        style={{ background: hex + "22", color: hex }}
      >
        <Icon name={agent?.icon || "terminal"} className="h-4 w-4" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="mb-1 flex items-center gap-2 text-sm">
          <span className="font-medium text-txt">{agent?.name || "Agent"}</span>
          <span className="text-xs text-faint">{streaming ? "thinking…" : "just now"}</span>
        </div>
        <div className="rounded-xl border border-line bg-card px-4 py-3 text-sm leading-relaxed text-txt">
          {text ? (
            <span className="whitespace-pre-wrap">
              {text}
              {streaming && <span className="ml-0.5 inline-block h-3.5 w-1.5 translate-y-0.5 animate-pulse bg-violet" />}
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 text-muted">
              <span className="flex gap-0.5">
                <Dot /> <Dot d={150} /> <Dot d={300} />
              </span>
              <span className="ml-1 text-xs">Thinking…</span>
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

function Dot({ d = 0 }: { d?: number }) {
  return (
    <span
      className="h-1.5 w-1.5 animate-pulse rounded-full bg-muted"
      style={{ animationDelay: `${d}ms` }}
    />
  );
}
