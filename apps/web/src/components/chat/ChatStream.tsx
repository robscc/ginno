"use client";

import { useEffect, useRef, useState } from "react";
import { Paperclip, Keyboard, ArrowUp } from "lucide-react";
import { useGinno } from "@/lib/store";
import { openSessionSocket } from "@/lib/runtime";
import { agentHex } from "@/lib/theme";
import { Icon } from "@/components/icons";
import { InnerBlocks, RefBlocks, hasPendingTool, type Block } from "@/components/chat/blocks";
import type { AgentConfig, SessionMeta } from "@/lib/types";

interface ChatMsg {
  id: string;
  role: "user" | "assistant";
  blocks: Block[];
  agentId?: string | null;
  agentName?: string;
}

interface PermissionPrompt {
  tool: string;
  args: unknown;
}

let _mid = 0;
const mid = () => `m${++_mid}`;

function applyBlock(blocks: Block[], ev: { event: string; [k: string]: unknown }): Block[] {
  const last = blocks[blocks.length - 1];
  switch (ev.event) {
    case "token.delta": {
      const t = (ev.content as string) || "";
      if (last && last.kind === "text") {
        const next = blocks.slice();
        next[next.length - 1] = { kind: "text", text: last.text + t };
        return next;
      }
      return [...blocks, { kind: "text", text: t }];
    }
    case "thinking.delta": {
      const t = (ev.content as string) || "";
      if (last && last.kind === "thinking") {
        const next = blocks.slice();
        next[next.length - 1] = { kind: "thinking", text: last.text + t };
        return next;
      }
      return [...blocks, { kind: "thinking", text: t }];
    }
    case "tool.start":
      return [...blocks, { kind: "tool", id: ev.id as string | undefined, name: ev.name as string, content: "…", pending: true }];
    case "tool.end": {
      const id = ev.id as string | undefined;
      const name = ev.name as string | undefined;
      let found = false;
      return blocks.map((b) => {
        if (b.kind !== "tool") return b;
        const matches = !found && (id ? b.id === id : name ? b.name === name : b.pending);
        if (matches) {
          found = true;
          return { ...b, content: ev.content as string, pending: false };
        }
        return b;
      });
    }
    case "widget.emit":
      return [...blocks, { kind: "widget", widgetKind: ev.kind as string, data: ev.data }];
    case "workflow.emit":
      return [...blocks, { kind: "workflow", run: ev.run as import("@/lib/types").WorkflowRun }];
    case "ref.emit":
      return [
        ...blocks,
        { kind: "ref", refKind: ev.kind as string, name: ev.name as string, refId: ev.ref_id as string | undefined },
      ];
    default:
      return blocks;
  }
}

