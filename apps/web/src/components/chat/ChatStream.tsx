"use client";

import { useEffect, useRef, useState } from "react";
import { Paperclip, Keyboard, ArrowUp, X, AlertCircle, Loader2 } from "lucide-react";
import { useGinno } from "@/lib/store";
import { openSessionSocket, getSessionHistory, uploadFile, debugLog, attachFilePath } from "@/lib/runtime";
import { agentHex } from "@/lib/theme";
import { Icon } from "@/components/icons";
import { InnerBlocks, RefBlocks, UserBlocks, hasPendingTool, type Block } from "@/components/chat/blocks";
import { DiffView } from "@/components/workflow/DiffView";
import { ComposerMenu } from "@/components/chat/ComposerMenu";
import {
  applySelection,
  buildMenuItems,
  dedupeMentions,
  detectTrigger,
  pruneMentions,
  type MenuItem,
  type ResolvedMention,
  type Trigger,
} from "@/components/chat/commandMenu";
import type { AgentConfig, SessionMeta } from "@/lib/types";

interface ChatMsg {
  id: string;
  role: "user" | "assistant";
  blocks: Block[];
  agentId?: string | null;
  agentName?: string;
  turnId?: string; // per-turn trace UUID (shown on the bubble, greppable in sidecar logs)
  // Delivery state — user bubbles only. "sending" = in flight to the sidecar;
  // "failed" = never delivered (red ❗, click to retry). Successful delivery
  // clears it back to undefined.
  status?: "sending" | "failed";
  failReason?: string;
  // Immutable snapshot of everything the turn carries, kept on the bubble so
  // a failed send can be retried or re-edited without losing content.
  sendPayload?: SendPayload;
  // Assistant turn-error card: the input WAS delivered but the run errored
  // (model/provider failure etc.). blocks[0] holds the error text;
  // sendPayload carries the originating user turn for the retry button.
  error?: boolean;
  // Error cards only: id of the originating user bubble. Retry operates on
  // THAT bubble in place — no duplicate message is appended.
  sourceMsgId?: string;
}

interface SendPayload {
  text: string;
  images: Attachment[];
  files: FileAttachment[];
  mentions: ResolvedMention[];
  agentId: string | null;
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

/** Non-image attachment: uploaded to the sidecar, referenced by registry id. */
interface FileAttachment {
  id: string;
  name: string;
  path: string;
  kind: string; // spreadsheet | table | document | presentation | pdf | …
  uploading?: boolean;
}

const TABLE_KINDS = new Set(["spreadsheet", "table"]);

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
  const [fileAttachments, setFileAttachments] = useState<FileAttachment[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const [target, setTarget] = useState<string | null>(null);
  const [permission, setPermission] = useState<PermissionPrompt | null>(null);
  const [propose, setPropose] = useState<VersionPropose | null>(null);
  const [wsStatus, setWsStatus] = useState<"connecting" | "live" | "reconnecting" | "offline">("connecting");
  // Composer height: undefined = auto-grow (capped); a number = user-dragged size.
  const [composerH, setComposerH] = useState<number | undefined>(undefined);
  // Command/mention autocomplete: open menu (items + active index + trigger).
  const [menu, setMenu] = useState<{ items: MenuItem[]; active: number; trigger: Trigger } | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true); // auto-scroll only while the user is near the bottom
  const fileRef = useRef<HTMLInputElement | null>(null);
  const composerBoxRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const dragRef = useRef<{ startY: number; startH: number } | null>(null);
  // liveIdRef mirrors liveId state for use inside callbacks without closure staleness
  const liveIdRef = useRef<string | null>(null);
  // connectRef: the reconnect button always calls this; updated on each session switch
  const connectRef = useRef<() => void>(() => {});
  // Which session is currently shown; used by syncDisplay to skip background updates
  const curSessionIdRef = useRef<string | null>(null);
  // ─── Per-session persistent stores ─────────────────────────────────────────
  // Sockets stay open across session switches; only closed on unmount or delete.
  // Background sockets keep feeding their session's store; syncDisplay() mirrors
  // that into React state only when the session is currently displayed — so
  // switching back mid-reply shows the stream continuing live.
  const storeRef         = useRef<Record<string, ChatMsg[]>>({});
  const liveBySessionRef = useRef<Record<string, string | null>>({});
  const socketsRef       = useRef<Record<string, WebSocket>>({});
  const statusRef        = useRef<Record<string, "connecting" | "live" | "reconnecting" | "offline">>({});
  const permsRef         = useRef<Record<string, PermissionPrompt | null>>({});
  const proposeRef       = useRef<Record<string, VersionPropose | null>>({});
  const busyBySessionRef = useRef<Record<string, boolean>>({});
  const streamAgentRef   = useRef<Record<string, string | null>>({});
  const pingTimerRef     = useRef<Record<string, ReturnType<typeof setInterval> | null>>({});
  const watchTimerRef    = useRef<Record<string, ReturnType<typeof setInterval> | null>>({});
  const reconnTimerRef   = useRef<Record<string, ReturnType<typeof setTimeout> | null>>({});
  const lastSeenRef      = useRef<Record<string, number>>({});
  // Unsent input + attachments + resolved mentions saved per session on switch
  const draftCacheRef    = useRef<Record<string, { input: string; attachments: Attachment[]; mentions?: ResolvedMention[] }>>({});
  // Resolved @mentions picked from the autocomplete menu, keyed by session id.
  // Pruned on every input change (edited-away token → dropped mention) and
  // sent along with the invoke payload as the authoritative structured list.
  const mentionsRef      = useRef<Record<string, ResolvedMention[]>>({});

