"use client";

/**
 * PinStream — the floating quick-chat window's conversation view
 * (docs/floating-window-design.md §2.4 / §4 Phase 3).
 *
 * A deliberately lightweight sibling of ChatStream: ONE session per mount
 * (the parent keys the component by session id) and the 13-event WS subset.
 * The wire protocol is 100% unchanged — the runtime already broadcasts every
 * turn to ALL sockets of a session, so the pin window and the main window can
 * watch (and answer permission prompts for) the same turn; `_PENDING_RESUME`
 * dedupes the responses.
 *
 * Deliberate duplication over abstraction: applyBlock / socket lifecycle
 * mirror ChatStream's semantics (3s reconnect, 20s ping, 45s silence close,
 * post-reconnect `turn_state` probe with a 6s reconcile fallback) without
 * touching the 3400-line monolith.
 *
 * NOT duplicated: the steer queue and the composer's attachment plumbing come
 * from the shared modules (`lib/steerQueue`, `lib/composerAttachments`) — a
 * third copy of the ack-timing-sensitive queue is exactly what we are not
 * doing (steer-queue-shared-brief §1).
 */

import { useEffect, useRef, useState } from "react";
import { AlertCircle, ArrowUp, Loader2, Paperclip, RotateCcw, Square, X } from "lucide-react";
import { getSessionHistory, openSessionSocket } from "@/lib/runtime";
import { loadToolLabels, toolLabel } from "@/lib/toolLabels";
import { useSteerQueue, type SteerItem } from "@/lib/steerQueue";
import {
  readImage,
  uploadDoc,
  isImageFile,
  type ComposerImage,
  type ComposerFile,
} from "@/lib/composerAttachments";
import {
  ContextBlocks,
  InnerBlocks,
  UserBlocks,
  hasPendingTool,
  type Block,
} from "@/components/chat/blocks";

export type PinStreamStatus = "idle" | "busy" | "permission";

/**
 * DOM relay event: "put the caret in the composer now". Fired by PinApp when
 * the shell signals a summon (the Tauri `pin:focus-input` event — global
 * hotkey / tray show) and on every pill→mini expansion. A plain window-focus
 * listener won't do: refocusing on EVERY activation would steal the user's
 * transcript selection when they merely tab back. PinStream stays
 * shell-agnostic by listening on `window` instead of Tauri directly.
 */
export const PIN_FOCUS_INPUT = "ginno-pin:focus-input";

export function requestComposerFocus() {
  window.dispatchEvent(new CustomEvent(PIN_FOCUS_INPUT));
}

interface PinMsg {
  id: string;
  // "system" = WorldState context chip rows (centered, not a bubble)
  role: "user" | "assistant" | "system";
  blocks: Block[];
  turnId?: string;
  /** Turn-error card (rendered red, with a retry button). */
  error?: boolean;
  /** Error cards: the originating user text, for retry. */
  retryText?: string;
}

interface PermissionPrompt {
  tool: string;
  args: unknown;
}

let _mid = 0;
const mid = () => `pm${++_mid}`;

