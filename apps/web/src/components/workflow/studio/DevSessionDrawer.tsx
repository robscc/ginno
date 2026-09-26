"use client";

/**
 * DevSessionDrawer — 方案B 阶段5 屏4: edit a workflow's DSL through
 * conversation WITHOUT leaving the Studio. A right-side drawer hosting the
 * workflow-dev session's live stream (PinStream architecture: ONE session per
 * mount, own WebSocket, own message loop on top of the blocks.tsx renderers —
 * ChatStream is a singleton in AppShell and cannot be mounted twice).
 *
 * The point of the drawer is the version.propose card: the dev agent proposes
 * a DSL edit, the user applies/rejects inline, the server writes v(N+1) and
 * the Studio canvas refreshes via reloadWorkflows.
 */

import { useEffect, useRef, useState } from "react";
import { AlertCircle, ArrowUp, FileEdit, Loader2, MessagesSquare, Square, X } from "lucide-react";
import { getSessionHistory, openSessionSocket } from "@/lib/runtime";
import { useGinno } from "@/lib/store";
import type { WorkflowDef } from "@/lib/types";
import { loadToolLabels, toolLabel } from "@/lib/toolLabels";
import { STATUS_LABEL } from "@/components/chat/RunBlocks";
import { DiffView } from "@/components/workflow/DiffView";
import {
  ContextBlocks,
  InnerBlocks,
  UserBlocks,
  hasPendingTool,
  type Block,
} from "@/components/chat/blocks";

interface VersionPropose {
  workflow_id: string;
  from_version: number;
  diff: string;
  rationale: string;
}

interface DrawerMsg {
  id: string;
  // "system" = context/compaction notice rows (centered, not a bubble)
  role: "user" | "assistant" | "system";
  blocks: Block[];
  turnId?: string;
  error?: boolean;
}

interface PermissionPrompt {
  tool: string;
  args: unknown;
}

let _mid = 0;
const mid = () => `dm${++_mid}`;