  // Push the given session's ref state into React display state.
  // No-op when sid is not the currently displayed session (background socket).
  const syncDisplay = (sid: string) => {
    if (sid !== curSessionIdRef.current) return;
    const lid = liveBySessionRef.current[sid] ?? null;
    setMessages([...(storeRef.current[sid] ?? [])]);
    setLiveId(lid);
    liveIdRef.current = lid;
    setWsStatus(statusRef.current[sid] ?? "connecting");
    setPermission(permsRef.current[sid] ?? null);
    setPropose(proposeRef.current[sid] ?? null);
    setStreamAgent(streamAgentRef.current[sid] ?? null);
  };

  // Drop per-session state for deleted sessions to prevent memory leaks.
  useEffect(() => {
    const live = new Set(g.sessions.map((s) => s.id));
    for (const id of Object.keys(storeRef.current)) {
      if (!live.has(id)) {
        if (reconnTimerRef.current[id]) clearTimeout(reconnTimerRef.current[id]!);
        if (pingTimerRef.current[id])   clearInterval(pingTimerRef.current[id]!);
        if (watchTimerRef.current[id])  clearInterval(watchTimerRef.current[id]!);
        try { socketsRef.current[id]?.close(); } catch { /* ignore */ }
        delete socketsRef.current[id];    delete storeRef.current[id];
        delete liveBySessionRef.current[id]; delete statusRef.current[id];
        delete permsRef.current[id];      delete proposeRef.current[id];
        delete busyBySessionRef.current[id]; delete streamAgentRef.current[id];
        delete draftCacheRef.current[id]; delete pingTimerRef.current[id];
        delete watchTimerRef.current[id]; delete reconnTimerRef.current[id];
        delete lastSeenRef.current[id];
      }
    }
  }, [g.sessions]);

  // Close all persistent sockets on component unmount.
  useEffect(() => {
    return () => {
      for (const sid of Object.keys(socketsRef.current)) {
        if (reconnTimerRef.current[sid]) clearTimeout(reconnTimerRef.current[sid]!);
        if (pingTimerRef.current[sid])   clearInterval(pingTimerRef.current[sid]!);
        if (watchTimerRef.current[sid])  clearInterval(watchTimerRef.current[sid]!);
        try { socketsRef.current[sid].close(); } catch { /* ignore */ }
      }
    };
  }, []);

