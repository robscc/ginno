"use client";

import { useEffect, useRef, useState } from "react";
import { Paperclip, Keyboard, ArrowUp, X } from "lucide-react";
import { useGinno } from "@/lib/store";
import { openSessionSocket, getSessionHistory } from "@/lib/runtime";
import { agentHex } from "@/lib/theme";
import { Icon } from "@/components/icons";
import { InnerBlocks, RefBlocks, UserBlocks, hasPendingTool, type Block } from "@/components/chat/blocks";
import { DiffView } from "@/components/workflow/DiffView";
import type { AgentConfig, SessionMeta } from "@/lib/types";

interface ChatMsg {
  id: string;
  role: "user" | "assistant";
  blocks: Block[];
  agentId?: string | null;
  agentName?: string;
  turnId?: string; // per-turn trace UUID (shown on the bubble, greppable in sidecar logs)
}

const newTurnId = () =>
  typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `t-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

async function copyText(t: string) {
  try {
    await navigator.clipboard.writeText(t);
    return true;
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = t;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch {
      return false;
    }
  }
}

/** Click-to-copy per-turn trace UUID. The full id is what you grep the sidecar
 *  logs for (`turn=...`); we show a short prefix to keep the bubble tidy. */
function TurnIdChip({ turnId }: { turnId?: string }) {
  const [copied, setCopied] = useState(false);
  if (!turnId) return null;
  const short = turnId.slice(0, 8);
  return (
    <button
      onClick={async () => {
        if (await copyText(turnId)) {
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        }
      }}
      title={`turn ${turnId}（点击复制，用于日志定位）`}
      className="rounded border border-line2 px-1 py-px font-mono text-[9px] text-faint transition-colors hover:border-violet/50 hover:text-violet"
    >
      {copied ? "copied" : `#${short}`}
    </button>
  );
}

interface PermissionPrompt {
  tool: string;
  args: unknown;
}

interface VersionPropose {
  workflow_id: string;
  from_version: number;
  diff: string;
  rationale: string;
}

let _mid = 0;
const mid = () => `m${++_mid}`;

interface Attachment {
  data: string; // base64 payload (no data-url prefix)
  mediaType: string;
  preview: string; // full data URL for local display
  name: string;
}

/**
 * Read an image file as a data URL. Files over ~400KB are re-encoded through a
 * canvas (max 1600px, JPEG 0.85) — the checkpointer rewrites the whole session
 * file on every step, so keeping embedded images small matters.
 */
function readImage(file: File): Promise<Attachment | null> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => {
      const src = String(reader.result || "");
      const finish = (url: string) => {
        const m = /^data:([^;]+);base64,(.*)$/.exec(url);
        resolve(m ? { data: m[2], mediaType: m[1], preview: url, name: file.name } : null);
      };
      if (file.size <= 400_000) return finish(src);
      const img = new Image();
      img.onload = () => {
        const MAX = 1600;
        const scale = Math.min(1, MAX / Math.max(img.width, img.height));
        const w = Math.max(1, Math.round(img.width * scale));
        const h = Math.max(1, Math.round(img.height * scale));
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext("2d");
        if (!ctx) return finish(src);
        ctx.drawImage(img, 0, 0, w, h);
        finish(canvas.toDataURL("image/jpeg", 0.85));
      };
      img.onerror = () => finish(src);
      img.src = src;
    };
    reader.onerror = () => resolve(null);
    reader.readAsDataURL(file);
  });
}

// Patterns that indicate a tool returned "no results" — hide these blocks to reduce noise.
// Covers both the builtin tool phrasing ("(no matches)") and the MCP filesystem
// server's phrasing ("No matches found"), so empty search results don't pile up
// as collapsed panels the agent already explains in prose.
const EMPTY_TOOL_RESULT_RE =
  /^\s*(\(no matches\)|\(no files found\)|\(empty\)|no results|no files matched|no matches found|no files found|\(nothing found\))\s*$/i;