export function ChatStream({
  session,
  onRunningChange,
}: {
  session: SessionMeta | null;
  onRunningChange?: (b: boolean) => void;
}) {
  const g = useGinno();
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [liveId, setLiveId] = useState<string | null>(null);
  const [streamAgent, setStreamAgent] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [target, setTarget] = useState<string | null>(null);
  const [permission, setPermission] = useState<PermissionPrompt | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const liveIdRef = useRef<string | null>(null);
  const busyRef = useRef(false); // send lock: one turn at a time
  useEffect(() => {
    liveIdRef.current = liveId;
  }, [liveId]);

  useEffect(() => {
    setMessages([]);
    setLiveId(null);
    setStreamAgent(null);
    setPermission(null);
    busyRef.current = false;
  }, [session?.id]);

  // websocket with auto-reconnect
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
        busyRef.current = false;
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

  const running =
    liveId !== null || !!permission || messages.some((m) => hasPendingTool(m.blocks));
  useEffect(() => {
    onRunningChange?.(running);
  }, [running, onRunningChange]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  function ensureLive(): string {
    if (liveIdRef.current) return liveIdRef.current;
    const id = mid();
    setMessages((m) => [...m, { id, role: "assistant", blocks: [], agentId: streamAgent }]);
    setLiveId(id);
    liveIdRef.current = id;
    return id;
  }

  function mutateLive(ev: { event: string; [k: string]: unknown }) {
    const id = ensureLive();
    setMessages((m) => m.map((msg) => (msg.id === id ? { ...msg, blocks: applyBlock(msg.blocks, ev) } : msg)));
  }

  function handle(ev: { event: string; [k: string]: unknown }) {
    switch (ev.event) {
      case "token.delta":
      case "thinking.delta":
      case "tool.start":
      case "tool.end":
      case "widget.emit":
      case "ref.emit":
      case "workflow.emit":
        mutateLive(ev);
        break;
      case "turn.start": {
        // authoritative agent for this turn (server-resolved, never null)
        const id = liveIdRef.current;
        if (id) {
          setMessages((m) =>
            m.map((msg) =>
              msg.id === id
                ? { ...msg, agentId: (ev.agent_id as string) || null, agentName: ev.name as string }
                : msg,
            ),
          );
        }
        break;
      }
      case "permission.request":
        setPermission({ tool: ev.tool as string, args: ev.args });
        break;
      case "todos.changed":
        g.reloadTodos();
        break;
      case "workflows.changed":
        g.reloadWorkflows();
        g.reloadWorkflowRuns();
        break;
      case "artifacts.changed":
        g.reloadArtifacts();
        break;
      case "message.end":
        setLiveId(null);
        liveIdRef.current = null;
        setStreamAgent(null);
        busyRef.current = false;
        break;
      case "error":
        setLiveId(null);
        liveIdRef.current = null;
        busyRef.current = false;
        setMessages((m) => [
          ...m,
          { id: mid(), role: "assistant", blocks: [{ kind: "text", text: `[error] ${ev.message}` }] },
        ]);
        break;
    }
  }

  function send() {
    if (busyRef.current) return; // one turn at a time
    const text = input.trim();
    const ws = wsRef.current;
    if (!text || !ws || !session || !g.connected) return;
    const agentId = target ?? session.agent_id ?? g.agents[0]?.id ?? null;
    if (agentId && agentId !== session.agent_id) g.setSessionAgent(session.id, agentId);
    const live = mid();
    const guessName = agentById(agentId)?.name ?? "Agent";
    busyRef.current = true;
    setMessages((m) => [
      ...m,
      { id: mid(), role: "user", blocks: [{ kind: "text", text }] },
      { id: live, role: "assistant", blocks: [], agentId: agentId, agentName: guessName },
    ]);
    setLiveId(live);
    liveIdRef.current = live;
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

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto flex max-w-3xl flex-col gap-5">
          {messages.length === 0 && (
            <div className="py-16 text-center text-sm text-faint">
              Start a conversation. The agent will use tools and may ask for permission.
            </div>
          )}

          {messages.map((m) =>
            m.role === "user" ? (
              <div key={m.id} className="flex justify-end">
                <div className="max-w-[78%] rounded-2xl bg-card2 px-4 py-2.5 text-sm leading-relaxed text-txt">
                  {(m.blocks[0] as Extract<Block, { kind: "text" }>)?.text}
                </div>
              </div>
            ) : (
              <AssistantBubble
                key={m.id}
                agent={agentById(m.agentId)}
                agentName={m.agentName}
                blocks={m.blocks}
                streaming={m.id === liveId}
              />
            ),
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
              placeholder="Ask any Agent …  (try: 用 stat_list 卡片展示 PR 状态)"
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
                disabled={!g.connected || !!permission || running || !input.trim()}
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
  agentName,
  blocks,
  streaming,
}: {
  agent: AgentConfig | null;
  agentName?: string;
  blocks: Block[];
  streaming?: boolean;
}) {
  const hex = agentHex(agent?.color);
  const displayName = agent?.name || agentName || "Agent";
  const hasInner = blocks.some((b) => b.kind !== "ref");
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
          <span className="font-medium text-txt">{displayName}</span>
          <span className="text-xs text-faint">{streaming ? "thinking…" : "just now"}</span>
        </div>
        <div className="rounded-xl border border-line bg-card px-4 py-3 text-sm leading-relaxed text-txt">
          {hasInner ? (
            <InnerBlocks blocks={blocks} streaming={streaming} />
          ) : (
            <span className="inline-flex items-center gap-1 text-muted">
              <span className="flex gap-0.5">
                <Dot /> <Dot d={150} /> <Dot d={300} />
              </span>
              <span className="ml-1 text-xs">Thinking…</span>
            </span>
          )}
        </div>
        <RefBlocks blocks={blocks} />
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
