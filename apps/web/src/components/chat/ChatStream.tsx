"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Paperclip, Keyboard, ArrowUp, X, AlertCircle, Loader2, Square, Zap, ChevronDown, FileEdit, Check, RotateCcw, Globe } from "lucide-react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import { openSessionSocket, getSessionHistory, uploadFile, debugLog, attachFilePath } from "@/lib/runtime";
import { loadToolLabels } from "@/lib/toolLabels";
import { agentHex } from "@/lib/theme";
import { greeting, relTime } from "@/lib/utils";
import { notifyNative } from "@/lib/desktop";
import { notifyPrefs } from "@/lib/notifyPrefs";
import { Icon } from "@/components/icons";
import { ContextBlocks, InnerBlocks, RefBlocks, UserBlocks, hasPendingTool, type Block } from "@/components/chat/blocks";
import { DiffView } from "@/components/workflow/DiffView";
import { LiveRunBlock } from "./RunBlocks";
import { SummarizeModal } from "./SummarizeModal";
import { ConfirmModal } from "@/components/ConfirmModal";
import { HandoffCard } from "@/components/browser/HandoffCard";
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
import {
  cancelWorkflowRun,
  createWorkflow,
  decideWorkflowRun,
  deleteWorkflowRun,
  getWorkflowRun,
  pauseWorkflowRun,
  retryWorkflowRun,
  retryWorkflowRunFromCheckpoint,
  summarizeSessionToDsl,
  triggerWorkflowRun,
} from "@/lib/runtime";
import type { WorkflowRun } from "@/lib/types";
import type { AgentConfig, ContextChange, Goal, SessionMeta, SessionUsage } from "@/lib/types";