function isEmptyToolResult(content: string): boolean {
  return EMPTY_TOOL_RESULT_RE.test(content);
}

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
      const content = ev.content as string;
      // Hide tool blocks that returned "no results" to reduce noise
      if (isEmptyToolResult(content)) {
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
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const [target, setTarget] = useState<string | null>(null);
  const [permission, setPermission] = useState<PermissionPrompt | null>(null);
  const [propose, setPropose] = useState<VersionPropose | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true); // auto-scroll only while the user is near the bottom
  const liveIdRef = useRef<string | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const busyRef = useRef(false); // send lock: one turn at a time
  // Composer height: undefined = auto-grow (capped); a number = user-dragged size.
  const [composerH, setComposerH] = useState<number | undefined>(undefined);
  const composerBoxRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{ startY: number; startH: number } | null>(null);
  // WebSocket liveness, surfaced in the composer so a dropped/stalled link is
  // visible (and clickable to force a reconnect) instead of silently freezing.
  const [wsStatus, setWsStatus] = useState<"connecting" | "live" | "reconnecting" | "offline">(
    "connecting",
  );
  const connectRef = useRef<() => void>(() => {});
  // Per-session cache: unsent composer draft + attachments + last-seen message
  // snapshot, so switching sessions preserves each session's state (and the
  // draft) with no empty-flash. On revisit we ALSO re-fetch the server's
  // authoritative history in the background, so a turn that completed while you
  // were on another session shows up in full (fixes "switch mid-reply -> reply
  // lost / empty reply").
  const sessionCacheRef = useRef<
    Record<string, { messages: ChatMsg[]; input: string; attachments: Attachment[] }>
  >({});
  const curSessionIdRef = useRef<string | null>(null);
  useEffect(() => {
    liveIdRef.current = liveId;
  }, [liveId]);

  // Drop cache entries for sessions that no longer exist (e.g. after delete) so
  // they don't leak memory or resurrect a deleted conversation.
  useEffect(() => {
    const live = new Set(g.sessions.map((s) => s.id));
    const cache = sessionCacheRef.current;
    for (const id of Object.keys(cache)) {
      if (!live.has(id)) delete cache[id];
    }
  }, [g.sessions]);

  // On session change: reset UI, load persisted history, then open the socket.
  useEffect(() => {
    if (!session) return;
    let closed = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let pingTimer: ReturnType<typeof setInterval> | null = null;
    let watchTimer: ReturnType<typeof setInterval> | null = null;
    let lastSeen = Date.now();
    let sock: WebSocket | null = null;

    // Save the session we're leaving so its latest in-memory conversation and
    // unsent draft survive the switch (instead of being wiped).
    const prev = curSessionIdRef.current;
    if (prev && prev !== session.id) {
      sessionCacheRef.current[prev] = { messages, input, attachments };
    }
    curSessionIdRef.current = session.id;

    // Always clear transient per-turn UI state on switch.
    setLiveId(null);
    liveIdRef.current = null;
    setStreamAgent(null);
    setPermission(null);
    setPropose(null);
    busyRef.current = false;

    const cached = sessionCacheRef.current[session.id];
    if (cached) {
      // Revisit: restore synchronously (no empty-flash, keeps latest in-memory
      // state incl. the unsent draft), then just (re)open the socket.
      setMessages(cached.messages);
      setInput(cached.input);
      setAttachments(cached.attachments);
    } else {
      // First visit this session: clear + load persisted history below.
      setMessages([]);
      setInput("");
      setAttachments([]);
    }

    const connect = () => {
      setWsStatus((s) => (s === "live" ? s : "connecting"));
      sock = openSessionSocket(session.id);
      wsRef.current = sock;
      // Every callback is guarded by `closed` (set by this effect's cleanup on
      // session switch). Without this, a stale socket from the previous session
      // could inject a phantom bubble into the new chat, clobber `connected`,
      // or reset the send-lock mid-turn and allow a concurrent double-send.
      sock.onopen = () => {
        if (closed) return;
        g.setConnected(true);
        setWsStatus("live");
        lastSeen = Date.now();
        // Self-heal a "stuck" turn: if a turn was in-flight when the socket
        // dropped, its message.end never arrived (server send hit the closed
        // socket) so liveId stayed set and the UI kept spinning. On reconnect
        // the server has no active turn for this session, so clear the live
        // state to unlock the composer. The completed turn shows on reload
        // (it was checkpointed server-side).
        if (liveIdRef.current) {
          setLiveId(null);
          liveIdRef.current = null;
          setStreamAgent(null);
          busyRef.current = false;
        }
        // Heartbeat: detect a half-open socket. The server answers `ping` with a
        // `pong` frame (which resets lastSeen via onmessage). A send into a dead
        // TCP connection buffers silently and never errors, so without this the
        // chat would freeze with no reconnect; if no frame arrives for 45s we
        // close and let the existing reconnect path take over.
        pingTimer = setInterval(() => {
          if (sock && sock.readyState === WebSocket.OPEN) {
            try {
              sock.send(JSON.stringify({ type: "ping" }));
            } catch {
              /* ignore */
            }
          }
        }, 20000);
        watchTimer = setInterval(() => {
          if (!closed && Date.now() - lastSeen > 45000) {
            try {
              sock?.close();
            } catch {
              /* ignore */
            }
          }
        }, 10000);
      };
      sock.onmessage = (e) => {
        if (closed) return;
        lastSeen = Date.now();
        try {
          handle(JSON.parse(e.data));
        } catch {
          /* ignore */
        }
      };
      sock.onerror = () => {
        if (closed) return;
        try {
          sock?.close();
        } catch {
          /* ignore */
        }
      };
      sock.onclose = () => {
        if (pingTimer) clearInterval(pingTimer);
        if (watchTimer) clearInterval(watchTimer);
        pingTimer = watchTimer = null;
        if (closed) return; // stale socket — the new session owns state now
        g.setConnected(false);
        setWsStatus("reconnecting");
        busyRef.current = false;
        timer = setTimeout(connect, 1500);
      };
      connectRef.current = connect;
    };

    // Load history FIRST (only on first visit — a cached revisit already has the
    // latest in-memory messages), then connect — so events from a (re)connecting
    // socket can't arrive before the history set and get clobbered by it.
    if (!cached) {
      setWsStatus("connecting");
      (async () => {
        try {
          const h = await getSessionHistory(session.id);
          const list: Array<{
            id?: string;
            role: "user" | "assistant";
            agentId?: string | null;
            blocks: Block[];
          }> = (h && h.messages) || [];
          if (!closed && list.length) {
            setMessages(
              list.map((m) => ({
                id: m.id ?? mid(),
                role: m.role,
                agentId: m.agentId ?? null,
                blocks: m.blocks || [],
              })),
            );
          }
        } catch {
          /* ignore */
        }
        if (!closed) connect();
      })();
    } else {
      // Revisit: we already restored the cached snapshot synchronously (no
      // flash). Re-fetch the server's authoritative history in the background
      // so a turn that finished while we were on another session is shown in
      // full. Only apply it if we're not mid-stream (liveId null) and the
      // server actually has at least as many messages as our snapshot — this
      // avoids clobbering an in-flight optimistic bubble or a live reconnect.
      (async () => {
        try {
          const h = await getSessionHistory(session.id);
          const list: Array<{
            id?: string;
            role: "user" | "assistant";
            agentId?: string | null;
            blocks: Block[];
          }> = (h && h.messages) || [];
          if (
            !closed &&
            curSessionIdRef.current === session.id &&
            !liveIdRef.current &&
            list.length >= (cached?.messages.length ?? 0)
          ) {
            const fresh = list.map((m) => ({
              id: m.id ?? mid(),
              role: m.role,
              agentId: m.agentId ?? null,
              blocks: m.blocks || [],
            }));
            setMessages(fresh);
            sessionCacheRef.current[session.id] = {
              messages: fresh,
              input: sessionCacheRef.current[session.id]?.input ?? "",
              attachments: sessionCacheRef.current[session.id]?.attachments ?? [],
            };
          }
        } catch {
          /* ignore */
        }
      })();
      connect();
    }

    return () => {
      closed = true;
      if (timer) clearTimeout(timer);
      if (pingTimer) clearInterval(pingTimer);
      if (watchTimer) clearInterval(watchTimer);
      try {
        sock?.close();
      } catch {
        /* ignore */
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.id]);

  const running =
    liveId !== null || !!permission || !!propose || messages.some((m) => hasPendingTool(m.blocks));
  useEffect(() => {
    onRunningChange?.(running);
  }, [running, onRunningChange]);

  // Auto-scroll only while the user is parked near the bottom; otherwise a
  // streaming token (or a history load) yanks them back down mid-read.
  useEffect(() => {
    if (stickRef.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  function ensureLive(): string {
    if (liveIdRef.current) return liveIdRef.current;
    const id = mid();
    setMessages((m) => [
      ...m,
      { id, role: "assistant", blocks: [], agentId: streamAgent, turnId: newTurnId() },
    ]);
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
        // authoritative agent for this turn (server-resolved, never null).
        // The server echoes the turn_id we sent (or mints one); adopt it as the
        // bubble's trace UUID so it matches the sidecar logs exactly.
        const id = liveIdRef.current;
        if (id) {
          const srvTurn = ev.turn_id as string | undefined;
          setMessages((m) =>
            m.map((msg) =>
              msg.id === id
                ? {
                    ...msg,
                    agentId: (ev.agent_id as string) || null,
                    agentName: ev.name as string,
                    turnId: srvTurn || msg.turnId,
                  }
                : msg,
            ),
          );
          // keep the user bubble's UUID in sync with the server's authoritative one
          if (srvTurn) {
            setMessages((m) =>
              m.map((msg, i, arr) =>
                msg.role === "user" && !msg.turnId && i === arr.length - 2
                  ? { ...msg, turnId: srvTurn }
                  : msg,
              ),
            );
          }
        }
        break;
      }
      case "permission.request":
        setPermission({ tool: ev.tool as string, args: ev.args });
        break;
      case "version.propose":
        setPropose({
          workflow_id: ev.workflow_id as string,
          from_version: (ev.from_version as number) ?? 0,
          diff: (ev.diff as string) ?? "",
          rationale: (ev.rationale as string) ?? "",
        });
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
      case "error": {
        setLiveId(null);
        liveIdRef.current = null;
        busyRef.current = false;
        setMessages((m) => [
          // A mid-turn/tool exception would otherwise leave a `pending` tool
          // block forever → `running` stuck true and the send button disabled.
          // Close out any in-flight tool blocks as interrupted.
          ...m.map((msg) =>
            hasPendingTool(msg.blocks)
              ? {
                  ...msg,
                  blocks: msg.blocks.map((b) =>
                    b.kind === "tool" && b.pending
                      ? { ...b, pending: false, content: b.content === "…" ? "(interrupted)" : b.content }
                      : b,
                  ),
                }
              : msg,
          ),
          { id: mid(), role: "assistant", blocks: [{ kind: "text", text: `[error] ${ev.message}` }] },
        ]);
        break;
      }
    }
  }

  async function addFiles(files: FileList | File[] | null) {
    if (!files?.length) return;
    const items = await Promise.all(
      Array.from(files)
        .filter((f) => f.type.startsWith("image/"))
        .map(readImage),
    );
    const ok = items.filter((x): x is Attachment => !!x);
    if (ok.length) setAttachments((a) => [...a, ...ok]);
  }

  function send() {
    if (busyRef.current) return; // one turn at a time
    const text = input.trim();
    const ws = wsRef.current;
    if ((!text && attachments.length === 0) || !ws || !session || !g.connected) return;
    const agentId = target ?? session.agent_id ?? g.agents[0]?.id ?? null;
    if (agentId && agentId !== session.agent_id) g.setSessionAgent(session.id, agentId);
    const live = mid();
    const turnId = newTurnId();
    const guessName = agentById(agentId)?.name ?? "Agent";
    const imgs = attachments;
    const userBlocks: Block[] = [
      ...imgs.map((a) => ({ kind: "image" as const, url: a.preview })),
      ...(text ? [{ kind: "text" as const, text }] : []),
    ];
    busyRef.current = true;
    setMessages((m) => [
      ...m,
      { id: mid(), role: "user", blocks: userBlocks, turnId },
      { id: live, role: "assistant", blocks: [], agentId: agentId, agentName: guessName, turnId },
    ]);
    setLiveId(live);
    liveIdRef.current = live;
    setStreamAgent(agentId);
    try {
      ws.send(
        JSON.stringify({
          type: "invoke",
          message: text,
          agent_id: agentId,
          turn_id: turnId,
          images: imgs.map((a) => ({ data: a.data, media_type: a.mediaType })),
        }),
      );
    } catch {
      // ws.send throws InvalidStateError when the socket is CLOSING/CLOSED (and
      // the `g.connected` guard lags the real readyState). Without this the
      // exception escapes the handler, busyRef stays locked, and the optimistic
      // "Thinking…" bubble never resolves → chat frozen. Unlock + mark the bubble.
      busyRef.current = false;
      setLiveId(null);
      liveIdRef.current = null;
      setMessages((m) =>
        m.map((msg) =>
          msg.id === live
            ? { ...msg, blocks: [{ kind: "text", text: "[error] 发送失败：连接未就绪，请稍后重试" }] }
            : msg,
        ),
      );
      return;
    }
    setInput("");
    setAttachments([]);
    setTarget(null);
  }

  function respond(decision: "allow" | "deny") {
    wsRef.current?.send(JSON.stringify({ type: "permission_response", decision }));
    setPermission(null);
  }

  function respondPropose(decision: "allow" | "deny") {
    // Reuses the permission_response channel; the server resumes the proposal
    // interrupt with {decision}, and the propose_edit tool applies on allow.
    wsRef.current?.send(JSON.stringify({ type: "permission_response", decision }));
    setPropose(null);
  }

  // Drag the composer's top handle to resize the input area. Auto-grow (capped)
  // still applies while composerH is undefined; dragging switches to a fixed,
  // scrollable height. The flex layout (message list = flex-1) keeps the overall
  // experience intact — the list simply takes the remaining space.
  function onResizeStart(e: React.PointerEvent) {
    const box = composerBoxRef.current;
    if (!box) return;
    dragRef.current = { startY: e.clientY, startH: box.offsetHeight };
    const minH = 96;
    const maxH = Math.max(160, Math.floor(window.innerHeight * 0.7));
    const move = (ev: PointerEvent) => {
      const d = dragRef.current;
      if (!d) return;
      const h = Math.min(maxH, Math.max(minH, d.startH + (d.startY - ev.clientY)));
      setComposerH(h);
    };
    const up = () => {
      dragRef.current = null;
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  const agentById = (id?: string | null) => g.agents.find((a) => a.id === id) ?? null;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div
        ref={scrollRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}
        className="flex-1 overflow-y-auto px-6 py-6"
      >
        <div className="mx-auto flex max-w-3xl flex-col gap-5">
          {messages.length === 0 && (
            <div className="py-16 text-center text-sm text-faint">
              Start a conversation. The agent will use tools and may ask for permission.
            </div>
          )}

          {messages.map((m) =>
            m.role === "user" ? (
              <div key={m.id} className="flex flex-col items-end gap-1">
                <TurnIdChip turnId={m.turnId} />
                <div className="max-w-[78%] rounded-2xl bg-card2 px-4 py-2.5 text-sm leading-relaxed text-txt">
                  <UserBlocks blocks={m.blocks} />
                </div>
              </div>
            ) : (
              <AssistantBubble
                key={m.id}
                agent={agentById(m.agentId)}
                agentName={m.agentName}
                blocks={m.blocks}
                streaming={m.id === liveId}
                turnId={m.turnId}
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

      {propose && (
        <div className="mx-auto w-full max-w-3xl px-6">
          <div className="mb-2 rounded-xl border border-violet/40 bg-violet/10 p-3">
            <div className="mb-1 flex items-center gap-2 text-sm font-medium text-violet">
              工作流修改待确认
              <span className="rounded border border-violet/40 px-1.5 py-0.5 text-[10px] font-normal">
                {propose.workflow_id} · v{propose.from_version} → 新版本
              </span>
            </div>
            {propose.rationale && (
              <div className="mb-2 text-xs text-muted">理由：{propose.rationale}</div>
            )}
            <DiffView diff={propose.diff} />
            <div className="mt-3 flex gap-2">
              <button
                onClick={() => respondPropose("allow")}
                className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90"
              >
                应用（创建新版本）
              </button>
              <button
                onClick={() => respondPropose("deny")}
                className="rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:text-txt"
              >
                拒绝
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
              onClick={() => g.newSession(g.agents[0]?.id)}
              className="flex items-center gap-1.5 rounded-lg border border-line bg-card px-2.5 py-1 text-xs text-muted hover:text-txt"
            >
              + New Session
            </button>
          </div>

          <div
            ref={composerBoxRef}
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              void addFiles(e.dataTransfer.files);
            }}
            className={`relative rounded-2xl border bg-card p-2.5 transition-colors focus-within:border-line2 ${
              dragOver ? "border-violet/70 ring-2 ring-violet/30" : "border-line"
            }`}
          >
            <div
              onPointerDown={onResizeStart}
              title="拖拽调整输入框高度（双击还原自动高度）"
              onDoubleClick={() => setComposerH(undefined)}
              className="absolute -top-1 left-1/2 z-10 flex h-2 w-12 -translate-x-1/2 cursor-ns-resize items-center justify-center rounded-full hover:bg-line2/60"
              aria-label="调整输入框高度"
            >
              <span className="h-0.5 w-6 rounded-full bg-line2" />
            </div>
            {attachments.length > 0 && (
              <div className="mb-2 flex flex-wrap gap-2">
                {attachments.map((a, i) => (
                  <div key={i} className="group relative">
                    <img
                      src={a.preview}
                      alt={a.name}
                      title={a.name}
                      className="h-14 w-14 rounded-lg border border-line object-cover"
                    />
                    <button
                      onClick={() => setAttachments((l) => l.filter((_, j) => j !== i))}
                      aria-label={`移除 ${a.name}`}
                      className="absolute -right-1.5 -top-1.5 flex h-[18px] w-[18px] items-center justify-center rounded-full bg-red text-white opacity-0 shadow transition-opacity group-hover:opacity-100"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                ))}
              </div>
            )}
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onPaste={(e) => {
                if (e.clipboardData?.files?.length) {
                  e.preventDefault();
                  void addFiles(e.clipboardData.files);
                }
              }}
              onKeyDown={(e) => {
                // Guard IME composition: without this, pressing Enter to commit a
                // CJK candidate (e.g. Chinese) fires send() mid-composition and
                // posts partial/garbled text. isComposing + keyCode 229 cover it.
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) {
                  e.preventDefault();
                  send();
                }
              }}
              rows={2}
              placeholder="Ask any Agent …  可粘贴 / 拖入截图  (try: 用 stat_list 卡片展示 PR 状态)"
              style={
                composerH != null
                  ? { height: composerH - 56, minHeight: 44, overflowY: "auto" }
                  : { maxHeight: 240 }
              }
              className="w-full resize-none bg-transparent px-1.5 py-1 text-sm text-txt outline-none placeholder:text-faint"
            />
            <div className="flex items-center justify-between px-1 pt-1">
              <div className="flex items-center gap-1 text-faint">
                <input
                  ref={fileRef}
                  type="file"
                  accept="image/*"
                  multiple
                  className="hidden"
                  onChange={(e) => {
                    void addFiles(e.target.files);
                    e.target.value = "";
                  }}
                />
                <button
                  onClick={() => fileRef.current?.click()}
                  className="rounded-md p-1.5 transition-colors hover:bg-card2 hover:text-muted"
                  title="添加图片（也可直接粘贴 / 拖拽）"
                >
                  <Paperclip className="h-4 w-4" />
                </button>
                <button
                  onClick={() => setInput((v) => v + "/")}
                  className="rounded-md p-1.5 hover:bg-card2 hover:text-muted"
                  title="插入斜杠命令 /"
                  aria-label="插入斜杠命令"
                >
                  <Keyboard className="h-4 w-4" />
                </button>
                <span className="px-1 text-xs">/</span>
                {(() => {
                  const live = wsStatus === "live";
                  const dot =
                    wsStatus === "live"
                      ? "#22c55e"
                      : wsStatus === "offline"
                        ? "#ef4444"
                        : "#eab308";
                  const label =
                    wsStatus === "live"
                      ? "已连接"
                      : wsStatus === "reconnecting"
                        ? "重连中"
                        : wsStatus === "offline"
                          ? "离线"
                          : "连接中";
                  const tip = live
                    ? "实时连接正常"
                    : wsStatus === "reconnecting"
                      ? "连接中断，正在自动重连…（点击立即重试）"
                      : wsStatus === "offline"
                        ? "未连接到运行时（点击重试）"
                        : "正在连接…";
                  return (
                    <button
                      type="button"
                      onClick={() => {
                        if (!live) connectRef.current();
                      }}
                      title={tip}
                      aria-label={`连接状态：${label}${live ? "" : "，点击重连"}`}
                      className={`ml-1 flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] transition-colors ${
                        live ? "cursor-default" : "cursor-pointer hover:bg-card2"
                      }`}
                      style={{ color: dot }}
                    >
                      <span
                        className={`h-1.5 w-1.5 rounded-full ${
                          live || wsStatus === "offline" ? "" : "animate-pulse"
                        }`}
                        style={{ background: dot }}
                      />
                      {label}
                    </button>
                  );
                })()}
              </div>
              <button
                onClick={send}
                disabled={!g.connected || !!permission || running || (!input.trim() && attachments.length === 0)}
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
  turnId,
}: {
  agent: AgentConfig | null;
  agentName?: string;
  blocks: Block[];
  streaming?: boolean;
  turnId?: string;
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
          <span className="ml-auto">
            <TurnIdChip turnId={turnId} />
          </span>
        </div>
        <div className="rounded-xl border border-line bg-card px-4 py-3 text-sm leading-relaxed text-txt">
          {hasInner ? (
            <InnerBlocks blocks={blocks} streaming={streaming} />
          ) : streaming ? (
            <span className="inline-flex items-center gap-1 text-muted">
              <span className="flex gap-0.5">
                <Dot /> <Dot d={150} /> <Dot d={300} />
              </span>
              <span className="ml-1 text-xs">Thinking…</span>
            </span>
          ) : (
            // A finished turn with no inner content (e.g. refs-only) must NOT keep
            // pulsing "Thinking…" — that read as a permanently-stuck indicator.
            <span className="text-xs text-faint">（空回复）</span>
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