const newTurnId = () =>
  typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `t-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

// Patterns that indicate a tool returned "no results" — hide these blocks
// (mirror of ChatStream's noise filter).
const EMPTY_TOOL_RESULT_RE =
  /^\s*(\(no matches\)|\(no files found\)|\(empty\)|no results|no files matched|no matches found|no files found|\(nothing found\))\s*$/i;

/** Streaming-event → block-list reducer (subset of ChatStream.applyBlock). */
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
    case "image.emit":
      return [
        ...blocks,
        {
          kind: "image",
          fileId: ev.file_id as string,
          name: ev.name as string | undefined,
          mtime: ev.mtime as number | undefined,
        },
      ];
    default:
      return blocks;
  }
}

function mapHistory(res: {
  messages?: Array<{ id?: string; role: "user" | "assistant"; blocks: Block[]; turnId?: string }>;
  last_error?: { message?: string; turn_id?: string } | null;
}): PinMsg[] {
  const msgs: PinMsg[] = (res.messages ?? []).map((m) => ({
    id: m.id ?? mid(),
    role: m.role === "user" ? "user" : "assistant",
    blocks: m.blocks ?? [],
    turnId: m.turnId,
  }));
  // Persisted turn failure: re-surface it so the retry affordance survives
  // a window reopen (same contract as ChatStream's mapHistory).
  if (res.last_error?.message) {
    const lastUser = [...msgs].reverse().find((m) => m.role === "user");
    const text = lastUser?.blocks.find((b) => b.kind === "text");
    msgs.push({
      id: mid(),
      role: "assistant",
      error: true,
      blocks: [{ kind: "text", text: res.last_error.message }],
      retryText: text && "text" in text ? text.text : undefined,
    });
  }
  return msgs;
}

export function PinStream({
  sessionId,
  onStatus,
}: {
  sessionId: string;
  /** Aggregated activity for the pill's status dot (green/orange/grey). */
  onStatus?: (s: PinStreamStatus) => void;
}) {
  const [messages, setMessages] = useState<PinMsg[]>([]);
  const [liveId, setLiveId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [permission, setPermission] = useState<PermissionPrompt | null>(null);
  const [wsStatus, setWsStatus] = useState<"connecting" | "live" | "reconnecting">("connecting");
  const [input, setInput] = useState("");
  const [sendError, setSendError] = useState<string | null>(null);
  // Composer attachments. Images ride the turn as base64 (multimodal); docs are
  // uploaded to the sidecar first and referenced by registry id.
  const [images, setImages] = useState<ComposerImage[]>([]);
  const [files, setFiles] = useState<ComposerFile[]>([]);
  /** readImage() in flight — `images` lags until it resolves, so sending now
   *  would silently drop the picture (the main window's original bug). */
  const [imagesReading, setImagesReading] = useState(0);
  /** Transient, visible composer feedback for a blocked send (never a silent
   *  no-op — brief §4.3). Auto-clears; the ⏹-hint strip is the same slot. */
  const [hint, setHint] = useState<string | null>(null);

  // Shared steer queue (brief §3.1): the pin owns only how it is DISPLAYED
  // (a compact `⏳ 待注入 N` line) and where a recalled entry goes (back into
  // this composer). Acks, re-sends and the flush-on-turn-end live in the hook.
  const steer = useSteerQueue();
  // Socket callbacks outlive renders (the socket is created once per mount), so
  // they read the hook through a ref: whatever memoisation Q chose, the pin
  // never calls a stale queue API.
  const steerRef = useRef(steer);
  useEffect(() => {
    steerRef.current = steer;
  });
  const steerItems = steer.itemsFor(sessionId);

  const sockRef = useRef<WebSocket | null>(null);
  // The turn the composer is writing into; "" lets the server use its own
  // running-turn id (SteerItem.turnId contract).
  const turnIdRef = useRef("");
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const hintTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
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
  // Last user text, kept for the error card's retry button.
  const lastUserTextRef = useRef<string | null>(null);

  // Tool display labels come from settings (module-cached — see toolLabels).
  useEffect(() => {
    loadToolLabels();
  }, []);

  // Summon-focus: the textarea's autoFocus only runs at FIRST mount, but this
  // webview survives hide/show cycles — a hotkey-summoned window would have
  // OS focus yet no caret, so typing went nowhere until a manual click.
  // The offsetParent check skips the pill-shape's display:none mount, where
  // focus() would silently no-op anyway.
  useEffect(() => {
    function onFocusInput() {
      const el = textareaRef.current;
      if (el && el.offsetParent !== null) el.focus({ preventScroll: true });
    }
    window.addEventListener(PIN_FOCUS_INPUT, onFocusInput);
    return () => window.removeEventListener(PIN_FOCUS_INPUT, onFocusInput);
  }, []);

  const status: PinStreamStatus = permission ? "permission" : busy || liveId ? "busy" : "idle";
  useEffect(() => {
    onStatus?.(status);
  }, [status, onStatus]);

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

  /** Finish the in-flight bubble. finalizeTools: force-close pending tool
   *  blocks (stop/error paths — the server never sends their tool.end). */
  function closeLive(finalizeTools: boolean) {
    const id = liveRef.current;
    liveRef.current = null;
    setLiveId(null);
    busyRef.current = false;
    setBusy(false);
    // No live turn → the next steer has no turn to ride (mirrors ChatStream's
    // liveTurnIdFor returning "").
    turnIdRef.current = "";
    setMessages((prev) => {
      let next = prev;
      if (id) {
        // An empty live bubble (ended before the first token) would render
        // as a confusing blank card — drop it; streamed content stays.
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

  /** Park an absorbed steer in the live bubble as a `steer` band. The hook
   *  drops the entry from the queue when it sees `steer.absorbed`, so the entry
   *  is captured BEFORE delegating; the band itself is display, which brief
   *  §3.1 leaves to the caller (blocks.tsx already renders `kind: "steer"`). */
  function appendSteerBand(entry: SteerItem, ev: { [k: string]: unknown }) {
    const id = ensureLive();
    const band: Block = {
      kind: "steer",
      text: entry.text,
      steerId: entry.steerId,
      injectedAt: Number(ev.injected_at) || Math.floor(Date.now() / 1000),
      ...(entry.images.length
        ? { images: entry.images.map((a) => ({ name: a.name, url: a.preview })) }
        : {}),
      ...(entry.files.length
        ? {
            files: entry.files.map((f) => ({
              id: f.id,
              name: f.name,
              path: f.path,
              kind: f.kind,
            })),
          }
        : {}),
    };
    setMessages((prev) =>
      prev.map((m) => (m.id === id ? { ...m, blocks: [...m.blocks, band] } : m)),
    );
  }

  function handle(ev: { event: string; [k: string]: unknown }) {
    // Every frame is fed to the shared queue: it owns the steer acks (and only
    // those two events — it returns false for the rest). The pin deliberately
    // does NOT early-return on its `true` ("this event is the queue's"): the
    // hook only ever touches its own queue state, never this component's
    // messages, so both can observe the frame.
    // steer.absorbed: the hook drops the entry, so capture it first — the band
    // is display, which brief §3.1 leaves to the caller.
    const absorbedEntry =
      ev.event === "steer.absorbed" && ev.steer_id
        ? steerRef.current.itemsFor(sessionId).find((i) => i.steerId === ev.steer_id)
        : undefined;
    steerRef.current.handleEvent(ev, sessionId);
    if (absorbedEntry) appendSteerBand(absorbedEntry, ev);

    switch (ev.event) {
      case "turn.start": {
        busyRef.current = true;
        setBusy(true);
        setSendError(null);
        const id = ensureLive();
        const srvTurn = ev.turn_id as string | undefined;
        // The server's turn id: a queued steer rides this turn (the pin keeps
        // no per-session turn map — one session per mount).
        if (srvTurn) turnIdRef.current = srvTurn;
        setMessages((prev) =>
          prev.map((m) => (m.id === id && srvTurn ? { ...m, turnId: srvTurn } : m)),
        );
        break;
      }
      case "token.delta":
      case "thinking.delta":
      case "tool.start":
      case "tool.args":
      case "tool.end":
      case "image.emit": {
        busyRef.current = true;
        setBusy(true);
        mutateLive(ev);
        break;
      }
      case "permission.request":
        setPermission({ tool: ev.tool as string, args: ev.args });
        break;
      case "notice":
        // Built-in command reply / busy-notice (e.g. follow mode: the main
        // window already runs a turn) — rendered into the live bubble.
        mutateLive({ event: "token.delta", content: (ev.message as string) || "" });
        break;
      case "context.updated": {
        const changes = ((ev.changes as Array<{ section?: string; summary?: string }>) || []).filter(
          Boolean,
        );
        const visible = changes.filter((c) => c.section !== "environment");
        for (const c of visible) addSystemRow(c.summary || "");
        break;
      }
      case "context.microcompacted":
        addSystemRow(
          `已清理 ${Number(ev.cleared_tool_outputs ?? 0)} 条较早的工具输出以节省上下文。`,
        );
        break;
      case "context.compacted":
        addSystemRow(
          `对话已压缩：${Number(ev.compacted_messages ?? 0)} 条较早的消息被摘要替代。`,
        );
        break;
      case "turn.state":
        // Answer to the post-reconnect probe: no turn running → whatever we
        // held as live is stale; rebuild from the persisted history.
        if (reconcileRef.current) {
          clearTimeout(reconcileRef.current);
          reconcileRef.current = null;
        }
        if (ev.running) break; // the broadcast stream resumes on this socket
        reconcileFromHistory();
        busyRef.current = false;
        setBusy(false);
        break;
      case "message.end":
        closeLive(false);
        // The turn is over — any parked prompt is moot (it may even have been
        // answered from the main window; `_PENDING_RESUME` dedupes server-side).
        setPermission(null);
        // Still-queued entries were never absorbed: the oldest becomes the next
        // turn (design §3.3), the rest follow on that turn's end.
        flushSteerQueue();
        break;
      case "turn.stopped":
        // ⏹ means stop: hand anything queued for absorption back to the
        // composer — text AND attachments — instead of discarding it. Done in
        // the event (like ChatStream) so a stop issued from the main window
        // recalls here too.
        recallSteers();
        closeLive(true);
        setPermission(null);
        break;
      case "error": {
        closeLive(true);
        setPermission(null);
        const text = String(ev.message || "") || "未知错误";
        setMessages((prev) => [
          ...prev,
          {
            id: mid(),
            role: "assistant",
            error: true,
            blocks: [{ kind: "text", text }],
            retryText: lastUserTextRef.current ?? undefined,
          },
        ]);
        // The turn died with entries still unacknowledged → the oldest becomes
        // the next turn, same rule as a normal end (design §3.3).
        flushSteerQueue();
        break;
      }
      default:
        // usage / goal.* / run.* / preview.* / todos.changed / … — ignored by
        // design (floating-window-design.md §2.4).
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
      // Re-send what is still queued (brief §3.1): a reconnect is exactly when
      // an entry stashed into a dropped socket would otherwise be lost.
      steerRef.current.onResume(sessionId, sendFrame);
      if (liveRef.current || busyRef.current) {
        // Remounted mid-turn: ask whether the turn still exists; if the
        // server never answers (older runtime), reconcile from history.
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

  // Socket + history lifecycle. The parent keys PinStream by session id, so
  // a session switch is a clean remount (socket closed in the cleanup).
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
      if (hintTimerRef.current) clearTimeout(hintTimerRef.current);
      reconnRef.current = pingRef.current = watchRef.current = reconcileRef.current = null;
      hintTimerRef.current = null;
      const sock = sockRef.current;
      sockRef.current = null; // detach first: onclose must not schedule a reconnect
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

  // Sticky auto-scroll: follow the stream while parked near the bottom.
  useEffect(() => {
    if (stickRef.current) bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  function onScroll() {
    const el = scrollRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  }

  /** Transient, visible composer feedback (never a silent no-op). */
  function showHint(msg: string) {
    setHint(msg);
    if (hintTimerRef.current) clearTimeout(hintTimerRef.current);
    hintTimerRef.current = setTimeout(() => setHint(null), 4000);
  }

  /** Raw-frame sender handed to the shared queue — the pin's own socket
   *  (brief §3.1: `send` is per-window). A dead socket is reported, not
   *  swallowed: the entry stays queued and rides the next turn / reconnect. */
  function sendFrame(frame: unknown) {
    const sock = sockRef.current;
    if (!sock || sock.readyState !== WebSocket.OPEN) {
      setSendError("连接未就绪，正在重连…");
      return;
    }
    try {
      sock.send(JSON.stringify(frame));
    } catch {
      setSendError("发送失败，请重试");
    }
  }

  /** ⏹ recall: put what is still queued back into THIS composer — text (in
   *  order, above whatever is typed) and the attachments that rode the entry,
   *  which would otherwise be lost silently (brief §4.4 / design §4.1). */
  function recallSteers() {
    const recalled = steerRef.current.recall(sessionId);
    if (!recalled.length) return;
    const joined = recalled.map((i) => i.text).join("\n");
    if (joined) setInput((cur) => (cur ? `${joined}\n${cur}` : joined));
    const imgs = recalled.flatMap((i) => i.images);
    const fs = recalled.flatMap((i) => i.files);
    if (imgs.length) setImages((a) => [...imgs, ...a]);
    if (fs.length) setFiles((a) => [...fs, ...a]);
  }

  /** 📎 picker → read/compress images, upload docs. No drag & drop and no
   *  clipboard paste here (user decision, brief §4.1). */
  async function addFiles(list: FileList | File[] | null) {
    if (!list?.length) return;
    const arr = Array.from(list);
    const imgs = arr.filter(isImageFile);
    const docs = arr.filter((f) => !isImageFile(f));
    if (imgs.length) {
      // Bracket the async read so sendText() can gate on it (see imagesReading).
      setImagesReading((n) => n + 1);
      try {
        const items = await Promise.all(imgs.map((f) => readImage(f)));
        const ok = items.filter((x): x is ComposerImage => !!x);
        if (ok.length) setImages((a) => [...a, ...ok]);
      } catch {
        showHint("图片读取失败，请重试");
      } finally {
        setImagesReading((n) => Math.max(0, n - 1));
      }
    }
    for (const f of docs) {
      // Optimistic chip (uploading) for instant feedback; uploadDoc() resolves
      // to the registered entry. The pin always has a session, so docs upload
      // immediately — no deferred-until-created path is needed.
      const tmpId = `up-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      setFiles((a) => [...a, { id: tmpId, name: f.name, path: "", kind: "", uploading: true }]);
      try {
        const entry = await uploadDoc(sessionId, f);
        setFiles((a) => a.map((x) => (x.id === tmpId ? { ...entry } : x)));
      } catch {
        setFiles((a) => a.filter((x) => x.id !== tmpId));
        showHint(`附件上传失败：${f.name}`);
      }
    }
  }

  function sendText(raw: string) {
    const text = raw.trim();
    const readyFiles = files.filter((f) => !f.uploading);
    // ── in-flight gate (brief §4.3). Both halves used to be a silent return in
    // the main window and read as a dead send button; say why instead. ──
    if (imagesReading > 0) {
      showHint("注意：图片处理中，请稍候再发送");
      return;
    }
    if (readyFiles.length !== files.length) {
      showHint("注意：文件上传中，请稍候再发送");
      return;
    }
    if (!text && images.length === 0 && readyFiles.length === 0) return;

    if (busyRef.current) {
      // A turn is running → this is a mid-turn injection, not a new turn: the
      // shared queue owns the frame, the acks and the re-sends. Text AND
      // attachments ride the steer (brief §4.4); an empty-text steer is legal.
      steerRef.current.enqueue({
        sessionId,
        turnId: turnIdRef.current,
        text,
        images,
        files: readyFiles,
        send: sendFrame,
      });
      setInput("");
      setImages([]);
      setFiles([]);
      requestAnimationFrame(() => {
        if (textareaRef.current) textareaRef.current.style.height = "auto";
      });
      return;
    }

    postTurn(text, images, readyFiles);
  }

  /** Post one real turn. Called by the composer AND by the queue's flush (the
   *  turn ended with an entry still unacknowledged → the OLDEST becomes the
   *  next turn, design §3.3). The socket check lives here so both callers get
   *  the same no-silent-loss treatment.
   *
   *  `fromQueue`: a flushed entry has ALREADY left the queue, so a dead socket
   *  must put it back into the composer or it is gone. The composer's own send
   *  needs no such restore — nothing was cleared yet, and re-appending the text
   *  on top of itself would duplicate the input. */
  function postTurn(
    text: string,
    imgs: ComposerImage[],
    readyFiles: ComposerFile[],
    fromQueue = false,
  ) {
    const sock = sockRef.current;
    if (!sock || sock.readyState !== WebSocket.OPEN) {
      // No silent no-op: tell the user and keep the content (the reconnect loop
      // runs in the background; they can press Enter again).
      if (fromQueue) {
        if (text) setInput((cur) => (cur ? `${text}\n${cur}` : text));
        if (imgs.length) setImages((a) => [...imgs, ...a]);
        if (readyFiles.length) setFiles((a) => [...readyFiles, ...a]);
      }
      setSendError("连接未就绪，正在重连…");
      return;
    }
    const turnId = newTurnId();
    const uid = mid();
    const lid = mid();
    lastUserTextRef.current = text;
    turnIdRef.current = turnId;
    busyRef.current = true;
    setBusy(true);
    liveRef.current = lid;
    setLiveId(lid);
    setSendError(null);
    setMessages((prev) => [
      ...prev,
      {
        id: uid,
        role: "user",
        blocks: [
          ...readyFiles.map((f) => ({
            kind: "file" as const,
            fileId: f.id,
            name: f.name,
            path: f.path,
            fileKind: f.kind,
          })),
          ...imgs.map((a) => ({ kind: "image" as const, url: a.preview })),
          ...(text ? [{ kind: "text" as const, text }] : []),
        ],
        turnId,
      },
      { id: lid, role: "assistant", blocks: [] },
    ]);
    setInput("");
    setImages([]);
    setFiles([]);
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
          images: imgs.map((a) => ({ data: a.data, media_type: a.mediaType })),
          files: readyFiles.map((f) => ({ id: f.id, name: f.name, path: f.path })),
        }),
      );
    } catch {
      // Socket flipped to CLOSING between the check and here: roll back.
      busyRef.current = false;
      setBusy(false);
      liveRef.current = null;
      setLiveId(null);
      turnIdRef.current = "";
      setMessages((prev) => prev.filter((m) => m.id !== lid && m.id !== uid));
      // Hand text and attachments back — a failed send must not eat them.
      if (text) setInput((cur) => (cur ? `${text}\n${cur}` : text));
      if (imgs.length) setImages((a) => [...imgs, ...a]);
      if (readyFiles.length) setFiles((a) => [...readyFiles, ...a]);
      setSendError("发送失败，请重试");
    }
  }

  /** The turn ended (or died) with entries still unacknowledged: promote the
   *  OLDEST to the next turn — Claude Code's rule verbatim, same as ChatStream
   *  (design §3.3). The hook pops the head; SENDING it is the caller's half
   *  (the hook has no socket). Attachments ride it, or they would be lost. */
  function flushSteerQueue() {
    const head = steerRef.current.takeOldest(sessionId);
    if (!head) return;
    postTurn(head.text, head.images, head.files, true);
  }

  function stopTurn() {
    try {
      sockRef.current?.send(JSON.stringify({ type: "stop" }));
    } catch {
      /* socket gone — reconnect reconciles via turn_state */
    }
  }

  function respond(decision: "allow" | "deny") {
    // Anything still queued belongs to this turn: re-send it as `steer` BEFORE
    // the resume, so the resumed segment absorbs it (same rule as ChatStream's
    // respond — idempotent by steer_id server-side). The pin has no
    // change-of-mind/deny-and-steer path; only the re-send rule is mirrored.
    steerRef.current.onResume(sessionId, sendFrame);
    try {
      sockRef.current?.send(JSON.stringify({ type: "permission_response", decision }));
    } catch {
      /* socket gone — reconnect re-emits the prompt if still pending */
    }
    setPermission(null);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      sendText(input);
    }
    // Esc / ⌘↵ bubble up to PinApp's window-level handlers (collapse / open main).
  }

  function onInput(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setInput(e.target.value);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 112)}px`;
  }

  const connecting = wsStatus !== "live";
  // A text-less send is legal when an attachment rides along (brief §3.3).
  const canSend = !!input.trim() || images.length > 0 || files.length > 0;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
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
                {m.retryText && (
                  <button
                    type="button"
                    onClick={() => sendText(m.retryText!)}
                    disabled={busy}
                    className="mt-1.5 flex items-center gap-1 rounded-lg border border-line2 px-2 py-1 text-[11px] text-muted transition-colors hover:border-violet/50 hover:text-txt disabled:opacity-50"
                  >
                    <RotateCcw className="h-3 w-3" />
                    重试
                  </button>
                )}
              </div>
            );
          }
          const streaming = m.id === liveId;
          return (
            <div key={m.id} className="max-w-[94%] rounded-2xl rounded-bl-md border border-line bg-card px-3 py-2 text-[13px] leading-relaxed text-txt">
              <div className="pin-compact">
                <InnerBlocks blocks={m.blocks} streaming={streaming} />
              </div>
              {streaming && m.blocks.length === 0 && (
                <Loader2 className="h-3.5 w-3.5 animate-spin text-faint" />
              )}
            </div>
          );
        })}
        <div ref={bottomRef} />
      </div>

      {/* inline permission confirmation (Q1: full capability in the pin) */}
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

      {/* connection / send hint strip */}
      {(connecting || sendError || hint) && (
        <div className="flex items-center gap-1.5 px-3 pb-1 text-[11px] text-faint">
          {connecting && <Loader2 className="h-3 w-3 animate-spin" />}
          {sendError ?? hint ?? (wsStatus === "reconnecting" ? "连接断开，重连中…" : "连接中…")}
        </div>
      )}

      {/* composer */}
      <div className="border-t border-line px-2.5 py-2">
        {/* attachments — same chip look as the main composer, sized for the pin */}
        {images.length > 0 && (
          <div className="mb-1.5 flex flex-wrap gap-1.5">
            {images.map((a, i) => (
              <div key={i} className="group relative">
                <img
                  src={a.preview}
                  alt={a.name ?? "图片"}
                  title={a.name ?? "图片"}
                  className="h-10 w-10 rounded-lg border border-line object-cover"
                />
                <button
                  type="button"
                  onClick={() => setImages((l) => l.filter((_, j) => j !== i))}
                  aria-label={`移除 ${a.name ?? "图片"}`}
                  className="absolute -right-1.5 -top-1.5 flex h-[18px] w-[18px] items-center justify-center rounded-full bg-red text-white opacity-0 shadow transition-opacity group-hover:opacity-100"
                >
                  <X className="h-3 w-3" />
                </button>
              </div>
            ))}
          </div>
        )}
        {files.length > 0 && (
          <div className="mb-1.5 flex flex-wrap gap-1.5">
            {files.map((f, i) => (
              <div
                key={f.id}
                className="group relative flex items-center gap-1 rounded-lg border border-line bg-card2 px-2 py-1 text-[11px] text-txt"
              >
                <Paperclip className="h-3 w-3 shrink-0 text-faint" />
                <span className="max-w-[130px] truncate" title={f.name}>
                  {f.name}
                </span>
                {f.uploading && <span className="text-faint">上传中…</span>}
                <button
                  type="button"
                  onClick={() => setFiles((l) => l.filter((_, j) => j !== i))}
                  aria-label={`移除 ${f.name}`}
                  className="absolute -right-1.5 -top-1.5 flex h-[18px] w-[18px] items-center justify-center rounded-full bg-red text-white opacity-0 shadow transition-opacity group-hover:opacity-100"
                >
                  <X className="h-3 w-3" />
                </button>
              </div>
            ))}
          </div>
        )}
        {imagesReading > 0 && (
          <div className="mb-1.5 flex items-center gap-1.5 text-[11px] text-faint">
            <Loader2 className="h-3 w-3 animate-spin" />
            图片处理中…
          </div>
        )}
        {steerItems.length > 0 && (
          // Compact queue badge — the main window's full queue bar is too wide
          // for the pin (brief §4.4). Details live in the tooltip; ⏹ takes them
          // back.
          <div
            className="mb-1.5 flex items-center gap-1.5 px-0.5 text-[11px] text-muted"
            title={`待注入 ${steerItems.length} 条 · 停止可取回\n${steerItems
              .map((it) => it.text || "（仅附件）")
              .join("\n")}`}
          >
            {steerItems.some((it) => it.status === "sending") ? (
              <Loader2 className="h-3 w-3 shrink-0 animate-spin text-faint" />
            ) : (
              <span className="shrink-0">⏳</span>
            )}
            <span className="truncate">待注入 {steerItems.length}</span>
            <span className="shrink-0 text-faint">停止可取回</span>
          </div>
        )}
        <div className="flex items-end gap-1.5 rounded-xl border border-line2 bg-card px-2.5 py-1.5 transition-colors focus-within:border-violet/60">
          <textarea
            ref={textareaRef}
            rows={1}
            autoFocus
            value={input}
            onChange={onInput}
            onKeyDown={onKeyDown}
            placeholder="问点什么…（Enter 发送，Esc 收起）"
            className="max-h-28 min-h-[24px] flex-1 resize-none bg-transparent text-[13px] leading-6 text-txt outline-none placeholder:text-faint"
          />
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*,.xlsx,.xls,.xlsm,.csv,.tsv,.docx,.pptx,.pdf,.json,.xml,.txt,.md"
            multiple
            className="hidden"
            onChange={(e) => {
              void addFiles(e.target.files);
              e.target.value = "";
            }}
          />
          {/* The pin is always mounted with a session (PinApp renders it keyed
              by activeId), so the picker never has to create one. */}
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            title="添加附件（图片 / Excel / Word / PPT / PDF）"
            aria-label="添加附件"
            className="mb-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-lg text-faint transition-colors hover:bg-card2 hover:text-muted"
          >
            <Paperclip className="h-3.5 w-3.5" />
          </button>
          {/* Send stays available while busy: that is the mid-turn injection
              (brief §4.4), and hiding it behind Enter made it undiscoverable. */}
          <button
            type="button"
            onClick={() => sendText(input)}
            disabled={!canSend}
            title={busy ? "注入本轮（Enter）" : "发送"}
            className="mb-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-violet text-white transition-opacity hover:opacity-90 disabled:opacity-30"
          >
            <ArrowUp className="h-3.5 w-3.5" />
          </button>
          {busy && (
            <button
              type="button"
              onClick={stopTurn}
              title="停止本轮"
              className="mb-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-red/15 text-red transition-colors hover:bg-red/25"
            >
              <Square className="h-3 w-3" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