const newTurnId = () =>
  typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `t-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

const EMPTY_TOOL_RESULT_RE =
  /^\s*(\(no matches\)|\(no files found\)|\(empty\)|no results|no files matched|no matches found|no files found|\(nothing found\))\s*$/i;

/** Streaming-event → block-list reducer (mirror of PinStream's subset). */
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
      return [
        ...blocks,
        {
          kind: "tool",
          id: ev.id as string | undefined,
          name: ev.name as string,
          content: "…",
          pending: true,
          argsPreview: (ev.preview as string) || undefined,
        },
      ];
    case "tool.args": {
      const id = ev.id as string | undefined;
      const preview = ev.preview as string;
      if (!id || !preview) return blocks;
      let matched = false;
      return blocks.map((b) => {
        if (b.kind !== "tool") return b;
        if (!matched && b.id === id) {
          matched = true;
          return { ...b, argsPreview: preview };
        }
        return b;
      });
    }
    case "tool.end": {
      const id = ev.id as string | undefined;
      const name = ev.name as string | undefined;
      const content = ev.content as string;
      if (EMPTY_TOOL_RESULT_RE.test(content)) {
        return blocks.filter((b) => {
          if (b.kind !== "tool") return true;
          const matches = id ? b.id === id : name ? b.name === name : b.pending;
          return !matches;
        });
      }
      let found = false;
      return blocks.map((b) => {
        if (b.kind !== "tool") return b;
        const matches = !found && (id ? b.id === id : name ? b.name === name : b.pending);
        if (matches) {
          found = true;
          return { ...b, content, pending: false };
        }
        return b;
      });
    }
    default:
      return blocks;
  }
}

/** /history → bubbles. History blocks arrive pre-mapped (server-side
 *  messages_ui); widgets/refs that InnerBlocks skips render as nothing —
 *  acceptable for a DSL-editing drawer. */
function mapHistory(res: {
  messages?: Array<{ id?: string; role: "user" | "assistant"; blocks: Block[]; turnId?: string }>;
  last_error?: { message?: string; turn_id?: string } | null;
}): DrawerMsg[] {
  const msgs: DrawerMsg[] = (res.messages ?? []).map((m) => ({
    id: m.id ?? mid(),
    role: m.role === "user" ? "user" : "assistant",
    blocks: m.blocks ?? [],
    turnId: m.turnId,
  }));
  if (res.last_error?.message) {
    msgs.push({
      id: mid(),
      role: "assistant",
      error: true,
      blocks: [{ kind: "text", text: res.last_error.message }],
    });
  }
  return msgs;
}

/**
 * Outer shell: session resolution (reuse the bound workflow-dev session or
 * create one — same title convention as the old router.push flow) + the
 * overlay chrome. The inner stream is keyed by session id so a session switch
 * is a clean remount (PinStream's contract).
 */
export function DevSessionDrawer({ wf, onClose }: { wf: WorkflowDef; onClose: () => void }) {
  const g = useGinno();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const existing = g.sessions.find((s) => s.workflow_id === wf.id);
    if (existing) {
      setSessionId(existing.id);
      return;
    }
    setErr(null);
    g.newSession("workflow-dev", { title: `精炼流程：${wf.name}`, workflow_id: wf.id }).then(
      (s) => {
        if (!alive) return;
        if (s?.id) setSessionId(s.id);
        else setErr("新建会话失败：请在 设置 → 模型 API 启用一个模型提供商");
      },
    );
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wf.id]);

  // Esc closes the drawer (the composer never sees it — no trap needed).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50" onClick={onClose}>
      <div
        className="absolute right-0 top-0 flex h-full w-[420px] max-w-[92vw] flex-col border-l border-line bg-panel shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-1.5 border-b border-line px-3 py-2">
          <MessagesSquare className="h-3.5 w-3.5 shrink-0 text-violet" />
          <span className="min-w-0 flex-1 truncate text-[12.5px] font-semibold text-txt">
            开发会话 · {wf.name}
          </span>
          <span className="rounded border border-line2 px-1 font-mono text-[10px] text-faint">
            v{wf.version ?? 1}
          </span>
          <button
            onClick={onClose}
            title="关闭 (Esc)"
            className="btn-press rounded border border-line2 px-1.5 py-0.5 text-[11px] text-muted hover:text-txt"
          >
            <X className="h-3 w-3" />
          </button>
        </div>

        {sessionId ? (
          <DrawerStream
            key={sessionId}
            sessionId={sessionId}
            onApplied={() => {
              // 应用后服务端写 v(N+1) 并广播 workflows.changed — 主动刷新让
              // Studio 画布立即跟上（不依赖全局 ChatStream 的转发）。
              void g.reloadWorkflows();
              void g.reloadWorkflowRuns();
            }}
          />
        ) : (
          <div className="flex flex-1 items-center justify-center px-6 text-center text-xs text-faint">
            {err ? <span className="text-red">{err}</span> : <Loader2 className="h-4 w-4 animate-spin" />}
          </div>
        )}
      </div>
    </div>
  );
}

/** The session stream: socket lifecycle + message loop mirror PinStream's
 *  semantics (3s reconnect, 20s ping, 45s silence close, post-reconnect
 *  turn_state probe) plus the two drawer-specific renders: the version.propose
 *  diff card and the subtle run-activity strip. */
function DrawerStream({
  sessionId,
  onApplied,
}: {
  sessionId: string;
  onApplied: () => void;
}) {
  const [messages, setMessages] = useState<DrawerMsg[]>([]);
  const [liveId, setLiveId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [permission, setPermission] = useState<PermissionPrompt | null>(null);
  const [propose, setPropose] = useState<VersionPropose | null>(null);
  const [proposeResult, setProposeResult] = useState<{ decision: "allow" | "deny"; fromVersion: number } | null>(null);
  const [runLines, setRunLines] = useState<Record<string, string>>({});
  const [wsStatus, setWsStatus] = useState<"connecting" | "live" | "reconnecting">("connecting");
  const [input, setInput] = useState("");
  const [sendError, setSendError] = useState<string | null>(null);

  const sockRef = useRef<WebSocket | null>(null);
  const liveRef = useRef<string | null>(null);
  const busyRef = useRef(false);
  const reconnRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const watchRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconcileRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastSeenRef = useRef(0);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    loadToolLabels();
  }, []);

  function ensureLive(): string {
    if (liveRef.current) return liveRef.current;
    const id = mid();
    liveRef.current = id;
    setLiveId(id);
    setMessages((prev) => [...prev, { id, role: "assistant", blocks: [] }]);
    return id;
  }

  function mutateLive(ev: { event: string; [k: string]: unknown }) {
    const id = ensureLive();
    setMessages((prev) =>
      prev.map((m) => (m.id === id ? { ...m, blocks: applyBlock(m.blocks, ev) } : m)),
    );
  }

  function closeLive(finalizeTools: boolean) {
    const id = liveRef.current;
    liveRef.current = null;
    setLiveId(null);
    busyRef.current = false;
    setBusy(false);
    setMessages((prev) => {
      let next = prev;
      if (id) {
        next = next.filter((m) => !(m.id === id && m.blocks.length === 0));
        if (finalizeTools) {
          next = next.map((m) =>
            hasPendingTool(m.blocks)
              ? {
                  ...m,
                  blocks: m.blocks.map((b) =>
                    b.kind === "tool" && b.pending
                      ? { ...b, pending: false, content: b.content === "…" ? "(interrupted)" : b.content }
                      : b,
                  ),
                }
              : m,
          );
        }
      }
      return next;
    });
  }

  function addSystemRow(text: string) {
    setMessages((prev) => [
      ...prev,
      { id: mid(), role: "system", blocks: [{ kind: "context", text }] },
    ]);
  }

  function reconcileFromHistory() {
    getSessionHistory(sessionId)
      .then((res) => setMessages(mapHistory(res)))
      .catch(() => {
        /* sidecar down — the reconnect loop will retry */
      });
  }

  function handle(ev: { event: string; [k: string]: unknown }) {
    switch (ev.event) {
      case "turn.start": {
        busyRef.current = true;
        setBusy(true);
        setSendError(null);
        ensureLive();
        break;
      }
      case "token.delta":
      case "thinking.delta":
      case "tool.start":
      case "tool.args":
      case "tool.end": {
        busyRef.current = true;
        setBusy(true);
        mutateLive(ev);
        break;
      }
      case "permission.request":
        setPermission({ tool: ev.tool as string, args: ev.args });
        break;
      case "version.propose":
        // The whole point of the drawer: the dev agent's DSL edit proposal.
        setPropose({
          workflow_id: ev.workflow_id as string,
          from_version: (ev.from_version as number) ?? 0,
          diff: (ev.diff as string) ?? "",
          rationale: (ev.rationale as string) ?? "",
        });
        break;
      case "run.bind":
      case "run.status": {
        // MINIMAL: the Studio observer shows full detail — the drawer only
        // surfaces a subtle "run #xxxx · 状态" strip for runs the dev agent
        // triggered (or that were presented into this session).
        const rid = ev.run_id as string;
        if (rid) {
          const st = (ev.status as string) || "bound";
          setRunLines((prev) => ({ ...prev, [rid]: st }));
        }
        break;
      }
      case "workflows.changed":
        // A propose was applied (here or elsewhere) — the shell's explicit
        // onApplied covers the local decision; this catches remote edits.
        onApplied();
        break;
      case "notice":
        mutateLive({ event: "token.delta", content: (ev.message as string) || "" });
        break;
      case "context.compacted":
        addSystemRow(`对话已压缩：${Number(ev.compacted_messages ?? 0)} 条较早的消息被摘要替代。`);
        break;
      case "turn.state":
        if (reconcileRef.current) {
          clearTimeout(reconcileRef.current);
          reconcileRef.current = null;
        }
        if (ev.running) break;
        reconcileFromHistory();
        busyRef.current = false;
        setBusy(false);
        break;
      case "message.end":
        closeLive(false);
        setPermission(null);
        break;
      case "turn.stopped":
        closeLive(true);
        setPermission(null);
        break;
      case "error": {
        closeLive(true);
        setPermission(null);
        const text = String(ev.message || "") || "未知错误";
        setMessages((prev) => [
          ...prev,
          { id: mid(), role: "assistant", error: true, blocks: [{ kind: "text", text }] },
        ]);
        break;
      }
      default:
        break;
    }
  }

  function connect() {
    const existing = sockRef.current;
    if (
      existing &&
      (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }
    if (reconnRef.current) {
      clearTimeout(reconnRef.current);
      reconnRef.current = null;
    }
    setWsStatus("connecting");
    const sock = openSessionSocket(sessionId);
    sockRef.current = sock;
    sock.onopen = () => {
      if (sockRef.current !== sock) return;
      setWsStatus("live");
      setSendError(null);
      lastSeenRef.current = Date.now();
      if (liveRef.current || busyRef.current) {
        try {
          sock.send(JSON.stringify({ type: "turn_state" }));
        } catch {
          /* ignore */
        }
        if (reconcileRef.current) clearTimeout(reconcileRef.current);
        reconcileRef.current = setTimeout(() => {
          reconcileRef.current = null;
          reconcileFromHistory();
        }, 6000);
      }
      pingRef.current = setInterval(() => {
        if (sock.readyState === WebSocket.OPEN) {
          try {
            sock.send(JSON.stringify({ type: "ping" }));
          } catch {
            /* ignore */
          }
        }
      }, 20000);
      watchRef.current = setInterval(() => {
        if (Date.now() - lastSeenRef.current > 45000) {
          try {
            sock.close();
          } catch {
            /* ignore */
          }
        }
      }, 10000);
    };
    sock.onmessage = (e) => {
      if (sockRef.current !== sock) return;
      lastSeenRef.current = Date.now();
      try {
        handle(JSON.parse(e.data));
      } catch {
        /* malformed frame — ignore */
      }
    };
    sock.onerror = () => {
      if (sockRef.current !== sock) return;
      try {
        sock.close();
      } catch {
        /* ignore */
      }
    };
    sock.onclose = () => {
      if (pingRef.current) {
        clearInterval(pingRef.current);
        pingRef.current = null;
      }
      if (watchRef.current) {
        clearInterval(watchRef.current);
        watchRef.current = null;
      }
      if (sockRef.current !== sock) return;
      sockRef.current = null;
      setWsStatus("reconnecting");
      reconnRef.current = setTimeout(() => {
        reconnRef.current = null;
        connect();
      }, 3000);
    };
  }

  useEffect(() => {
    let alive = true;
    connect();
    getSessionHistory(sessionId)
      .then((res) => {
        if (alive) setMessages(mapHistory(res));
      })
      .catch(() => {
        /* sidecar down — events will populate once it returns */
      });
    return () => {
      alive = false;
      if (reconnRef.current) clearTimeout(reconnRef.current);
      if (pingRef.current) clearInterval(pingRef.current);
      if (watchRef.current) clearInterval(watchRef.current);
      if (reconcileRef.current) clearTimeout(reconcileRef.current);
      reconnRef.current = pingRef.current = watchRef.current = reconcileRef.current = null;
      const sock = sockRef.current;
      sockRef.current = null;
      if (sock) {
        try {
          sock.close();
        } catch {
          /* ignore */
        }
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  useEffect(() => {
    if (stickRef.current) bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages, propose]);

  function onScroll() {
    const el = scrollRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  }

  function sendText(raw: string) {
    const text = raw.trim();
    if (!text || busyRef.current) return;
    const sock = sockRef.current;
    if (!sock || sock.readyState !== WebSocket.OPEN) {
      setSendError("连接未就绪，正在重连…");
      return;
    }
    const turnId = newTurnId();
    const uid = mid();
    const lid = mid();
    busyRef.current = true;
    setBusy(true);
    liveRef.current = lid;
    setLiveId(lid);
    setSendError(null);
    setMessages((prev) => [
      ...prev,
      { id: uid, role: "user", blocks: [{ kind: "text", text }], turnId },
      { id: lid, role: "assistant", blocks: [] },
    ]);
    setInput("");
    requestAnimationFrame(() => {
      if (textareaRef.current) textareaRef.current.style.height = "auto";
    });
    try {
      sock.send(
        JSON.stringify({
          type: "invoke",
          message: text,
          agent_id: null,
          turn_id: turnId,
          images: [],
          files: [],
        }),
      );
    } catch {
      busyRef.current = false;
      setBusy(false);
      liveRef.current = null;
      setLiveId(null);
      setMessages((prev) => prev.filter((m) => m.id !== lid && m.id !== uid));
      setInput(text);
      setSendError("发送失败，请重试");
    }
  }

  function stopTurn() {
    try {
      sockRef.current?.send(JSON.stringify({ type: "stop" }));
    } catch {
      /* socket gone — reconnect reconciles via turn_state */
    }
  }

  /** Exact ChatStream wire format (ChatStream.tsx respondPropose): the plain
   *  permission_response channel with no propose_id — the server tracks the
   *  pending proposal interrupt per session and resumes it with {decision}. */
  function respondPropose(decision: "allow" | "deny") {
    try {
      sockRef.current?.send(JSON.stringify({ type: "permission_response", decision }));
    } catch {
      /* socket gone — reconnect re-emits version.propose if still pending */
    }
    const p = propose;
    setPropose(null);
    if (p) {
      setProposeResult({ decision, fromVersion: p.from_version });
      if (decision === "allow") onApplied();
      window.setTimeout(() => setProposeResult(null), 4000);
    }
  }

  function respond(decision: "allow" | "deny") {
    try {
      sockRef.current?.send(JSON.stringify({ type: "permission_response", decision }));
    } catch {
      /* socket gone — reconnect re-emits the prompt if still pending */
    }
    setPermission(null);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !e.nativeEvent.isComposing) {
      e.preventDefault();
      sendText(input);
    }
  }

  function onInput(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setInput(e.target.value);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 112)}px`;
  }

  const connecting = wsStatus !== "live";

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* run activity strip (MINIMAL — full detail lives in the observer) */}
      {Object.keys(runLines).length > 0 && (
        <div className="flex flex-wrap gap-x-3 gap-y-0.5 border-b border-line px-3 py-1 text-[10px] text-faint">
          {Object.entries(runLines).map(([rid, st]) => (
            <span key={rid} className="flex items-center gap-1">
              <span
                className={`h-1 w-1 rounded-full ${
                  st === "running" ? "animate-pulse bg-green" : "bg-faint"
                }`}
              />
              run #{rid.slice(0, 8)} · {STATUS_LABEL[st] || st}
            </span>
          ))}
        </div>
      )}

      {/* messages */}
      <div ref={scrollRef} onScroll={onScroll} className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2.5 py-2.5">
        {messages.map((m) => {
          if (m.role === "system") {
            return (
              <ContextBlocks
                key={m.id}
                blocks={m.blocks.filter((b): b is Extract<Block, { kind: "context" }> => b.kind === "context")}
              />
            );
          }
          if (m.role === "user") {
            return (
              <div key={m.id} className="flex justify-end">
                <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md border border-violet/25 bg-violet/[0.12] px-3 py-1.5 text-[13px] leading-relaxed text-txt">
                  <UserBlocks blocks={m.blocks} />
                </div>
              </div>
            );
          }
          if (m.error) {
            const text = m.blocks.find((b) => b.kind === "text");
            return (
              <div key={m.id} className="rounded-xl border border-red/40 bg-red/[0.07] px-3 py-2">
                <div className="mb-1 flex items-center gap-1.5 text-xs font-medium text-red">
                  <AlertCircle className="h-3.5 w-3.5 shrink-0" />
                  回复失败
                </div>
                <div className="whitespace-pre-wrap break-words text-xs leading-relaxed text-muted">
                  {"text" in (text ?? {}) ? String((text as { text?: string })?.text ?? "") : ""}
                </div>
              </div>
            );
          }
          const streaming = m.id === liveId;
          return (
            <div key={m.id} className="rounded-2xl rounded-bl-md border border-line bg-card px-3 py-2 text-[13px] leading-relaxed text-txt">
              <InnerBlocks blocks={m.blocks} streaming={streaming} />
              {streaming && m.blocks.length === 0 && (
                <Loader2 className="h-3.5 w-3.5 animate-spin text-faint" />
              )}
            </div>
          );
        })}

        {/* the drawer's reason to exist: the DSL 变更提案 diff card */}
        {propose && <DrawerProposeCard propose={propose} onDecide={respondPropose} />}
        {proposeResult && (
          <div className="rounded-lg border border-line2 bg-card2/60 px-3 py-1.5 text-[11px] text-muted">
            {proposeResult.decision === "allow" ? "已应用变更" : "已拒绝该 DSL 变更"} · v
            {proposeResult.fromVersion} → {proposeResult.decision === "allow" ? "新版本" : "保持不变"}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* inline permission confirmation (non-workflow tool the agent needs) */}
      {permission && (
        <div className="mx-2.5 mb-2 rounded-xl border border-yellow/40 bg-yellow/10 p-2.5">
          <div className="mb-1 flex items-center gap-1.5 text-xs font-medium text-yellow">
            <AlertCircle className="h-3.5 w-3.5 shrink-0" />
            权限确认 · <code className="font-mono text-txt">{toolLabel(permission.tool)}</code>
          </div>
          <pre className="mb-2 max-h-20 overflow-auto rounded-lg bg-base/60 p-2 font-mono text-[10px] leading-snug text-muted">
            {JSON.stringify(permission.args, null, 2)}
          </pre>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => respond("allow")}
              className="rounded-lg bg-violet px-2.5 py-1 text-xs font-medium text-white transition-opacity hover:opacity-90"
            >
              允许一次
            </button>
            <button
              type="button"
              onClick={() => respond("deny")}
              className="rounded-lg border border-line2 px-2.5 py-1 text-xs text-muted transition-colors hover:text-txt"
            >
              拒绝
            </button>
          </div>
        </div>
      )}

      {(connecting || sendError) && (
        <div className="flex items-center gap-1.5 px-3 pb-1 text-[11px] text-faint">
          {connecting && <Loader2 className="h-3 w-3 animate-spin" />}
          {sendError ?? (wsStatus === "reconnecting" ? "连接断开，重连中…" : "连接中…")}
        </div>
      )}

      {/* composer */}
      <div className="border-t border-line px-2.5 py-2">
        <div className="flex items-end gap-1.5 rounded-xl border border-line2 bg-card px-2.5 py-1.5 transition-colors focus-within:border-violet/60">
          <textarea
            ref={textareaRef}
            rows={1}
            value={input}
            onChange={onInput}
            onKeyDown={onKeyDown}
            placeholder="让代理改这份流程…（⌘/Ctrl+Enter 发送）"
            className="max-h-28 min-h-[24px] flex-1 resize-none bg-transparent text-[13px] leading-6 text-txt outline-none placeholder:text-faint"
          />
          {busy ? (
            <button
              type="button"
              onClick={stopTurn}
              title="停止本轮"
              className="mb-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-red/15 text-red transition-colors hover:bg-red/25"
            >
              <Square className="h-3 w-3" />
            </button>
          ) : (
            <button
              type="button"
              onClick={() => sendText(input)}
              disabled={!input.trim()}
              title="发送 (⌘/Ctrl+Enter)"
              className="mb-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-violet text-white transition-opacity hover:opacity-90 disabled:opacity-30"
            >
              <ArrowUp className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/** workflow_propose_edit diff confirmation card, drawer-width variant of
 *  ChatStream's ProposeCard (collapsed-by-default diff with hunk count; the
 *  session graph is paused at the tool's interrupt until the user decides). */
function DrawerProposeCard({
  propose,
  onDecide,
}: {
  propose: VersionPropose;
  onDecide: (decision: "allow" | "deny") => void;
}) {
  const [deciding, setDeciding] = useState<null | "allow" | "deny">(null);
  const [diffOpen, setDiffOpen] = useState(false);
  const hunks = (propose.diff.match(/^@@/gm) || []).length;
  const decide = (d: "allow" | "deny") => {
    if (deciding) return;
    setDeciding(d);
    onDecide(d);
  };
  return (
    <div className="rounded-xl border border-yellow/30 bg-yellow/[0.04] p-2.5">
      <div className="mb-1 flex flex-wrap items-center gap-1.5 text-[12.5px] font-medium text-yellow">
        <FileEdit className="h-3.5 w-3.5 shrink-0" />
        DSL 变更提案
        <span className="rounded border border-yellow/40 px-1.5 py-0.5 text-[10px] font-normal text-muted">
          {propose.workflow_id} · v{propose.from_version} → 新版本
        </span>
      </div>
      {propose.rationale && <div className="mb-2 text-xs text-muted">理由：{propose.rationale}</div>}
      <button
        onClick={() => setDiffOpen((v) => !v)}
        className="mb-2 flex items-center gap-1 text-[11px] text-faint hover:text-muted"
      >
        {diffOpen ? "收起 diff" : `查看完整 diff（${hunks} 处改动）`}
      </button>
      {diffOpen && <DiffView diff={propose.diff} />}
      <div className="mt-2.5 flex gap-2">
        <button
          onClick={() => decide("allow")}
          disabled={!!deciding}
          className="btn-press flex items-center gap-1 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
        >
          {deciding === "allow" && <Loader2 className="h-3 w-3 animate-spin" />}
          {deciding === "allow" ? "应用中…" : "应用（创建新版本）"}
        </button>
        <button
          onClick={() => decide("deny")}
          disabled={!!deciding}
          className="btn-press flex items-center gap-1 rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:bg-red/10 hover:text-red disabled:opacity-50"
        >
          {deciding === "deny" && <Loader2 className="h-3 w-3 animate-spin" />}
          拒绝
        </button>
      </div>
    </div>
  );
}