  // Open (or reuse) a per-session WebSocket. Sockets stay open when the user
  // switches sessions; they are only closed on unmount or session delete.
  function connectSession(sid: string) {
    const existing = socketsRef.current[sid];
    if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)) return;
    if (reconnTimerRef.current[sid]) { clearTimeout(reconnTimerRef.current[sid]!); reconnTimerRef.current[sid] = null; }
    statusRef.current[sid] = "connecting";
    syncDisplay(sid);
    const sock = openSessionSocket(sid);
    socketsRef.current[sid] = sock;
    sock.onopen = () => {
      if (socketsRef.current[sid] !== sock) return;
      g.setConnected(true);
      statusRef.current[sid] = "live";
      lastSeenRef.current[sid] = Date.now();
      if (liveBySessionRef.current[sid]) {
        liveBySessionRef.current[sid] = null;
        streamAgentRef.current[sid] = null;
        busyBySessionRef.current[sid] = false;
        // An in-flight turn does not survive a socket drop: any user bubble
        // still "sending" is marked failed so it gets the retry affordance.
        storeRef.current[sid] = (storeRef.current[sid] ?? []).map((m) =>
          m.role === "user" && m.status === "sending"
            ? { ...m, status: "failed" as const, failReason: "连接中断，未送达" }
            : m,
        );
      }
      syncDisplay(sid);
      pingTimerRef.current[sid] = setInterval(() => {
        const s = socketsRef.current[sid];
        if (s?.readyState === WebSocket.OPEN) {
          try { s.send(JSON.stringify({ type: "ping" })); } catch { /* ignore */ }
        }
      }, 20000);
      watchTimerRef.current[sid] = setInterval(() => {
        if (Date.now() - (lastSeenRef.current[sid] ?? Date.now()) > 45000) {
          try { socketsRef.current[sid]?.close(); } catch { /* ignore */ }
        }
      }, 10000);
    };
    sock.onmessage = (e) => {
      if (socketsRef.current[sid] !== sock) return;
      lastSeenRef.current[sid] = Date.now();
      try { handle(sid, JSON.parse(e.data)); } catch { /* ignore */ }
    };
    sock.onerror = () => {
      if (socketsRef.current[sid] !== sock) return;
      try { sock.close(); } catch { /* ignore */ }
    };
    sock.onclose = () => {
      if (pingTimerRef.current[sid]) { clearInterval(pingTimerRef.current[sid]!); pingTimerRef.current[sid] = null; }
      if (watchTimerRef.current[sid]) { clearInterval(watchTimerRef.current[sid]!); watchTimerRef.current[sid] = null; }
      if (socketsRef.current[sid] !== sock) return;
      delete socketsRef.current[sid];
      statusRef.current[sid] = "reconnecting";
      syncDisplay(sid);
      reconnTimerRef.current[sid] = setTimeout(() => {
        reconnTimerRef.current[sid] = null;
        connectSession(sid);
      }, 3000);
    };
  }

  // When the active session changes: save draft, restore draft, connect socket,
  // load history, and sync display state from refs → React state.
  useEffect(() => {
    if (!session) return;
    const sid = session.id;
    const prev = curSessionIdRef.current;

    // Save outgoing draft
    if (prev && prev !== sid) {
      draftCacheRef.current[prev] = {
        input,
        attachments,
        mentions: pruneMentions(mentionsRef.current[prev] ?? [], input),
      };
      setInput("");
      setAttachments([]);
      setTarget(null);
      setMenu(null); // menu is composer-global state; never leak across sessions
    }

    curSessionIdRef.current = sid;
    connectRef.current = () => connectSession(sid);

    connectSession(sid);

    // Load history if this session has no messages yet
    if (!storeRef.current[sid]) {
      storeRef.current[sid] = [];
      getSessionHistory(sid).then((res) => {
        if (!res?.messages?.length) return;
        storeRef.current[sid] = res.messages.map((m) => ({
          id: m.id ?? mid(),
          role: m.role,
          blocks: m.blocks,
          agentId: m.agentId,
        }));
        syncDisplay(sid);
      });
    }

    // Restore draft if any (mentions re-pruned against the restored text so a
    // token the user deleted before switching stays deleted)
    const draft = draftCacheRef.current[sid];
    if (draft) {
      setInput(draft.input);
      setAttachments(draft.attachments);
      mentionsRef.current[sid] = pruneMentions(draft.mentions ?? [], draft.input);
    }

    syncDisplay(sid);
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

  // Delivery confirmed (the server started or finished the turn) → clear the
  // "sending" marker off user bubbles.
  function markDelivered(sid: string) {
    const list = storeRef.current[sid];
    if (!list?.some((m) => m.status === "sending")) return;
    storeRef.current[sid] = list.map((m) =>
      m.status === "sending" ? { ...m, status: undefined, failReason: undefined } : m,
    );
  }

  function ensureLive(sid: string): string {
    const existing = liveBySessionRef.current[sid];
    if (existing) return existing;
    const id = mid();
    storeRef.current[sid] = [
      ...(storeRef.current[sid] ?? []),
      { id, role: "assistant", blocks: [], agentId: streamAgentRef.current[sid], turnId: newTurnId() },
    ];
    liveBySessionRef.current[sid] = id;
    return id;
  }

  function mutateLive(sid: string, ev: { event: string; [k: string]: unknown }) {
    const id = ensureLive(sid);
    storeRef.current[sid] = (storeRef.current[sid] ?? []).map((msg) =>
      msg.id === id ? { ...msg, blocks: applyBlock(msg.blocks, ev) } : msg,
    );
  }

  function handle(sid: string, ev: { event: string; [k: string]: unknown }) {
    switch (ev.event) {
      case "token.delta":
      case "thinking.delta":
      case "tool.start":
      case "tool.end":
      case "widget.emit":
      case "ref.emit":
      case "workflow.emit":
        mutateLive(sid, ev);
        break;
      case "turn.start": {
        markDelivered(sid);
        // authoritative agent for this turn (server-resolved, never null).
        // The server echoes the turn_id we sent (or mints one); adopt it as the
        // bubble's trace UUID so it matches the sidecar logs exactly.
        const id = liveBySessionRef.current[sid];
        if (id) {
          const srvTurn = ev.turn_id as string | undefined;
          storeRef.current[sid] = (storeRef.current[sid] ?? []).map((msg) =>
            msg.id === id
              ? {
                  ...msg,
                  agentId: (ev.agent_id as string) || null,
                  agentName: ev.name as string,
                  turnId: srvTurn || msg.turnId,
                }
              : msg,
          );
          // keep the user bubble's UUID in sync with the server's authoritative one
          if (srvTurn) {
            storeRef.current[sid] = (storeRef.current[sid] ?? []).map((msg, i, arr) =>
              msg.role === "user" && !msg.turnId && i === arr.length - 2
                ? { ...msg, turnId: srvTurn }
                : msg,
            );
          }
        }
        break;
      }
      case "permission.request":
        permsRef.current[sid] = { tool: ev.tool as string, args: ev.args };
        break;
      case "version.propose":
        proposeRef.current[sid] = {
          workflow_id: ev.workflow_id as string,
          from_version: (ev.from_version as number) ?? 0,
          diff: (ev.diff as string) ?? "",
          rationale: (ev.rationale as string) ?? "",
        };
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
      case "preview.emit":
        // Agent produced a previewable file (e.g. analysis result) → open it.
        if (ev.open && ev.file_id) {
          g.openPreview({
            id: ev.file_id as string,
            name: (ev.name as string) || "result",
            path: (ev.path as string) || "",
            kind: ev.kind as string | undefined,
          });
        }
        g.reloadArtifacts();
        break;
      case "preview.invalidate":
        // A tracked file changed (tool wrote it / mtime watcher) → the
        // SheetViewer refetches if that file is the one being viewed.
        if (ev.file_id) g.notifyPreviewInvalidate(ev.file_id as string);
        break;
      case "notice":
        // Built-in command reply (e.g. /help): no graph turn ran, so the server
        // pushes the rendered text directly into the live bubble as one delta.
        markDelivered(sid);
        mutateLive(sid, { event: "token.delta", content: (ev.message as string) || "" });
        break;
      case "message.end":
        markDelivered(sid);
        liveBySessionRef.current[sid] = null;
        streamAgentRef.current[sid] = null;
        busyBySessionRef.current[sid] = false;
        break;
      case "error": {
        // The turn reached the server (it is the run, not the delivery, that
        // failed) → the user bubble counts as delivered; the failure becomes
        // a dedicated error card with a retry action.
        markDelivered(sid);
        const liveMsgId = liveBySessionRef.current[sid];
        liveBySessionRef.current[sid] = null;
        streamAgentRef.current[sid] = null;
        busyBySessionRef.current[sid] = false;
        const list = storeRef.current[sid] ?? [];
        const liveBubble = list.find((m) => m.id === liveMsgId);
        // Retry payload: the originating user turn's snapshot (live turns
        // always carry one). Absent → the card renders without a retry button.
        const lastUser = [...list].reverse().find((m) => m.role === "user" && m.sendPayload);
        storeRef.current[sid] = [
          ...list
            // An empty live bubble (error before any token) would render as a
            // confusing "（空回复）" right above the error card — drop it.
            .filter((m) => !(m.id === liveMsgId && m.blocks.length === 0))
            // Close out any in-flight tool blocks as interrupted so `running` unsticks.
            .map((msg) =>
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
          {
            id: mid(),
            role: "assistant" as const,
            blocks: [{ kind: "text", text: String(ev.message || "") || "未知错误" }],
            turnId: liveBubble?.turnId,
            error: true,
            sendPayload: lastUser?.sendPayload,
            sourceMsgId: lastUser?.id,
          },
        ];
        break;
      }
    }
    syncDisplay(sid);
  }

  async function addFiles(files: FileList | File[] | null) {
    // [DEBUG] telemetry for WKWebView drag & drop diagnosis
    void debugLog({
      where: "addFiles:enter",
      hasSession: !!session,
      count: files?.length ?? 0,
      files: Array.from(files ?? []).map((f) => ({ name: f.name, type: f.type, size: f.size })),
    });
    if (!files?.length || !session) return;
    const sid = session.id;
    const list = Array.from(files);
    // Images keep the base64 → multimodal path; everything else is uploaded
    // to the sidecar and attached by registry ref (docs §7.2).
    const images = list.filter((f) => f.type.startsWith("image/"));
    const docs = list.filter((f) => !f.type.startsWith("image/"));
    void debugLog({ where: "addFiles:split", images: images.length, docs: docs.length });
    if (images.length) {
      const items = await Promise.all(images.map(readImage));
      const ok = items.filter((x): x is Attachment => !!x);
      if (ok.length) setAttachments((a) => [...a, ...ok]);
    }
    for (const f of docs) {
      // optimistic chip (uploading state) so the user gets instant feedback
      const tmpId = `up-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      setFileAttachments((a) => [
        ...a,
        { id: tmpId, name: f.name, path: "", kind: "", uploading: true },
      ]);
      try {
        const r = await uploadFile(sid, f);
        void debugLog({ where: "addFiles:upload-resp", name: f.name, ok: r?.ok, hasFile: !!r?.file, error: r?.error });
        if (r.ok && r.file) {
          const entry = r.file;
          setFileAttachments((a) =>
            a.map((x) =>
              x.id === tmpId
                ? { id: entry.id, name: entry.name, path: entry.path, kind: entry.kind }
                : x,
            ),
          );
          // spreadsheets/tables auto-open the preview on drop
          if (TABLE_KINDS.has(entry.kind)) {
            g.openPreview({ id: entry.id, name: entry.name, path: entry.path, kind: entry.kind });
          }
          g.reloadArtifacts();
        } else {
          setFileAttachments((a) => a.filter((x) => x.id !== tmpId));
        }
      } catch (e) {
        void debugLog({ where: "addFiles:upload-error", name: f.name, error: String(e) });
        setFileAttachments((a) => a.filter((x) => x.id !== tmpId));
      }
    }
  }

  // Native OS file-drop bridge for the desktop app. WKWebView never fires the
  // HTML5 onDrop for Finder drags, so the Tauri shell handles the drop and
  // forwards the native paths here (lib.rs → window.eval). Browsers use the
  // HTML5 path above instead; this is a no-op there (nothing calls it).
  useEffect(() => {
    (window as unknown as { __ginnoFileDrop?: (p: string[]) => void }).__ginnoFileDrop = (
      paths: string[],
    ) => void attachPaths(paths);
    return () => {
      delete (window as unknown as { __ginnoFileDrop?: unknown }).__ginnoFileDrop;
    };
  });

  async function attachPaths(paths: string[]) {
    void debugLog({ where: "attachPaths", paths });
    if (!session || !paths?.length) return;
    const sid = session.id;
    for (const p of paths) {
      const name = p.split("/").pop() || p;
      const tmpId = `path-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      setFileAttachments((a) => [...a, { id: tmpId, name, path: p, kind: "", uploading: true }]);
      try {
        const r = await attachFilePath(sid, p);
        void debugLog({ where: "attachPaths:resp", name, ok: r?.ok, error: r?.error });
        if (r.ok && r.file) {
          const entry = r.file;
          setFileAttachments((a) =>
            a.map((x) =>
              x.id === tmpId
                ? { id: entry.id, name: entry.name, path: entry.path, kind: entry.kind }
                : x,
            ),
          );
          if (TABLE_KINDS.has(entry.kind)) {
            g.openPreview({ id: entry.id, name: entry.name, path: entry.path, kind: entry.kind });
          }
          g.reloadArtifacts();
        } else {
          setFileAttachments((a) => a.filter((x) => x.id !== tmpId));
        }
      } catch (e) {
        void debugLog({ where: "attachPaths:error", name, error: String(e) });
        setFileAttachments((a) => a.filter((x) => x.id !== tmpId));
      }
    }
  }

  // ─── Command / mention autocomplete ────────────────────────────────────────
  // Re-evaluate whether the composer's current text+caret opens a menu. Called
  // on every input change and after programmatic edits (the "/" button).
  function recomputeMenu(text: string) {
    const caret = textareaRef.current?.selectionStart ?? text.length;
    const trigger = detectTrigger(text, caret);
    if (!trigger) {
      setMenu(null);
      return;
    }
    const items = buildMenuItems(trigger, {
      skills: g.skills,
      agents: g.agents,
      workflows: g.workflows,
      artifacts: g.artifacts,
    });
    setMenu(items.length ? { items, active: 0, trigger } : null);
  }

  // Insert the picked item, record its resolved mention, and refocus the caret
  // right after the inserted token (ready to type the prompt).
  function pickItem(item: MenuItem) {
    if (!menu) return;
    const caret = textareaRef.current?.selectionStart ?? input.length;
    const r = applySelection(input, caret, menu.trigger, item);
    setInput(r.text);
    setMenu(null);
    if (r.mention && session) {
      const sid = session.id;
      const rest = (mentionsRef.current[sid] ?? []).filter(
        (m) => !(m.kind === r.mention!.kind && m.id === r.mention!.id),
      );
      mentionsRef.current[sid] = [...rest, r.mention];
    }
    // @agent also retargets the turn — same code path as the "Ask X" chips, so
    // the structured mention and agent_id never disagree from the UI.
    if (item.kind === "agent") setTarget(item.id);
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (el) {
        el.focus();
        el.setSelectionRange(r.caret, r.caret);
      }
    });
  }

  function send() {
    if (!session) return;
    const sid = session.id;
    if (busyBySessionRef.current[sid]) return; // one turn at a time
    const text = input.trim();
    const readyFiles = fileAttachments.filter((f) => !f.uploading);
    if (!text && attachments.length === 0 && readyFiles.length === 0) return;
    if (readyFiles.length !== fileAttachments.length) return; // upload in flight
    const agentId = target ?? session.agent_id ?? g.agents[0]?.id ?? null;
    if (agentId && agentId !== session.agent_id) g.setSessionAgent(session.id, agentId);
    // Final prune against the raw (untrimmed) input, deduped — only mentions
    // whose @kind:label token is still present are sent. The server treats this
    // structured list as authoritative (text tokens are its raw-client fallback).
    const mentions = dedupeMentions(pruneMentions(mentionsRef.current[sid] ?? [], input));
    // Not connected? No silent no-op — the bubble is still created and lands
    // in the failed state (red ❗) so the send attempt always has a visible
    // outcome and can be retried.
    attemptSend(sid, { text, images: attachments, files: readyFiles, mentions, agentId });
    setInput("");
    setAttachments([]);
    setFileAttachments([]);
    setTarget(null);
    setMenu(null);
    mentionsRef.current[sid] = [];
  }

  /**
   * Post one user turn. The user bubble is created up-front and carries the
   * full send payload, so a failed delivery shows a red ❗ and can be retried
   * (or re-edited) without losing content. Passing `userMsgId` reuses an
   * existing failed bubble in place (retry); otherwise a new one is appended.
   */
  function attemptSend(sid: string, payload: SendPayload, userMsgId?: string) {
    const turnId = newTurnId();
    const guessName = agentById(payload.agentId)?.name ?? "Agent";
    const userBlocks: Block[] = [
      ...payload.files.map((f) => ({
        kind: "file" as const,
        fileId: f.id,
        name: f.name,
        path: f.path,
        fileKind: f.kind,
      })),
      ...payload.images.map((a) => ({ kind: "image" as const, url: a.preview })),
      ...(payload.text ? [{ kind: "text" as const, text: payload.text }] : []),
    ];
    const uid = userMsgId ?? mid();
    const sock = socketsRef.current[sid];
    const sockReady = !!sock && sock.readyState === WebSocket.OPEN;

    if (!sockReady) {
      // Not delivered: the bubble lands in the failed state. No assistant
      // placeholder, no busy lock — retry becomes available once the socket
      // reconnects.
      storeRef.current[sid] = userMsgId
        ? (storeRef.current[sid] ?? []).map((m) =>
            m.id === userMsgId
              ? { ...m, turnId, status: "failed" as const, failReason: "连接未就绪" }
              : m,
          )
        : [
            ...(storeRef.current[sid] ?? []),
            {
              id: uid,
              role: "user" as const,
              blocks: userBlocks,
              turnId,
              agentId: payload.agentId,
              status: "failed" as const,
              failReason: "连接未就绪",
              sendPayload: payload,
            },
          ];
      syncDisplay(sid);
      return;
    }

    const live = mid();
    busyBySessionRef.current[sid] = true;
    streamAgentRef.current[sid] = payload.agentId;
    const liveBubble: ChatMsg = {
      id: live,
      role: "assistant",
      blocks: [],
      agentId: payload.agentId,
      agentName: guessName,
      turnId,
    };
    storeRef.current[sid] = userMsgId
      ? // In-place retry: refresh the original bubble and insert the response
        // placeholder immediately after it — never duplicate the message.
        (storeRef.current[sid] ?? []).flatMap((m) =>
          m.id === userMsgId
            ? [{ ...m, turnId, status: "sending" as const, failReason: undefined }, liveBubble]
            : [m],
        )
      : [
          ...(storeRef.current[sid] ?? []),
          {
            id: uid,
            role: "user" as const,
            blocks: userBlocks,
            turnId,
            agentId: payload.agentId,
            status: "sending" as const,
            sendPayload: payload,
          },
          liveBubble,
        ];
    liveBySessionRef.current[sid] = live;
    syncDisplay(sid);
    try {
      sock!.send(
        JSON.stringify({
          type: "invoke",
          message: payload.text,
          agent_id: payload.agentId,
          turn_id: turnId,
          images: payload.images.map((a) => ({ data: a.data, media_type: a.mediaType })),
          files: payload.files.map((f) => ({ id: f.id, name: f.name, path: f.path })),
          ...(payload.mentions.length
            ? { mentions: payload.mentions.map(({ kind, id }) => ({ kind, id })) }
            : {}),
        }),
      );
    } catch {
      // sock.send throws when the socket flipped to CLOSING/CLOSED between the
      // readyState check and here. Drop the assistant placeholder and mark the
      // user bubble failed — the payload on it keeps retry/re-edit lossless.
      busyBySessionRef.current[sid] = false;
      liveBySessionRef.current[sid] = null;
      storeRef.current[sid] = (storeRef.current[sid] ?? [])
        .filter((m) => m.id !== live)
        .map((m) =>
          m.id === uid ? { ...m, status: "failed" as const, failReason: "连接中断，未送达" } : m,
        );
      syncDisplay(sid);
    }
  }

  /** Click on the red ❗: resend the exact payload with a fresh turn id (a
   * retry is a genuinely new turn). Operates on the failed bubble IN PLACE —
   * the bubble flips back to "sending" and the response slots in right after
   * it; no duplicate message is appended. */
  function retryFailed(msgId: string) {
    const sid = curSessionIdRef.current;
    if (!sid || busyBySessionRef.current[sid]) return; // one turn at a time
    const list = storeRef.current[sid] ?? [];
    const msg = list.find((m) => m.id === msgId);
    if (!msg || msg.role !== "user" || msg.status !== "failed" || !msg.sendPayload) return;
    attemptSend(sid, msg.sendPayload, msg.id);
  }

  /** Pull the failed payload back into the composer (text, images, files,
   * mentions, target agent) and drop the bubble — content is never lost. */
  function editResend(msgId: string) {
    const sid = curSessionIdRef.current;
    if (!sid) return;
    const msg = (storeRef.current[sid] ?? []).find((m) => m.id === msgId);
    if (!msg || msg.status !== "failed" || !msg.sendPayload) return;
    const p = msg.sendPayload;
    storeRef.current[sid] = (storeRef.current[sid] ?? []).filter((m) => m.id !== msgId);
    setInput(p.text);
    setAttachments(p.images);
    setFileAttachments(p.files);
    setTarget(p.agentId);
    mentionsRef.current[sid] = p.mentions;
    syncDisplay(sid);
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (el) {
        el.focus();
        el.setSelectionRange(p.text.length, p.text.length);
      }
      recomputeMenu(p.text);
    });
  }

  function dismissFailed(msgId: string) {
    const sid = curSessionIdRef.current;
    if (!sid) return;
    storeRef.current[sid] = (storeRef.current[sid] ?? []).filter(
      (m) => !(m.id === msgId && m.status === "failed"),
    );
    syncDisplay(sid);
  }

  /** Retry on a turn-error card: drop the card and re-invoke the originating
   * payload ON THE ORIGINAL user bubble in place (it flips back to "sending",
   * the response slots in right after it) — no duplicate message. Only if the
   * original bubble is somehow gone do we fall back to appending a fresh one. */
  function retryError(msgId: string) {
    const sid = curSessionIdRef.current;
    if (!sid || busyBySessionRef.current[sid]) return; // one turn at a time
    const list = storeRef.current[sid] ?? [];
    const card = list.find((m) => m.id === msgId);
    if (!card?.error || !card.sendPayload) return;
    storeRef.current[sid] = list.filter((m) => m.id !== msgId);
    const source = card.sourceMsgId
      ? (storeRef.current[sid] ?? []).find((m) => m.id === card.sourceMsgId && m.role === "user")
      : undefined;
    attemptSend(sid, card.sendPayload, source?.id);
  }

  function respond(decision: "allow" | "deny") {
    const sid = curSessionIdRef.current;
    if (!sid) return;
    try {
      socketsRef.current[sid]?.send(JSON.stringify({ type: "permission_response", decision }));
    } catch {
      /* socket gone — reconnect re-emits the prompt if still pending */
    }
    permsRef.current[sid] = null;
    setPermission(null);
  }

  function respondPropose(decision: "allow" | "deny") {
    const sid = curSessionIdRef.current;
    if (!sid) return;
    // Reuses the permission_response channel; the server resumes the proposal
    // interrupt with {decision}, and the propose_edit tool applies on allow.
    try {
      socketsRef.current[sid]?.send(JSON.stringify({ type: "permission_response", decision }));
    } catch {
      /* socket gone — reconnect re-emits version.propose if still pending */
    }
    proposeRef.current[sid] = null;
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
                {/* w-full (not max-w-full): the row width must be definite so the
                    bubble's max-w-[78%] resolves against the column, not against
                    the row's own shrink-to-fit width — otherwise the percentage
                    collapses the bubble and the text overflows to the right. */}
                <div className="group flex w-full items-center justify-end gap-2">
                  {m.status === "failed" && (
                    <div className="flex shrink-0 items-center gap-1.5">
                      <button
                        onClick={() => editResend(m.id)}
                        className="rounded-md border border-line2 px-1.5 py-0.5 text-[10px] text-muted opacity-0 transition-opacity hover:text-txt group-hover:opacity-100"
                      >
                        编辑重发
                      </button>
                      <button
                        onClick={() => dismissFailed(m.id)}
                        className="rounded-md border border-line2 px-1.5 py-0.5 text-[10px] text-muted opacity-0 transition-opacity hover:text-red group-hover:opacity-100"
                      >
                        删除
                      </button>
                      <button
                        onClick={() => retryFailed(m.id)}
                        title={`发送失败：${m.failReason ?? "未知原因"}（点击重试）`}
                        aria-label="发送失败，点击重试"
                        className="shrink-0 transition-transform hover:scale-110"
                      >
                        <AlertCircle className="h-[18px] w-[18px] text-red" />
                      </button>
                    </div>
                  )}
                  {m.status === "sending" && (
                    <Loader2 className="h-4 w-4 shrink-0 animate-spin text-faint" aria-label="发送中" />
                  )}
                  <div
                    className={`max-w-[78%] rounded-2xl rounded-tr-md border px-4 py-2.5 text-sm leading-relaxed ${
                      m.status === "failed"
                        ? "border-red/40 bg-card2/60 text-muted"
                        : "border-line bg-card2 text-txt"
                    }`}
                  >
                    <UserBlocks blocks={m.blocks} />
                  </div>
                </div>
                {m.status === "failed" && (
                  <div className="text-[10px] text-red/80">
                    发送失败{m.failReason ? `：${m.failReason}` : ""} · 点击红色感叹号重试
                  </div>
                )}
              </div>
            ) : m.error ? (
              <ErrorCard
                key={m.id}
                message={m.blocks[0]?.kind === "text" ? m.blocks[0].text : ""}
                turnId={m.turnId}
                canRetry={!!m.sendPayload}
                busy={running}
                onRetry={() => retryError(m.id)}
              />
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
            {menu && (
              <ComposerMenu
                items={menu.items}
                active={menu.active}
                onPick={pickItem}
                onHover={(i) => setMenu((m) => (m ? { ...m, active: i } : m))}
              />
            )}
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
            {fileAttachments.length > 0 && (
              <div className="mb-2 flex flex-wrap gap-2">
                {fileAttachments.map((f, i) => (
                  <div
                    key={f.id}
                    className="group relative flex items-center gap-1.5 rounded-lg border border-line bg-card2 px-2.5 py-1.5 text-xs text-txt"
                  >
                    <span>{TABLE_KINDS.has(f.kind) ? "📊" : "📄"}</span>
                    <span className="max-w-[180px] truncate" title={f.name}>
                      {f.name}
                    </span>
                    {f.uploading && <span className="text-faint">上传中…</span>}
                    <button
                      onClick={() => setFileAttachments((l) => l.filter((_, j) => j !== i))}
                      aria-label={`移除 ${f.name}`}
                      className="absolute -right-1.5 -top-1.5 flex h-[18px] w-[18px] items-center justify-center rounded-full bg-red text-white opacity-0 shadow transition-opacity group-hover:opacity-100"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                ))}
              </div>
            )}
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => {
                const v = e.target.value;
                setInput(v);
                recomputeMenu(v);
                // Keep the resolved mentions in sync with the visible tokens.
                if (session) {
                  mentionsRef.current[session.id] = pruneMentions(
                    mentionsRef.current[session.id] ?? [],
                    v,
                  );
                }
              }}
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
                const composing = e.nativeEvent.isComposing || e.keyCode === 229;
                // While the autocomplete menu is open, navigate/confirm it
                // instead of sending. (IME guard first — same as send.)
                if (menu && !composing) {
                  if (e.key === "ArrowDown") {
                    e.preventDefault();
                    setMenu((m) => m && { ...m, active: (m.active + 1) % m.items.length });
                    return;
                  }
                  if (e.key === "ArrowUp") {
                    e.preventDefault();
                    setMenu((m) => m && { ...m, active: (m.active - 1 + m.items.length) % m.items.length });
                    return;
                  }
                  if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
                    e.preventDefault();
                    pickItem(menu.items[menu.active]);
                    return;
                  }
                  if (e.key === "Escape") {
                    e.preventDefault();
                    setMenu(null);
                    return;
                  }
                }
                if (e.key === "Enter" && !e.shiftKey && !composing) {
                  e.preventDefault();
                  send();
                }
              }}
              rows={2}
              placeholder="Ask any Agent …  / 调用技能 · @ 提及产物/智能体/工作流/记忆 · 可拖入图片 / Excel / Word / PPT / PDF"
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
                  accept="image/*,.xlsx,.xls,.xlsm,.csv,.tsv,.docx,.pptx,.pdf,.json,.xml,.txt,.md"
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
                  title="添加附件（图片 / Excel / Word / PPT / PDF，也可直接粘贴 / 拖拽）"
                >
                  <Paperclip className="h-4 w-4" />
                </button>
                <button
                  onClick={() => {
                    // Start a slash command — only valid as the FIRST token, so
                    // this only makes sense on an empty composer. With existing
                    // text we just focus the textarea instead of mangling it.
                    if (!input.trim()) {
                      setInput("/");
                      requestAnimationFrame(() => {
                        const el = textareaRef.current;
                        if (el) {
                          el.focus();
                          el.setSelectionRange(1, 1);
                        }
                        recomputeMenu("/");
                      });
                    } else {
                      textareaRef.current?.focus();
                    }
                  }}
                  className="rounded-md p-1.5 hover:bg-card2 hover:text-muted"
                  title="斜杠命令 / @ 提及（输入 / 或 @ 触发补全）"
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
                disabled={
                  !!permission ||
                  running ||
                  fileAttachments.some((f) => f.uploading) ||
                  (!input.trim() && attachments.length === 0 && fileAttachments.length === 0)
                }
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

/** Turn failed at runtime: the input was delivered, the run errored (model /
 * provider failure, stall watchdog, …). Rendered as a dedicated red card with
 * a retry action instead of a plain "[error]" text bubble. */
function ErrorCard({
  message,
  turnId,
  canRetry,
  busy,
  onRetry,
}: {
  message: string;
  turnId?: string;
  canRetry: boolean;
  busy?: boolean;
  onRetry: () => void;
}) {
  return (
    <div className="rounded-xl border border-red/40 bg-red/10 px-4 py-3">
      <div className="mb-1.5 flex items-center gap-2">
        <AlertCircle className="h-4 w-4 shrink-0 text-red" />
        <span className="text-sm font-medium text-red">请求失败</span>
        <span className="ml-auto">
          <TurnIdChip turnId={turnId} />
        </span>
      </div>
      <pre className="mb-3 max-h-32 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-muted">
        {message}
      </pre>
      {canRetry && (
        <button
          onClick={onRetry}
          disabled={busy}
          title="用原输入重新发起一次回合"
          className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40"
        >
          重试
        </button>
      )}
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