interface ChatMsg {
  id: string;
  // "system" = WorldState context chip rows (centered, not a bubble)
  role: "user" | "assistant" | "system";
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

// S6: localStorage key for the unsaved summarize draft (24h TTL, see openSummarize).
const SUMMARIZE_DRAFT_KEY = "ginno-summarize-draft";
const SUMMARIZE_DRAFT_TTL = 24 * 3600 * 1000;

interface SummarizeDraft {
  dsl: Record<string, unknown>;
  sourceSessionId?: string;
  sourceLabel?: string;
  savedAt: number;
}

function readSummarizeDraft(): SummarizeDraft | null {
  try {
    const raw = localStorage.getItem(SUMMARIZE_DRAFT_KEY);
    if (!raw) return null;
    const d = JSON.parse(raw) as SummarizeDraft;
    if (d?.dsl && typeof d.savedAt === "number" && Date.now() - d.savedAt < SUMMARIZE_DRAFT_TTL) {
      return d;
    }
  } catch {
    /* corrupted draft */
  }
  return null;
}

/** One-line summary of a tool call's args for the live-run tool row
 *  (workflow-ux-redesign P1): first string-ish value, truncated to 50 chars. */
function toolArgsPreview(args: unknown): string {
  if (!args || typeof args !== "object") return "";
  for (const v of Object.values(args as Record<string, unknown>)) {
    if (typeof v === "string" && v.trim()) {
      const t = v.trim();
      return t.length > 50 ? t.slice(0, 49) + "…" : t;
    }
  }
  return "";
}

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

/** Rebuild a retry payload from a history user bubble's blocks — used to
 * re-surface a persisted turn-error card (with working retry) after a reload
 * or route/session switch. Image data URLs round-trip through the checkpoint. */
function payloadFromBlocks(blocks: Block[], agentId: string | null): SendPayload {
  const payload: SendPayload = { text: "", images: [], files: [], mentions: [], agentId };
  for (const b of blocks) {
    if (b.kind === "text") {
      payload.text = payload.text ? `${payload.text}\n${b.text}` : b.text;
    } else if (b.kind === "file") {
      payload.files.push({
        id: b.fileId ?? "",
        name: b.name,
        path: b.path ?? "",
        kind: b.fileKind ?? "",
      });
    } else if (b.kind === "image") {
      const m = /^data:([^;]+);base64,(.*)$/.exec(b.url);
      if (m) payload.images.push({ data: m[2], mediaType: m[1], preview: b.url, name: "image" });
    }
  }
  return payload;
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
    case "tool.args": {
      // Attach the tool call's args preview (e.g. the bash command) to the
      // pending bubble so the user sees WHAT is running, not just the label.
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
  compact,
  onRunningChange,
  onUsageChange,
  onOpenGoal,
  onBrowserHandoff,
  onOpenBrowser,
}: {
  session: SessionMeta | null;
  compact?: boolean;
  onRunningChange?: (b: boolean) => void;
  onUsageChange?: (u: SessionUsage) => void;
  onOpenGoal?: () => void;
  onBrowserHandoff?: (h: { space?: string; url?: string; reason?: string } | null) => void;
  onOpenBrowser?: () => void;
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
  const [handoff, setHandoff] = useState<{ space?: string; url?: string; reason?: string } | null>(null);
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
  // Socket callbacks outlive session switches (per-session sockets stay open),
  // so they capture stale context — anything they need live must come from refs.
  const activeSidRef = useRef<string | null>(g.activeSessionId);
  activeSidRef.current = g.activeSessionId;
  // Set when a notification click asked to land on a session's latest message;
  // consumed by the session-switch effect / focus-latest listener below.
  const focusLatestRef = useRef<string | null>(null);
  // socket-ready promises per sid (lazy-creation send awaits the socket).
  const socketReadyRef = useRef<
    Record<string, { promise: Promise<void>; resolve: () => void; reject: (e: unknown) => void }>
  >({});
  // Draft-slot tracking incl. the landing home ("__home__"); see switch effect.
  const prevSlotRef = useRef<string | null>(null);
  function armSocketReady(sid: string) {
    let resolve!: () => void;
    let reject!: (e: unknown) => void;
    const promise = new Promise<void>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    promise.catch(() => {}); // closes without a waiter are normal
    socketReadyRef.current[sid] = { promise, resolve, reject };
  }
  /** Resolves true once the sid's socket is OPEN; false on timeout/close. */
  function waitForSocketOpen(sid: string, timeoutMs = 8000): Promise<boolean> {
    const sock = socketsRef.current[sid];
    if (sock?.readyState === WebSocket.OPEN) return Promise.resolve(true);
    const entry = socketReadyRef.current[sid];
    if (!entry) return Promise.resolve(false);
    return Promise.race([
      entry.promise.then(
        () => true,
        () => false,
      ),
      new Promise<boolean>((res) => setTimeout(() => res(false), timeoutMs)),
    ]);
  }

  // Abandon an in-flight turn after a socket drop that the server cannot
  // answer for (legacy fallback): the user bubble keeps its retry payload.
  function abandonLiveTurn(sid: string) {
    liveBySessionRef.current[sid] = null;
    streamAgentRef.current[sid] = null;
    busyBySessionRef.current[sid] = false;
    storeRef.current[sid] = (storeRef.current[sid] ?? []).map((m) =>
      m.role === "user" && m.status === "sending"
        ? { ...m, status: "failed" as const, failReason: "连接中断，未送达" }
        : m,
    );
    syncDisplay(sid);
  }

  // Map a /history response into chat bubbles (shared by the initial load and
  // the post-reconnect reconciliation).
  function mapHistory(res: {
    messages?: Array<{
      id?: string;
      role: ChatMsg["role"];
      blocks: Block[];
      agentId?: string | null;
      turnId?: string;
    }>;
    last_error?: { message?: string; turn_id?: string } | null;
  }): ChatMsg[] {
    const mapped: ChatMsg[] = (res.messages ?? []).map((m) => ({
      id: m.id ?? mid(),
      role: m.role,
      blocks: m.blocks,
      agentId: m.agentId,
      turnId: m.turnId ?? (m.role === "user" ? m.id : undefined),
      // Rebuild the retry payload for history user bubbles too — without
      // it, a retry that fails again would produce an error card with no
      // payload (no retry button), and the error handler's "last user with
      // payload" lookup would come up empty.
      sendPayload:
        m.role === "user"
          ? payloadFromBlocks(m.blocks, m.agentId ?? session?.agent_id ?? null)
          : undefined,
    }));
    // Re-surface a persisted turn failure as an error card (with retry)
    // so the last error survives reloads and route/session switches.
    const err = res.last_error;
    if (err?.message) {
      const lastUser = [...mapped].reverse().find((m) => m.role === "user");
      mapped.push({
        id: mid(),
        role: "assistant",
        blocks: [{ kind: "text", text: err.message }],
        turnId: err.turn_id ?? lastUser?.turnId,
        error: true,
        sendPayload: lastUser
          ? payloadFromBlocks(lastUser.blocks, lastUser.agentId ?? session?.agent_id ?? null)
          : undefined,
        sourceMsgId: lastUser?.id,
      });
    }
    return mapped;
  }

  // The server says no turn is running for this session (post-reconnect
  // turn_state probe): the stream will not resume. Reload persisted history —
  // a turn that FINISHED while we were disconnected is fully restored from
  // the checkpoint. A user bubble still "sending" that never reached the
  // graph survives as a failed bubble with its retry payload.
  function reconcileTurnFromHistory(sid: string) {
    getSessionHistory(sid).then((res) => {
      const textOf = (m: ChatMsg) =>
        m.blocks.map((b) => (b.kind === "text" ? b.text : "")).join("\n");
      const pending = (storeRef.current[sid] ?? []).filter(
        (m) => m.role === "user" && m.status === "sending",
      );
      const mapped = mapHistory(res ?? {});
      for (const p of pending) {
        const delivered = mapped.some(
          (m) => m.role === "user" && textOf(m) === textOf(p),
        );
        if (!delivered) {
          mapped.push({ ...p, status: "failed" as const, failReason: "连接中断，未送达" });
        }
      }
      storeRef.current[sid] = mapped;
      liveBySessionRef.current[sid] = null;
      streamAgentRef.current[sid] = null;
      busyBySessionRef.current[sid] = false;
      syncDisplay(sid);
    });
  }
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
  const handoffRef       = useRef<Record<string, { space?: string; url?: string; reason?: string } | null>>({});
  const busyBySessionRef = useRef<Record<string, boolean>>({});
  const streamAgentRef   = useRef<Record<string, string | null>>({});
  // Orphan-stream tracking: this instance adopted an ALREADY-RUNNING turn
  // (remount mid-turn — the user navigated to another page while a reply was
  // streaming). Its turn.start is never seen, so the live bubble ensureLive
  // creates would render as a SECOND section next to the history-rendered
  // partial bubble; message.end heals the split by reconciling from history.
  const orphanStreamRef  = useRef<Record<string, boolean>>({});
  const seenTurnStartRef = useRef<Record<string, boolean>>({});
  const pingTimerRef     = useRef<Record<string, ReturnType<typeof setInterval> | null>>({});
  const watchTimerRef    = useRef<Record<string, ReturnType<typeof setInterval> | null>>({});
  // Post-reconnect turn_state fallback: if the server never answers (older
  // runtime), the in-flight turn is abandoned after a grace period.
  const reconcileTimerRef = useRef<Record<string, ReturnType<typeof setTimeout> | null>>({});
  const reconnTimerRef   = useRef<Record<string, ReturnType<typeof setTimeout> | null>>({});
  const lastSeenRef      = useRef<Record<string, number>>({});
  // Unsent input + attachments + resolved mentions saved per session on switch
  const draftCacheRef    = useRef<Record<string, { input: string; attachments: Attachment[]; mentions?: ResolvedMention[] }>>({});
  // Resolved @mentions picked from the autocomplete menu, keyed by session id.
  // Pruned on every input change (edited-away token → dropped mention) and
  // sent along with the invoke payload as the authoritative structured list.
  const mentionsRef      = useRef<Record<string, ResolvedMention[]>>({});
  // In-chat live workflow runs bound to each session (design A: run 回到对话)
  const runsBySessionRef = useRef<Record<string, WorkflowRun[]>>({});
  const [runs, setRuns]   = useState<WorkflowRun[]>([]);
  // Run id pending delete confirmation (ConfirmModal guards the destructive op).
  const [confirmDelRun, setConfirmDelRun] = useState<string | null>(null);
  // 「总结成流程」draft + busy state + inline failure reason (modal stays open)
  const [summarize, setSummarize] = useState<Record<string, unknown> | null>(null);
  const [sumBusy, setSumBusy]     = useState<"create" | "run" | "dev" | null>(null);
  const [sumErr, setSumErr]       = useState<string | null>(null);
  // Create-only success receipt: keeps the modal open with an explicit
  // "已创建 <name>" confirmation (so the user never wonders whether the
  // workflow was added). 创建并运行 closes and the run card animates in instead.
  const [sumCreated, setSumCreated] = useState<string | null>(null);
  // S1: summarize API call in flight + which session the draft came from (the
  // modal's retry button and header label need both).
  const [sumLoading, setSumLoading] = useState(false);
  const [sumSource, setSumSource] = useState<{ id: string; label: string } | null>(null);
  // quality-plan §3.1: synthesis case id for outcome backfill (adoption/first-run).
  const [sumSynthesisId, setSumSynthesisId] = useState<string | null>(null);
  const [sumMenuOpen, setSumMenuOpen] = useState(false);
  // S5: trace range — null = full session; 5/10/20 = last N messages.
  const [sumLastN, setSumLastN] = useState<number | null>(null);
  // S6: bump to re-read the localStorage draft (after restore/delete/save). The
  // draft is exposed as an OPT-IN row in the summarize dropdown — it must never
  // block a fresh summarize (a leftover draft from another session used to wedge
  // the button and prevent creating any workflow).
  const [draftTick, setDraftTick] = useState(0);
  // Re-read the localStorage draft when it may have changed (open/save/delete).
  const savedDraft = useMemo(
    () => (sumMenuOpen ? readSummarizeDraft() : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sumMenuOpen, draftTick],
  );
  // Receipt shown briefly after a version_propose decision (card already unmounted).
  const [proposeResult, setProposeResult] = useState<
    { decision: "allow" | "deny"; workflowId: string; fromVersion: number } | null
  >(null);

  // Push the given session's ref state into React display state.
  // No-op when sid is not the currently displayed session (background socket).
  const syncDisplay = (sid: string) => {
    if (sid !== curSessionIdRef.current) return;
    const lid = liveBySessionRef.current[sid] ?? null;
    setMessages([...(storeRef.current[sid] ?? [])]);
    setRuns([...(runsBySessionRef.current[sid] ?? [])]);
    setLiveId(lid);
    liveIdRef.current = lid;
    setWsStatus(statusRef.current[sid] ?? "connecting");
    setPermission(permsRef.current[sid] ?? null);
    setPropose(proposeRef.current[sid] ?? null);
    setHandoff(handoffRef.current[sid] ?? null);
    setStreamAgent(streamAgentRef.current[sid] ?? null);
  };

  // Pre-load tool display labels from settings (cached at module level).
  useEffect(() => { loadToolLabels(); }, []);

  // Drop per-session state for deleted sessions to prevent memory leaks.
  useEffect(() => {
    const live = new Set(g.sessions.map((s) => s.id));
    for (const id of Object.keys(storeRef.current)) {
      if (!live.has(id)) {
        if (reconnTimerRef.current[id]) clearTimeout(reconnTimerRef.current[id]!);
        if (pingTimerRef.current[id])   clearInterval(pingTimerRef.current[id]!);
        if (watchTimerRef.current[id])  clearInterval(watchTimerRef.current[id]!);
        if (reconcileTimerRef.current[id]) clearTimeout(reconcileTimerRef.current[id]!);
        try { socketsRef.current[id]?.close(); } catch { /* ignore */ }
        delete socketsRef.current[id];    delete storeRef.current[id];
        delete liveBySessionRef.current[id]; delete statusRef.current[id];
        delete permsRef.current[id];      delete proposeRef.current[id];
        delete handoffRef.current[id];
        delete busyBySessionRef.current[id]; delete streamAgentRef.current[id];
        delete draftCacheRef.current[id]; delete pingTimerRef.current[id];
        delete watchTimerRef.current[id]; delete reconnTimerRef.current[id];
        delete lastSeenRef.current[id];   delete reconcileTimerRef.current[id];
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
        if (reconcileTimerRef.current[sid]) clearTimeout(reconcileTimerRef.current[sid]!);
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
    // socket-ready promise: lazy creation (home → first send) must be able to
    // await the socket instead of racing it into a "连接未就绪" failed bubble.
    armSocketReady(sid);
    sock.onopen = () => {
      if (socketsRef.current[sid] !== sock) return;
      socketReadyRef.current[sid]?.resolve();
      g.setConnected(true);
      statusRef.current[sid] = "live";
      lastSeenRef.current[sid] = Date.now();
      if (liveBySessionRef.current[sid] || busyBySessionRef.current[sid]) {
        // A turn was in flight when this socket's predecessor dropped. Turn
        // events broadcast to EVERY socket of the session, so the running
        // stream resumes into the same live bubble automatically. Ask the
        // server whether the turn still exists; if not (it finished while we
        // were gone, or the runtime restarted), reconcile from history.
        try { sock.send(JSON.stringify({ type: "turn_state" })); } catch { /* ignore */ }
        if (reconcileTimerRef.current[sid]) clearTimeout(reconcileTimerRef.current[sid]!);
        reconcileTimerRef.current[sid] = setTimeout(() => {
          reconcileTimerRef.current[sid] = null;
          // No turn.state answer (older runtime): legacy abandon path.
          if (liveBySessionRef.current[sid]) abandonLiveTurn(sid);
        }, 6000);
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
      socketReadyRef.current[sid]?.reject(new Error("socket error"));
      try { sock.close(); } catch { /* ignore */ }
    };
    sock.onclose = () => {
      if (pingTimerRef.current[sid]) { clearInterval(pingTimerRef.current[sid]!); pingTimerRef.current[sid] = null; }
      if (watchTimerRef.current[sid]) { clearInterval(watchTimerRef.current[sid]!); watchTimerRef.current[sid] = null; }
      if (socketsRef.current[sid] !== sock) return;
      socketReadyRef.current[sid]?.reject(new Error("socket closed"));
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
    const sid = session?.id ?? null;
    // Draft slots include the landing home so ⌘N → type → open session → ⌘N
    // round-trips keep the text.
    const HOME_SLOT = "__home__";
    const prevSlot = prevSlotRef.current;
    const nextSlot = sid ?? HOME_SLOT;

    // Save outgoing draft (before any early return)
    if (prevSlot && prevSlot !== nextSlot) {
      draftCacheRef.current[prevSlot] = {
        input,
        attachments,
        mentions: pruneMentions(
          mentionsRef.current[prevSlot === HOME_SLOT ? "" : prevSlot] ?? [],
          input,
        ),
      };
      setInput("");
      setAttachments([]);
      setTarget(null);
      setMenu(null); // menu is composer-global state; never leak across sessions
    }
    prevSlotRef.current = nextSlot;

    if (!session || !sid) {
      curSessionIdRef.current = null;
      const draft = draftCacheRef.current[HOME_SLOT];
      if (draft) {
        setInput(draft.input);
        setAttachments(draft.attachments);
      }
      return;
    }

    curSessionIdRef.current = sid;
    connectRef.current = () => connectSession(sid);

    connectSession(sid);

    // Load history if this session has no messages yet
    if (!storeRef.current[sid]) {
      storeRef.current[sid] = [];
      getSessionHistory(sid).then((res) => {
        if (!res?.messages?.length) return;
        storeRef.current[sid] = mapHistory(res);
        syncDisplay(sid);
      });
    }

    // Load the session's goal snapshot for the TopBar chip (goal-design.md).
    // Live updates then arrive via goal.updated / goal.cleared WS events.
    g.loadGoal(sid);

    // Restore draft if any (mentions re-pruned against the restored text so a
    // token the user deleted before switching stays deleted)
    const draft = draftCacheRef.current[sid];
    if (draft) {
      setInput(draft.input);
      setAttachments(draft.attachments);
      mentionsRef.current[sid] = pruneMentions(draft.mentions ?? [], draft.input);
    }

    syncDisplay(sid);

    // Notification-click jump: land on the latest message regardless of the
    // parked scroll position. For uncached sessions the async history load
    // re-syncs display later; stickRef=true lets the [messages] auto-scroll
    // effect finish the job then.
    if (focusLatestRef.current === sid) {
      focusLatestRef.current = null;
      stickRef.current = true;
      scrollToBottomSoon();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.id]);

  const running =
    liveId !== null || !!permission || !!propose || !!handoff || messages.some((m) => hasPendingTool(m.blocks));
  useEffect(() => {
    onRunningChange?.(running);
  }, [running, onRunningChange]);

  // Session goal (goal-design.md) — drives the stop=pause button and the
  // paused/blocked resume banner.
  const goal = session ? g.goalBySession[session.id] ?? null : null;
  const goalActive = goal?.status === "active";
  const goalStalled =
    goal?.status === "paused" || goal?.status === "blocked" || goal?.status === "usage_limited";
  // Dismiss the resume banner per mount; reappears when the session is reopened.
  const [resumeDismissed, setResumeDismissed] = useState(false);
  useEffect(() => {
    setResumeDismissed(false);
  }, [session?.id, goal?.status]);

  // Auto-scroll only while the user is parked near the bottom; otherwise a
  // streaming token (or a history load) yanks them back down mid-read.
  useEffect(() => {
    if (stickRef.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  // Scroll-to-bottom that tolerates the container still having no layout
  // (e.g. the route flipping back to "/" right after a notification click) —
  // retries a few frames until scrollHeight is real.
  function scrollToBottomSoon() {
    let tries = 0;
    const attempt = () => {
      const el = scrollRef.current;
      if (el && el.scrollHeight > 0) {
        el.scrollTop = el.scrollHeight;
        return;
      }
      if (++tries < 20) requestAnimationFrame(attempt);
    };
    requestAnimationFrame(attempt);
  }

  // Notification-click jump target (dispatched by AppShell's __ginnoOpenSession
  // and by the HTML5-notification browser fallback in the message.end handler).
  // Arm stick-to-bottom; if the session is already displayed scroll now,
  // otherwise the session-switch effect handles it once it lands.
  useEffect(() => {
    const onFocusLatest = (e: Event) => {
      const sid = (e as CustomEvent<string>).detail;
      if (!sid) return;
      focusLatestRef.current = sid;
      stickRef.current = true;
      if (curSessionIdRef.current === sid) scrollToBottomSoon();
    };
    window.addEventListener("ginno:focus-latest", onFocusLatest);
    return () => window.removeEventListener("ginno:focus-latest", onFocusLatest);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // BrowserPane 交还：同一条 permission_response / browser_resume 通道。
  useEffect(() => {
    const onResume = (e: Event) => {
      const space = (e as CustomEvent<{ space?: string }>).detail?.space;
      respondBrowserResume(space);
    };
    window.addEventListener("ginno:browser-resume", onResume);
    return () => window.removeEventListener("ginno:browser-resume", onResume);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Delivery confirmed (the server started or finished the turn) → clear the
  // "sending" marker off user bubbles.
  function markDelivered(sid: string) {
    const list = storeRef.current[sid];
    if (!list?.some((m) => m.status === "sending")) return;
    storeRef.current[sid] = list.map((m) =>
      m.status === "sending" ? { ...m, status: undefined, failReason: undefined } : m,
    );
  }

  // The server is streaming a turn this component instance never saw start
  // (remount mid-turn after navigating away). Mark the stream orphaned so the
  // split self-heals on message.end, and backfill the session's agent so the
  // continuation bubble's header shows the right name instead of "Agent".
  function adoptOrphanStream(sid: string) {
    if (seenTurnStartRef.current[sid] || orphanStreamRef.current[sid]) return;
    orphanStreamRef.current[sid] = true;
    busyBySessionRef.current[sid] = true;
    if (!streamAgentRef.current[sid]) streamAgentRef.current[sid] = session?.agent_id ?? null;
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
      case "tool.args":
      case "tool.end":
      case "widget.emit":
      case "ref.emit":
      case "workflow.emit":
        adoptOrphanStream(sid);
        mutateLive(sid, ev);
        break;
      case "turn.start": {
        markDelivered(sid);
        seenTurnStartRef.current[sid] = true;
        // authoritative agent for this turn (server-resolved, never null).
        // The server echoes the turn_id we sent (or mints one); adopt it as the
        // bubble's trace UUID so it matches the sidecar logs exactly.
        const srvTurn = ev.turn_id as string | undefined;
        const evAgent = (ev.agent_id as string) || null;
        // Headless (goal continuation) turns have NO client bubble yet — the
        // first token.delta would otherwise create one with a null agent and
        // render the generic "Agent". Prime the bubble here so the
        // server-provided agent name is kept (bug: continuation showed "Agent").
        if (evAgent) streamAgentRef.current[sid] = evAgent;
        const id = liveBySessionRef.current[sid] ?? ensureLive(sid);
        storeRef.current[sid] = (storeRef.current[sid] ?? []).map((msg) =>
          msg.id === id
            ? {
                ...msg,
                agentId: evAgent,
                agentName: (ev.name as string) || undefined,
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
      case "browser.handoff": {
        const h = {
          space: ev.space as string | undefined,
          url: (ev.url as string) || "",
          reason: (ev.reason as string) || "",
        };
        handoffRef.current[sid] = h;
        onBrowserHandoff?.(h);
        break;
      }
      case "browser.space":
        if (ev.owner === "agentDelegatedToUser") {
          const h = {
            space: ev.name as string | undefined,
            url: (ev.url as string) || "",
            reason: (ev.reason as string) || "",
          };
          handoffRef.current[sid] = h;
          onBrowserHandoff?.(h);
        } else if (ev.owner === "agent") {
          // Agent started browsing — surface the embedded browser.
          onOpenBrowser?.();
        }
        break;
      case "todos.changed":
        g.reloadTodos();
        break;
      case "skills.changed":
        // A turn (install_skills tool, bash) or the Settings page mutated
        // ~/.ginno/skills — refresh the slash menu's skill list live.
        g.reloadSkills();
        break;
      case "agents.changed":
        // Agent CRUD in Settings — keep the picker/mention list in sync.
        g.reloadAgents();
        break;
      case "workflows.changed":
        g.reloadWorkflows();
        g.reloadWorkflowRuns();
        break;
      case "run.bind": {
        const runId = ev.run_id as string;
        getWorkflowRun(runId).then((r) => {
          if (!r?.run) return;
          const list = runsBySessionRef.current[sid] ?? [];
          if (!list.some((x) => x.id === runId)) list.push(r.run);
          runsBySessionRef.current[sid] = [...list];
          syncDisplay(sid);
        });
        break;
      }
      case "run.event": {
        const runId = ev.run_id as string;
        const inner = (ev.payload ?? {}) as Record<string, unknown>;
        const list = runsBySessionRef.current[sid] ?? [];
        const run = list.find((x) => x.id === runId);
        // Live tool-call visibility (workflow-ux-redesign P1): show the
        // in-flight tool under the running step; results/exit clear it.
        const innerKind = inner.kind as string | undefined;
        if (innerKind === "tool_call") {
          const calls = (inner.calls as Array<{ name?: string; args?: unknown }>) ?? [];
          const latest = calls[calls.length - 1]; // batched calls: show the newest
          if (latest?.name) {
            g.notifyRunToolActivity(runId, {
              nodeId: (inner.node_id as string) ?? "",
              toolName: latest.name,
              argsPreview: toolArgsPreview(latest.args),
            });
          }
        } else if (
          innerKind === "tool_result" || innerKind === "node_exit" ||
          innerKind === "error" || innerKind === "done"
        ) {
          g.notifyRunToolActivity(runId, null);
        }
        if (run) {
          const nid = inner.node_id as string | undefined;
          const kind2 = inner.kind as string | undefined;
          // Mirror the server's per-event _touch_run: without this the in-chat
          // card's adaptive stuck check fires during long steps that DO emit
          // tool traffic (the 1.5s panel poll doesn't cover the chat list).
          run.updated = Date.now() / 1000;
          if (nid && (kind2 === "node_enter" || kind2 === "node_exit")) {
            // node_exit carries the step's real outcome — a failed step must not
            // render as done (green) in the live card.
            const stepStatus =
              kind2 === "node_enter" ? "running" : inner.status === "failed" ? "failed" : "done";
            run.steps = run.steps.map((s) => (s.id === nid ? { ...s, status: stepStatus } : s));
            runsBySessionRef.current[sid] = [...list];
          } else if (kind2 === "interrupt") {
            // A node suspended the graph (P1): stamp the payload so the card
            // renders immediately, without waiting for the reload round-trip.
            // nature: "human" (question card) vs "manual" (user pause, #14);
            // HumanNode events carry no nature and default to human. The step
            // flips to running (done on resume — except manual pauses, whose
            // step re-executes and settles via node_enter/exit).
            run.pending_interrupt = {
              ...(inner as object),
              kind: (inner.nature as string) || "human",
            } as typeof run.pending_interrupt;
            if (nid) run.steps = run.steps.map((s) => (s.id === nid ? { ...s, status: "running" } : s));
            runsBySessionRef.current[sid] = [...list];
          } else if (kind2 === "resume") {
            run.pending_interrupt = null;
            if (nid && inner.nature !== "manual") {
              run.steps = run.steps.map((s) => (s.id === nid ? { ...s, status: "done" } : s));
            }
            runsBySessionRef.current[sid] = [...list];
          } else if (kind2 === "error") {
            // Show the failure one beat before run.status lands, and stamp the
            // structured diagnostic so RunErrorBox renders without a lazy fetch.
            if (typeof inner.error === "string") run.error = inner.error;
            run.error_detail = {
              node_id: (inner.node_id as string | null) ?? null,
              traceback: (inner.traceback as string | undefined) ?? null,
            };
            runsBySessionRef.current[sid] = [...list];
          }
          syncDisplay(sid);
        }
        break;
      }
      case "run.status": {
        const runId = ev.run_id as string;
        const status = ev.status as string;
        const list = runsBySessionRef.current[sid] ?? [];
        const run = list.find((x) => x.id === runId);
        if (run) {
          run.status = status;
          if (typeof ev.error === "string") run.error = ev.error;
          // P1: the paused push carries WHY (human question); terminal states
          // and fresh resumes clear it.
          if (status === "paused") {
            run.pending_interrupt =
              (ev.pending_interrupt as typeof run.pending_interrupt) ?? run.pending_interrupt ?? null;
          } else {
            run.pending_interrupt = null;
          }
          runsBySessionRef.current[sid] = [...list];
        }
        syncDisplay(sid);
        g.reloadWorkflowRuns();
        break;
      }
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
      case "goal.updated":
        // Live goal snapshot (created/status/accounting) → TopBar chip.
        g.notifyGoal(sid, (ev.goal as Goal) ?? null);
        break;
      case "goal.cleared":
        g.notifyGoal(sid, null);
        break;
      case "session_title":
        // Auto-title from the first user message (runtime _touch_session_title):
        // sidebar + TopBar rename live without a sessions reload.
        g.applySessionPatch(sid, { title: (ev.title as string) ?? "", title_auto: false });
        break;
      case "session.context":
        // Mount set changed server-side (context-folders-design.md): /mount
        // command or another client ran PUT /sessions/{id}/context. Patch the
        // store so the TopBar chip re-renders without a full reload.
        g.applySessionPatch(sid, {
          context_folders: ((ev.context_folders as string[]) ?? []),
          primary_folder: (ev.primary_folder as string | null) ?? null,
        });
        break;
      case "context.updated": {
        // WorldState change announcement (world-state-plan §7). Chip display
        // level table: environment-only changes (date rollover) stay SILENT in
        // the UI; everything else gets a centered context row.
        const changes = ((ev.changes as ContextChange[]) || []).filter(Boolean);
        const visible = changes.filter((c) => c.section !== "environment");
        if (!visible.length) break; // environment-only (date rollover) = silent
        const rows = visible.map((c): Block => ({ kind: "context", text: c.summary }));
        storeRef.current[sid] = [
          ...(storeRef.current[sid] ?? []),
          { id: mid(), role: "system" as const, blocks: rows },
        ];
        syncDisplay(sid);
        break;
      }
      case "context.microcompacted": {
        // Stale tool outputs cleared to placeholders (E2.5) — always visible.
        const n = Number(ev.cleared_tool_outputs ?? 0);
        storeRef.current[sid] = [
          ...(storeRef.current[sid] ?? []),
          {
            id: mid(),
            role: "system" as const,
            blocks: [
              {
                kind: "context",
                text: `已清理 ${n} 条较早的工具输出以节省上下文，需要时可重新调用工具获取。`,
              },
            ],
          },
        ];
        syncDisplay(sid);
        break;
      }
      case "context.compacted": {
        // History compaction announcement (E3) — always visible.
        const n = Number(ev.compacted_messages ?? 0);
        storeRef.current[sid] = [
          ...(storeRef.current[sid] ?? []),
          {
            id: mid(),
            role: "system" as const,
            blocks: [
              {
                kind: "context",
                text: `对话已压缩：${n} 条较早的消息被摘要替代，最近的对话原样保留。`,
              },
            ],
          },
        ];
        syncDisplay(sid);
        break;
      }
      case "usage": {
        // Session-cumulative model usage (D2) → TopBar counter via callback.
        const s = ev.session as SessionUsage | undefined;
        if (s && typeof s.input_tokens === "number") onUsageChange?.(s);
        break;
      }
      case "turn.state": {
        // Answer to the post-reconnect probe: is a turn still running (or
        // parked at an interrupt) for this session?
        if (reconcileTimerRef.current[sid]) {
          clearTimeout(reconcileTimerRef.current[sid]!);
          reconcileTimerRef.current[sid] = null;
        }
        if (ev.running) break; // the broadcast stream resumes on this socket
        reconcileTurnFromHistory(sid);
        break;
      }
      case "message.end": {
        markDelivered(sid);
        const wasOrphan = !!orphanStreamRef.current[sid];
        orphanStreamRef.current[sid] = false;
        liveBySessionRef.current[sid] = null;
        streamAgentRef.current[sid] = null;
        busyBySessionRef.current[sid] = false;
        // Orphaned continuation (remount mid-turn): the visible store holds a
        // history-rendered partial bubble PLUS a second live section. The
        // persisted history renders the whole turn as ONE merged bubble (with
        // the right agent name) — rebuild from it to heal the split.
        if (wasOrphan) reconcileTurnFromHistory(sid);
        // Turn done → desktop notification unless the user is watching this
        // exact session right now (visible ∧ workspace route ∧ active session).
        // Socket callbacks capture stale closures (sockets outlive session
        // switches) — read refs / live values only. The session title may be
        // stale too (rename after connect); cosmetic, accepted.
        {
          // Settings → Notifications (settings.json; sync cache — see
          // lib/notifyPrefs.ts for why this isn't React state).
          const np = notifyPrefs();
          const watching =
            document.visibilityState === "visible" &&
            window.location.pathname === "/" &&
            activeSidRef.current === sid;
          if (np.enabled && !watching) {
            const title = g.sessions.find((s) => s.id === sid)?.title?.trim() || "Ginno";
            const raw = typeof ev.text === "string" ? ev.text.trim() : "";
            const body = raw || "回复已完成";
            void notifyNative({
              kind: "session",
              id: sid,
              title,
              body,
              sound: np.sound ? np.soundName : undefined,
            }).then((sent) => {
              if (sent) return;
              // Plain-browser dev fallback — WKWebView has no Notification API,
              // so inside the packaged app this branch is a silent no-op.
              if (typeof Notification === "undefined") return;
              if (Notification.permission === "default") {
                try {
                  void Notification.requestPermission();
                } catch {
                  /* unsupported */
                }
                return;
              }
              if (Notification.permission !== "granted") return;
              try {
                const n = new Notification(title, { body });
                n.onclick = () => {
                  window.focus();
                  g.setActiveSession(sid); // stable setter — stale closure safe
                  window.dispatchEvent(
                    new CustomEvent("ginno:focus-latest", { detail: sid }),
                  );
                  n.close();
                };
              } catch {
                /* blocked/unsupported */
              }
            });
          }
        }
        break;
      }
      case "error": {
        // The turn reached the server (it is the run, not the delivery, that
        // failed) → the user bubble counts as delivered; the failure becomes
        // a dedicated error card with a retry action.
        markDelivered(sid);
        const liveMsgId = liveBySessionRef.current[sid];
        liveBySessionRef.current[sid] = null;
        streamAgentRef.current[sid] = null;
        busyBySessionRef.current[sid] = false;
        // Orphaned turn that failed: reconcile from history instead of
        // building a card on the split store — mapHistory re-surfaces the
        // persisted last_error as a proper error card with retry.
        if (orphanStreamRef.current[sid]) {
          orphanStreamRef.current[sid] = false;
          reconcileTurnFromHistory(sid);
          break;
        }
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

  // Docs/native paths dropped while on the landing home (no session yet):
  // buffered with optimistic chips, uploaded right after lazy creation.
  const pendingDocsRef = useRef<Array<{ file: File; tmpId: string }>>([]);
  const pendingPathsRef = useRef<Array<{ path: string; tmpId: string }>>([]);

  async function uploadOneDoc(sid: string, f: File, tmpId: string) {
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

  async function addFiles(files: FileList | File[] | null) {
    // [DEBUG] telemetry for WKWebView drag & drop diagnosis
    void debugLog({
      where: "addFiles:enter",
      hasSession: !!session,
      count: files?.length ?? 0,
      files: Array.from(files ?? []).map((f) => ({ name: f.name, type: f.type, size: f.size })),
    });
    if (!files?.length) return;
    const sid = session?.id ?? null;
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
      if (!sid) {
        pendingDocsRef.current.push({ file: f, tmpId });
        continue;
      }
      await uploadOneDoc(sid, f, tmpId);
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

  async function attachOne(sid: string, p: string, tmpId: string) {
    const name = p.split("/").pop() || p;
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

  async function attachPaths(paths: string[]) {
    void debugLog({ where: "attachPaths", paths });
    if (!paths?.length) return;
    const sid = session?.id ?? null;
    for (const p of paths) {
      const name = p.split("/").pop() || p;
      const tmpId = `path-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      setFileAttachments((a) => [...a, { id: tmpId, name, path: p, kind: "", uploading: true }]);
      if (!sid) {
        pendingPathsRef.current.push({ path: p, tmpId });
        continue;
      }
      await attachOne(sid, p, tmpId);
    }
  }

  // ─── 闭环 (design A): 总结成流程 + 对话内运行块控制 ─────────────────────────
  // The actual LLM summarization path (also used by the modal's ↺ retry).
  async function freshSummarize(sessionId?: string) {
    setSumMenuOpen(false);
    const targetId = sessionId || session?.id;
    if (!targetId) return;
    const label =
      targetId === session?.id
        ? session?.title || "当前会话"
        : g.sessions.find((s) => s.id === targetId)?.title || "历史会话";
    setSumLoading(true);
    setSumErr(null);
    setSumCreated(null);
    try {
      const r = await summarizeSessionToDsl(targetId, undefined, sumLastN ?? undefined);
      if (r.ok) {
        setSumSource({ id: targetId, label });
        setSumSynthesisId(r.synthesis_id ?? null);
        setSummarize(r.dsl);
      } else {
        setSumErr(`总结失败：${r.error ?? "unknown"}`);
        setSummarize({}); // keep the modal open so the reason is visible
      }
    } catch {
      setSumErr("总结失败：无法连接运行时");
      setSummarize({});
    } finally {
      setSumLoading(false);
    }
  }

  // Summarize is the primary action and must ALWAYS run fresh — a leftover
  // draft (possibly from another session) must never intercept it. Draft
  // recovery is an explicit opt-in row in the dropdown (openDraftModal).
  async function openSummarize(sessionId?: string) {
    await freshSummarize(sessionId);
  }

  function openDraftModal() {
    const draft = readSummarizeDraft();
    if (!draft) return;
    setSumMenuOpen(false);
    setSumSource({
      id: draft.sourceSessionId || "",
      label: draft.sourceLabel || "上次草稿",
    });
    setSummarize(draft.dsl);
    setSumErr(null);
    setSumCreated(null);
  }

  function deleteDraft() {
    try {
      localStorage.removeItem(SUMMARIZE_DRAFT_KEY);
    } catch {
      /* ignore */
    }
    setDraftTick((n) => n + 1); // refresh the dropdown's draft row
  }

  function closeSummarize(saveDraft: boolean) {
    // S6: closing without creating keeps the draft recoverable for 24h
    // (sourceSessionId travels with it so ↺重新总结 works after restore). Only
    // non-trivial drafts are saved — an empty/failed `{}` must not become a
    // "restorable" draft that later confuses the user.
    const hasNodes = Array.isArray(summarize?.nodes) && (summarize!.nodes as unknown[]).length > 0;
    try {
      if (saveDraft && summarize && hasNodes) {
        localStorage.setItem(
          SUMMARIZE_DRAFT_KEY,
          JSON.stringify({
            dsl: summarize,
            sourceSessionId: sumSource?.id || undefined,
            sourceLabel: sumSource?.label,
            savedAt: Date.now(),
          }),
        );
      } else {
        localStorage.removeItem(SUMMARIZE_DRAFT_KEY);
      }
    } catch {
      /* storage unavailable */
    }
    setDraftTick((n) => n + 1);
    setSummarize(null);
    setSumErr(null);
    setSumCreated(null);
  }

  async function createFromSummarize(run: boolean, editedDsl: Record<string, unknown>) {
    if (!session) return;
    setSumBusy(run ? "run" : "create");
    setSumErr(null);
    try {
      const cw = await createWorkflow({
        name: (editedDsl.name as string) || "新流程",
        description: (editedDsl.description as string) || "",
        dsl: editedDsl,
        ...(sumSynthesisId ? { synthesis_id: sumSynthesisId } : {}),
      });
      const cwBody = cw as { ok?: boolean; workflow?: import("@/lib/types").WorkflowDef; detail?: string };
      if (!cwBody.workflow) {
        // json() doesn't throw on HTTP errors — surface the reason inline and
        // KEEP the modal open so the draft isn't lost.
        setSumErr(cwBody.detail || "创建工作流失败");
        return;
      }
      await g.reloadWorkflows(); // list reflects the new workflow immediately
      setDraftTick((n) => n + 1);
      try {
        localStorage.removeItem(SUMMARIZE_DRAFT_KEY); // created → draft consumed
      } catch { /* ignore */ }
      if (run) {
        const tr = await triggerWorkflowRun(cwBody.workflow.id, undefined, session.id);
        const trBody = tr as { ok?: boolean; run?: import("@/lib/types").WorkflowRun; detail?: string };
        if (trBody.run) {
          const list = runsBySessionRef.current[session.id] ?? [];
          if (!list.some((x) => x.id === trBody.run!.id)) list.push(trBody.run);
          runsBySessionRef.current[session.id] = [...list];
          syncDisplay(session.id);
        } else {
          // Created but not started: still a partial success — report inline.
          setSumErr(`已创建，但运行触发失败：${trBody.detail || "未知错误"}`);
          return;
        }
        setSummarize(null); // run card animates in — close the modal
      } else {
        // Create-only: keep the modal open with an explicit receipt so it is
        // unambiguous that the workflow was added (then 完成 closes it).
        setSumErr(null);
        setSumCreated(cwBody.workflow.name || "新流程");
      }
    } catch {
      setSumErr("无法连接运行时");
    } finally {
      setSumBusy(null);
    }
  }

  // S2: 进入开发会话精炼 — create the draft as v1 first, then open a
  // workflow-dev session where the agent can propose further edits.
  async function openDevFromSummarize(editedDsl: Record<string, unknown>) {
    setSumBusy("dev");
    setSumErr(null);
    try {
      const cw = await createWorkflow({
        name: (editedDsl.name as string) || "新流程",
        description: (editedDsl.description as string) || "",
        dsl: editedDsl,
        ...(sumSynthesisId ? { synthesis_id: sumSynthesisId } : {}),
      });
      const cwBody = cw as { ok?: boolean; workflow?: import("@/lib/types").WorkflowDef; detail?: string };
      if (!cwBody.workflow) {
        setSumErr(cwBody.detail || "创建工作流失败");
        return;
      }
      await g.reloadWorkflows();
      setDraftTick((n) => n + 1);
      try {
        localStorage.removeItem(SUMMARIZE_DRAFT_KEY);
      } catch { /* ignore */ }
      setSummarize(null);
      setSumCreated(null);
      await g.newSession("workflow-dev", {
        title: `精炼流程：${cwBody.workflow.name}`,
      });
    } catch {
      setSumErr("无法连接运行时");
    } finally {
      setSumBusy(null);
    }
  }

  function cancelRun(runId: string) {
    cancelWorkflowRun(runId);
  }
  function pauseRun(runId: string) {
    // Manual pause (#14): cooperative — the run flips to paused via the
    // run.status push once it reaches a safe boundary.
    void pauseWorkflowRun(runId);
  }
  function continueRun(runId: string) {
    decideWorkflowRun(runId, "continue");
  }
  function retryRun(runId: string): Promise<{ ok?: boolean; detail?: string } | void> {
    // The retry creates a NEW run bound to the same session; the run.bind push
    // (or the workflows.changed reload) surfaces it. Refresh the local list too.
    // Returns the outcome so LiveRunBlock can shake + show the reason on failure.
    return retryWorkflowRun(runId)
      .then((r) => {
        g.reloadWorkflowRuns();
        const body = r as { ok?: boolean; detail?: string } | undefined;
        if (body && body.ok === false) return { ok: false, detail: body.detail };
        return undefined;
      })
      .catch(() => ({ ok: false, detail: "无法连接运行时" }));
  }
  function retryRunFromCheckpoint(runId: string): Promise<{ ok?: boolean; detail?: string } | void> {
    // P2: re-execute from the persisted checkpoint (failed node + suffix only).
    return retryWorkflowRunFromCheckpoint(runId)
      .then((r) => {
        g.reloadWorkflowRuns();
        const body = r as { ok?: boolean; detail?: string } | undefined;
        if (body && body.ok === false) return { ok: false, detail: body.detail };
        return undefined;
      })
      .catch(() => ({ ok: false, detail: "无法连接运行时" }));
  }
  function deleteRun(runId: string) {
    void deleteWorkflowRun(runId).then(() => {
      const sid = curSessionIdRef.current;
      if (sid) {
        const list = (runsBySessionRef.current[sid] ?? []).filter((r) => r.id !== runId);
        runsBySessionRef.current[sid] = list;
        syncDisplay(sid);
      }
      g.reloadWorkflowRuns();
    });
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

  // Home-state model pick (composer model chip, M3) consumed on lazy creation.
  const [homeModel, setHomeModel] = useState<{ provider?: string; model?: string } | undefined>(
    undefined,
  );
  const homeCreatingRef = useRef(false);

  // ── composer inline controls (open-experience redesign M3) ──────────────
  // Model chip = per-session provider/model switch (server drops the graph,
  // next WS connect rebuilds).
  const [modelOpen, setModelOpen] = useState(false);
  function cycleSessionSocket(sid: string) {
    const old = socketsRef.current[sid];
    if (old) {
      try {
        old.close();
      } catch {
        /* ignore */
      }
    }
    delete socketsRef.current[sid];
    connectSession(sid);
  }
  async function pickModel(pid: string, model: string) {
    setModelOpen(false);
    if (!session) {
      setHomeModel({ provider: pid, model });
      return;
    }
    const prevProvider = session.provider;
    const prevModel = session.model;
    g.applySessionPatch(session.id, { provider: pid, model });
    try {
      const r = await api.patchSession(session.id, { provider: pid, model });
      if (!r?.ok || !r.session) throw new Error(r?.error ?? "switch failed");
      g.applySessionPatch(session.id, r.session);
      cycleSessionSocket(session.id);
    } catch {
      g.applySessionPatch(session.id, { provider: prevProvider, model: prevModel });
    }
  }
  const enabledProviders = Object.entries(g.providers).filter(([, p]) => p.enabled);
  const modelChipLabel = session
    ? session.model || session.provider
    : homeModel?.model || homeModel?.provider || g.defaultProvider;

  /** Lazy creation: home composer send creates the session, flushes buffered
   *  attachments, awaits the socket, then posts the turn. */
  async function createAndSend(payload: {
    text: string;
    images: Attachment[];
    files: FileAttachment[];
  }) {
    if (homeCreatingRef.current) return;
    homeCreatingRef.current = true;
    try {
      const agentId = target ?? g.agents[0]?.id ?? null;
      const s = await g.newSession(agentId, homeModel ? { ...homeModel } : undefined);
      if (!s) return; // sessionError banner carries the reason; composer keeps text
      curSessionIdRef.current = s.id;
      connectSession(s.id);
      const docs = pendingDocsRef.current;
      pendingDocsRef.current = [];
      for (const d of docs) await uploadOneDoc(s.id, d.file, d.tmpId);
      const natives = pendingPathsRef.current;
      pendingPathsRef.current = [];
      for (const n of natives) await attachOne(s.id, n.path, n.tmpId);
      // The socket race fix: await open instead of failing fast. Timeout
      // degrades to attemptSend's retryable failed bubble.
      await waitForSocketOpen(s.id);
      attemptSend(s.id, { ...payload, mentions: [], agentId });
      setInput("");
      setAttachments([]);
      setFileAttachments([]);
      setTarget(null);
      setMenu(null);
    } finally {
      homeCreatingRef.current = false;
    }
  }

  function send() {
    const text = input.trim();
    const readyFiles = fileAttachments.filter((f) => !f.uploading);
    if (!text && attachments.length === 0 && readyFiles.length === 0) return;
    if (readyFiles.length !== fileAttachments.length) return; // upload in flight
    if (!session) {
      void createAndSend({ text, images: attachments, files: readyFiles });
      return;
    }
    const sid = session.id;
    if (busyBySessionRef.current[sid]) return; // one turn at a time
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
    // On retry reuse the original turnId — the server sets HumanMessage.id = turn_id,
    // so resending the same id lets LangGraph's add_messages deduplicate the user
    // message in the checkpoint (update-in-place rather than append).
    const existingTurn = userMsgId
      ? (storeRef.current[sid] ?? []).find((m) => m.id === userMsgId)?.turnId
      : undefined;
    const turnId = existingTurn ?? newTurnId();
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
    seenTurnStartRef.current[sid] = true;
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
    const p = propose;
    proposeRef.current[sid] = null;
    setPropose(null);
    // P0 polish: the card unmounts on decide, so leave a short receipt line
    // ("已应用 · v3 → 新版本" / "已拒绝") where the card used to be.
    if (p) {
      setProposeResult({ decision, workflowId: p.workflow_id, fromVersion: p.from_version });
      window.setTimeout(() => setProposeResult(null), 4000);
    }
  }

  function respondBrowserResume(space?: string) {
    const sid = curSessionIdRef.current;
    if (!sid) return;
    const name = space || handoffRef.current[sid]?.space || handoff?.space;
    try {
      socketsRef.current[sid]?.send(
        JSON.stringify({ type: "permission_response", decision: "browser_resume", space: name }),
      );
    } catch {
      /* socket gone — reconnect re-emits browser.handoff if still pending */
    }
    handoffRef.current[sid] = null;
    setHandoff(null);
    onBrowserHandoff?.(null);
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
  // Only the LAST failed/error bubble is retryable — re-invoking earlier ones
  // would re-insert stale user messages out of order in the server history.
  const lastRetryableId = messages.reduce<string | null>(
    (last, m) => ((m.status === "failed" || m.error) && m.sendPayload ? m.id : last),
    null,
  );

  const isHome = !session;

  // Home autofocus: the composer remounts on the home slot (keyed), so focus
  // must be re-armed after the transition.
  useEffect(() => {
    if (!session) textareaRef.current?.focus();
  }, [session]);

  // Composer box extracted so the landing home can center it. Same single
  // instance; the two keyed slots force a clean remount on home↔session
  // transitions (all composer state lives on ChatStream — only DOM focus is
  // lost, and home autofocuses above).
  const composerBoxEl = (
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
              placeholder="问点什么…  / 命令 · @ 提及产物/智能体/工作流/记忆 · 可拖入图片 / Excel / Word / PPT / PDF"
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
                {!isHome && (() => {
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
              {running && goalActive && (
                <button
                  onClick={() => void g.setGoalStatus(session!.id, "paused")}
                  title="暂停目标（当前轮跑完后停止自主续跑）"
                  aria-label="暂停目标"
                  className="flex h-8 w-8 items-center justify-center rounded-lg border border-line2 text-muted hover:text-txt"
                >
                  <Square className="h-3.5 w-3.5" />
                </button>
              )}
              {/* model chip: per-session provider/model switch (home: pick for
                  the session that first send will create) */}
              <div className="relative ml-auto">
                <button
                  type="button"
                  disabled={running || !!permission}
                  onClick={() => setModelOpen((v) => !v)}
                  title={session ? "切换本会话模型" : "选择新会话使用的模型"}
                  className="flex items-center gap-1.5 rounded-md border border-line2 bg-card px-2 py-1 text-xs text-muted hover:border-line hover:bg-card2 hover:text-txt disabled:opacity-50"
                >
                  <Globe className="h-3.5 w-3.5" />
                  <span className="max-w-[160px] truncate font-medium">{modelChipLabel}</span>
                  <ChevronDown className="h-3 w-3 opacity-70" />
                </button>
                {modelOpen && (
                  <>
                    <div className="fixed inset-0 z-40" onClick={() => setModelOpen(false)} />
                    <div className="absolute bottom-full right-0 z-50 mb-1 w-72 rounded-lg border border-line bg-card py-1 shadow-xl">
                      {enabledProviders.length === 0 && (
                        <div className="px-3 py-2 text-xs text-faint">
                          无已启用提供商 — 去 设置 → 模型 API 启用
                        </div>
                      )}
                      {enabledProviders.map(([pid, p]) => {
                        const m = p.default_model || p.model || "";
                        const on = (session ? session.provider : homeModel?.provider) === pid;
                        return (
                          <button
                            key={pid}
                            onClick={() => void pickModel(pid, m)}
                            className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-card2 ${
                              on ? "text-txt" : "text-muted"
                            }`}
                          >
                            <span className={on ? "" : "opacity-0"}>✓</span>
                            <span className="min-w-0 flex-1 truncate">
                              {p.name || pid} · {m}
                            </span>
                          </button>
                        );
                      })}
                      <div className="mt-1 border-t border-line px-3 pt-1 text-[10px] text-faint">
                        下一轮生效 · 设置页更改会覆盖会话级选择
                      </div>
                    </div>
                  </>
                )}
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
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {isHome ? (
        <div className="flex min-h-0 flex-1 flex-col items-center justify-center overflow-y-auto px-6 pb-10 pt-6">
          <Icon name="star" className="h-48 w-48" style={{ color: "rgba(233,233,240,0.05)" }} />
          <div className="mt-2 text-center text-[26px] font-semibold tracking-tight text-txt">
            {greeting()}
          </div>
          <div className="mt-2 text-center text-[13px] text-faint">
            交给 Agent：代码、文档、数据、工作流。
          </div>
          <div key="composer-home" className="mt-8 w-full max-w-[760px]">
            {composerBoxEl}
          </div>
          <div className="mt-6 flex max-w-[820px] flex-wrap justify-center gap-2.5">
            {[
              { icon: "📊", label: "分析拖入的 Excel / CSV", fill: "分析这份 7 月用量报表，找出异常增长" },
              { icon: "🔁", label: "跑一次晨报 workflow", fill: "/workflow 跑一次晨报" },
              { icon: "🧠", label: "@记忆 回顾上周决定", fill: "@记忆 上周我们定了什么方案？" },
            ].map((c) => (
              <button
                key={c.label}
                onClick={() => {
                  setInput(c.fill);
                  requestAnimationFrame(() => textareaRef.current?.focus());
                }}
                className="inline-flex items-center gap-2 rounded-full border border-line px-4 py-2 text-[12.5px] text-muted transition-colors hover:border-line2 hover:bg-card hover:text-txt"
              >
                <span>{c.icon}</span>
                {c.label}
              </button>
            ))}
            {!compact && (
              <button
                onClick={() => onOpenGoal?.()}
                className="inline-flex items-center gap-2 rounded-full border border-line px-4 py-2 text-[12.5px] text-muted transition-colors hover:border-line2 hover:bg-card hover:text-txt"
              >
                <span>🎯</span>
                设定一个长程目标
              </button>
            )}
          </div>
          <div className="mt-6 text-[11px] text-faint">
            支持拖入 Excel / Word / PPT / PDF · / 命令 · @ 提及产物 / 智能体 / 工作流 / 记忆
          </div>
        </div>
      ) : (
        <>
      {goal && goalStalled && !resumeDismissed && (
        <div className="mx-auto mb-2 flex w-full max-w-3xl items-center gap-2 rounded-lg border border-line2 bg-card px-3 py-2 text-xs">
          <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: "#f97316" }} />
          <span className="flex-1 text-muted">
            目标已{goal.status === "paused" ? "暂停" : goal.status === "blocked" ? "受阻" : "用量受限"}：
            <span className="text-txt">{goal.objective}</span>
          </span>
          <button
            onClick={() => void g.setGoalStatus(session!.id, "active")}
            className="rounded-md bg-violet px-2 py-1 text-[11px] font-medium text-white hover:opacity-90"
          >
            恢复
          </button>
          <button
            onClick={() => setResumeDismissed(true)}
            aria-label="关闭提示"
            className="rounded-md p-1 text-faint hover:text-txt"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
      <div
        ref={scrollRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}
        className={`flex-1 overflow-y-auto ${compact ? "px-3 py-3" : "px-6 py-6"}`}
      >
        <div className={`mx-auto flex flex-col gap-5 ${compact ? "max-w-none" : "max-w-3xl"}`}>
          {messages.length === 0 && (
            <div className="py-16 text-center text-sm text-faint">
              开始对话吧，Agent 会使用工具完成任务，并可能就权限询问你。
            </div>
          )}

          {messages.map((m) =>
            m.role === "system" ? (
              <ContextBlocks
                key={m.id}
                blocks={m.blocks.filter((b): b is Extract<Block, { kind: "context" }> => b.kind === "context")}
              />
            ) : m.role === "user" ? (
              <div key={m.id} className="flex flex-col items-end gap-1">
                <TurnIdChip turnId={m.turnId} />
                {/* w-full (not max-w-full): the row width must be definite so the
                    bubble's max-w-[78%] resolves against the column, not against
                    the row's own shrink-to-fit width — otherwise the percentage
                    collapses the bubble and the text overflows to the right. */}
                <div className="group flex w-full items-center justify-end gap-2">
                  {m.status === "failed" && (
                    <div className="flex shrink-0 items-center gap-1.5">
                      {m.id === lastRetryableId && (
                        <>
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
                        </>
                      )}
                      {m.id === lastRetryableId ? (
                        <button
                          onClick={() => retryFailed(m.id)}
                          title={`发送失败：${m.failReason ?? "未知原因"}（点击重试）`}
                          aria-label="发送失败，点击重试"
                          className="shrink-0 transition-transform hover:scale-110"
                        >
                          <AlertCircle className="h-[18px] w-[18px] text-red" />
                        </button>
                      ) : (
                        <span title={`发送失败：${m.failReason ?? "未知原因"}`}>
                          <AlertCircle className="h-[18px] w-[18px] shrink-0 text-red/50" />
                        </span>
                      )}
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
                    发送失败{m.failReason ? `：${m.failReason}` : ""}
                    {m.id === lastRetryableId && " · 点击红色感叹号重试"}
                  </div>
                )}
              </div>
            ) : m.error ? (
              <ErrorCard
                key={m.id}
                message={m.blocks[0]?.kind === "text" ? m.blocks[0].text : ""}
                turnId={m.turnId}
                canRetry={!!m.sendPayload && m.id === lastRetryableId}
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

          {runs.map((r) => (
            <LiveRunBlock
              key={r.id}
              run={r}
              onCancel={cancelRun}
              onPause={pauseRun}
              onContinue={continueRun}
              onRetry={retryRun}
              onRetryFromCheckpoint={retryRunFromCheckpoint}
              onDelete={(id) => setConfirmDelRun(id)}
            />
          ))}

          <div ref={bottomRef} />
        </div>
      </div>

      {confirmDelRun && (
        <ConfirmModal
          title="删除运行记录"
          message="删除该运行记录？事件日志与检查点将一并删除，此操作不可撤销。"
          confirmLabel="删除"
          onConfirm={() => {
            const id = confirmDelRun;
            setConfirmDelRun(null);
            deleteRun(id);
          }}
          onCancel={() => setConfirmDelRun(null)}
        />
      )}

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

      {propose && <ProposeCard propose={propose} onDecide={respondPropose} />}

      {handoff && (
        <HandoffCard
          space={handoff.space}
          url={handoff.url}
          reason={handoff.reason}
          onGo={() => onOpenBrowser?.()}
          onReturn={() => respondBrowserResume(handoff.space)}
        />
      )}

      {proposeResult && (
        <div className="mx-auto w-full max-w-3xl px-6">
          <div
            className={`anim-slide-in mb-2 flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs ${
              proposeResult.decision === "allow"
                ? "border-green/30 bg-green/[0.06] text-green"
                : "border-line bg-card2/40 text-muted"
            }`}
          >
            {proposeResult.decision === "allow" ? (
              <Check className="h-3 w-3" />
            ) : (
              <X className="h-3 w-3" />
            )}
            {proposeResult.decision === "allow"
              ? `已应用变更 · ${proposeResult.workflowId} v${proposeResult.fromVersion} → 新版本`
              : `已拒绝该 DSL 变更 · ${proposeResult.workflowId}`}
          </div>
        </div>
      )}

      {summarize && (
        <SummarizeModal
          dsl={summarize}
          busy={sumBusy}
          error={sumErr}
          createdName={sumCreated}
          sourceLabel={sumSource?.label}
          onClose={() => closeSummarize(!sumCreated)}
          onCreate={createFromSummarize}
          onRetry={sumSource?.id ? () => void freshSummarize(sumSource!.id) : undefined}
          onOpenDevSession={openDevFromSummarize}
        />
      )}

      {/* composer */}
      <div className={`${compact ? "px-3" : "px-6"} pb-5 pt-2`}>
        <div className={`mx-auto ${compact ? "max-w-none" : "max-w-3xl"}`}>
          {!compact && (
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
              onClick={() => g.setActiveSession(null)}
              className="flex items-center gap-1.5 rounded-lg border border-line bg-card px-2.5 py-1 text-xs text-muted hover:text-txt"
            >
              + New Session
            </button>
            {/* S1/S5: summarize entry — session picker + trace range (last N
                messages) live in one dropdown. */}
            <div className="relative">
              <button
                onClick={() => setSumMenuOpen((v) => !v)}
                disabled={sumLoading || g.sessions.length === 0}
                title="把会话总结成 workflow"
                className="flex items-center gap-1.5 rounded-lg border border-violet/40 bg-violet/10 px-2.5 py-1 text-xs text-violet hover:bg-violet/20 disabled:opacity-60"
              >
                {sumLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Zap className="h-3.5 w-3.5" />}
                {sumLoading ? "正在总结…" : "总结成流程"}
                <ChevronDown className="h-3 w-3 opacity-70" />
              </button>
              {sumMenuOpen && (
                <div className="absolute bottom-full left-0 z-30 mb-1 w-64 rounded-lg border border-line bg-card p-1 shadow-2xl">
                  {/* S6: an unsaved draft is an OPT-IN restore, never a blocker. */}
                  {savedDraft && (
                    <div className="mb-1 flex items-center gap-1 rounded-md border border-violet/30 bg-violet/[0.06] px-2 py-1.5">
                      <button
                        onClick={openDraftModal}
                        title="恢复这份未保存的草稿"
                        className="flex min-w-0 flex-1 items-center gap-1.5 text-left text-[11px] text-violet hover:opacity-80"
                      >
                        <RotateCcw className="h-3 w-3 shrink-0" />
                        <span className="truncate">恢复草稿 · {relTime(savedDraft.savedAt / 1000)}</span>
                      </button>
                      <button
                        onClick={deleteDraft}
                        title="删除草稿"
                        className="shrink-0 rounded p-0.5 text-faint hover:bg-red/10 hover:text-red"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </div>
                  )}
                  <div className="px-2 pb-1 pt-1.5 text-[10px] font-medium uppercase tracking-wide text-faint">
                    选择要总结的会话
                  </div>
                  {g.sessions.slice(0, 10).map((s) => (
                    <button
                      key={s.id}
                      onClick={() => void openSummarize(s.id)}
                      className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs text-muted hover:bg-card2 hover:text-txt"
                    >
                      {s.id === session?.id && <span className="text-violet">●</span>}
                      <span className="max-w-[150px] truncate">{s.title || "未命名会话"}</span>
                      {s.id === session?.id && <span className="text-[10px] text-faint">（推荐）</span>}
                      <span className="ml-auto shrink-0 text-[10px] text-faint">{relTime(s.updated)}</span>
                    </button>
                  ))}
                  <div className="mt-1 border-t border-line2 px-2 pb-1 pt-1.5">
                    <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-faint">范围</div>
                    <div className="flex gap-1">
                      {([null, 5, 10, 20] as const).map((n) => (
                        <button
                          key={String(n)}
                          onClick={() => setSumLastN(n)}
                          className={`rounded px-1.5 py-0.5 text-[10px] ${
                            sumLastN === n
                              ? "bg-violet/15 text-violet"
                              : "text-faint hover:bg-card2 hover:text-muted"
                          }`}
                        >
                          {n === null ? "全部" : `最近 ${n} 条`}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
              )}
            </div>
          </div>
          )}

          <div key="composer-session">{composerBoxEl}</div>
        </div>
      </div>
        </>
      )}
    </div>
  );
}

/** workflow_propose_edit diff confirmation card (workflow-ux-redesign P0
 *  polish): busy buttons + collapsed-by-default diff with hunk count. The
 *  session graph is paused at the tool's interrupt until the user decides. */
function ProposeCard({
  propose,
  onDecide,
}: {
  propose: VersionPropose;
  onDecide: (decision: "allow" | "deny") => void;
}) {
  const [busy, setBusy] = useState<null | "allow" | "deny">(null);
  const [diffOpen, setDiffOpen] = useState(false);
  const hunks = (propose.diff.match(/^@@/gm) || []).length;
  const decide = (d: "allow" | "deny") => {
    if (busy) return;
    setBusy(d);
    onDecide(d); // the card unmounts when the server clears the pending propose
  };
  return (
    <div className="mx-auto w-full max-w-3xl px-6">
      <div className="mb-2 rounded-xl border border-yellow/30 bg-yellow/[0.04] p-3">
        <div className="mb-1 flex items-center gap-2 text-sm font-medium text-yellow">
          <FileEdit className="h-3.5 w-3.5" />
          DSL 变更提案
          <span className="rounded border border-yellow/40 px-1.5 py-0.5 text-[10px] font-normal text-muted">
            {propose.workflow_id} · v{propose.from_version} → 新版本
          </span>
        </div>
        {propose.rationale && (
          <div className="mb-2 text-xs text-muted">理由：{propose.rationale}</div>
        )}
        <button
          onClick={() => setDiffOpen((v) => !v)}
          className="mb-2 flex items-center gap-1 text-[11px] text-faint hover:text-muted"
        >
          <ChevronDown className={`h-3 w-3 transition-transform ${diffOpen ? "" : "-rotate-90"}`} />
          {diffOpen ? "收起 diff" : `查看完整 diff（${hunks} 处改动）`}
        </button>
        {diffOpen && <DiffView diff={propose.diff} />}
        <div className="mt-3 flex gap-2">
          <button
            onClick={() => decide("allow")}
            disabled={!!busy}
            className="btn-press flex items-center gap-1 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            {busy === "allow" && <Loader2 className="h-3 w-3 animate-spin" />}
            {busy === "allow" ? "应用中…" : "应用变更（创建新版本）"}
          </button>
          <button
            onClick={() => decide("deny")}
            disabled={!!busy}
            className="btn-press flex items-center gap-1 rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:bg-red/10 hover:text-red disabled:opacity-50"
          >
            {busy === "deny" && <Loader2 className="h-3 w-3 animate-spin" />}
            拒绝
          </button>
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
