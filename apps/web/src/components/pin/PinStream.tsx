"use client";

/**
 * PinStream — the floating quick-chat window's conversation view
 * (docs/floating-window-design.md §2.4 / §4 Phase 3). ONE session per mount
 * (the parent keys the component by session id).
 *
 * Refactor batch 3-4: socket lifecycle + message loop live in
 * `usePinSessionLoop` (built on the shared `components/session/` layer).
 * The batch-3-1 (#240) semantics stay pinned in `pinStreamCore`
 * (turn_state probe-sequence guard, believing-live periodic probe);
 * applyBlock itself is now a mainline superset shared via sessionBlocks
 * (P1-4) — pin, dev drawer and the main window run the same reducer.
 *
 * Stays HERE by design: the composer (IME-safe Enter, ↑/↓ input history,
 * attachments, in-flight gates), the steer queue's display/recall path,
 * the AgentTeam trio (P2-24), and all rendering.
 */

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { useEventI18nText } from "@/components/chat/blocks";
import {
  appendEntry,
  caretOnFirstLine,
  caretOnLastLine,
  initHistory,
  navDown,
  navUp,
  seedEntries,
  userTextFromBlocks,
  type HistoryState,
} from "@/components/chat/inputHistory";
import { AlertCircle, ArrowUp, ExternalLink, Loader2, Paperclip, RotateCcw, Square, X } from "lucide-react";
import { invoke } from "@tauri-apps/api/core";
import { isDesktop } from "@/lib/desktop";
import type { TeamReplyMeta } from "@/lib/types";
import { mergeHistoryWithLive } from "@/lib/historyMerge";
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
  type Block,
} from "@/components/chat/blocks";
import { mapHistoryToMsgs, type SessionMsg } from "@/components/session/sessionBlocks";
import { usePinSessionLoop, type PinTurnApi } from "@/components/pin/usePinSessionLoop";

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

interface PinMsg extends SessionMsg {
  /** Turn-error card: the originating user text, for retry. */
  retryText?: string;
  /** P2-24：团队成员回帖（team.reply 实时帧 / history 的 teamReply 标记）。
   *  渲染为带归属徽标的回帖卡，而不是普通 assistant 气泡。 */
  teamReply?: TeamReplyMeta | null;
}

export function PinStream({
  sessionId,
  onStatus,
}: {
  sessionId: string;
  /** Aggregated activity for the pill's status dot (green/orange/grey). */
  onStatus?: (s: PinStreamStatus) => void;
}) {
  // 悬浮窗会话视图的 UI 文案 catalog（i18n Wave2）
  const t = useTranslations("pin.stream");
  const evText = useEventI18nText();
  // E3 压缩进行中（context.compacting）——live 气泡空块时 spinner 旁的
  // 「压缩中…」文案开关。
  const [compacting, setCompacting] = useState(false);
  // P2-24：follow 团队广场时的团队上卷态。广场主会话自己不跑 turn（消息都
  // 路由给成员），成员活动经 team.changed 的 rollup 聚合到这里驱动徽标。
  const [teamRollup, setTeamRollup] = useState<{ working: number; blocked: number } | null>(null);
  const [input, setInput] = useState("");
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

  // The turn the composer is writing into; "" lets the server use its own
  // running-turn id (SteerItem.turnId contract).
  const turnIdRef = useRef("");
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const hintTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  // Last user text, kept for the error card's retry button.
  const lastUserTextRef = useRef<string | null>(null);
  // Composer ↑/↓ input history. PinApp keys PinStream by session id, so this
  // single instance is per-session by remount; the seed comes from the loaded
  // user messages (see histFor).
  const histRef = useRef<HistoryState>(initHistory());

  // ── 消息环 + socket 生命周期（批次 3-4：本地 connect/handle 骨架绞杀，
  //    三处 #240 语义经 pinStreamCore 保留，见 usePinSessionLoop 头注释）──
  const loop = usePinSessionLoop<PinMsg>({
    sessionId,
    midPrefix: "pm",
    mapHistory: (res, mid) =>
      mapHistoryToMsgs<PinMsg>(res, mid, {
        // 事件契约翻译器（useEventI18nText）：last_error 带 key 时落地翻译，
        // 与 ChatStream 的 mapHistory 同语义。缺省时英文 message 原样直显。
        errorText: (le, fallback) => evText(le, fallback),
        withRetryText: true,
        // P2-24：历史里的成员回帖（messages_ui 序列化 teamReply）——重载 /
        // 重连 reconcile 后回帖卡不丢，与实时 team.reply 帧同渲染。
        decorate: (raw, msg) => ({
          ...msg,
          teamReply: (raw as { teamReply?: TeamReplyMeta | null }).teamReply ?? null,
        }),
      }),
    mergeWithLive: mergeHistoryWithLive,
    // 「压缩中…」瞬态：与主窗口同一契约——置位事件之外的任何事件都意味着
    // 压缩已结束（压缩期间没有别的流事件）。流帧在公共 case 消化，故走
    // onFrame（每帧钩子）而不是 onExtraEvent。
    onFrame: (ev) => {
      if (ev.event !== "context.compacting") setCompacting(false);
    },
    texts: {
      interruptedTool: () => t("interrupted"),
      unknownError: () => t("unknownError"),
      compacted: (count) => t("compacted", { count }),
    },
    buildErrorMsg: (text, api) => ({
      id: api.mid(),
      role: "assistant",
      error: true,
      blocks: [{ kind: "text", text }],
      retryText: lastUserTextRef.current ?? undefined,
    }),
    onTurnStart: (ev, liveId, api) => {
      // The server's turn id: a queued steer rides this turn (the pin keeps
      // no per-session turn map — one session per mount).
      const srvTurn = ev.turn_id as string | undefined;
      if (srvTurn) {
        turnIdRef.current = srvTurn;
        api.setMessages((prev) =>
          prev.map((m) => (m.id === liveId ? { ...m, turnId: srvTurn } : m)),
        );
      }
    },
    onTurnEnd: () => {
      // No live turn → the next steer has no turn to ride (mirrors
      // ChatStream's liveTurnIdFor returning "").
      turnIdRef.current = "";
      // Still-queued entries were never absorbed: the oldest becomes the next
      // turn (design §3.3), the rest follow on that turn's end.
      flushSteerQueue();
    },
    onTurnStopped: () => {
      // ⏹ means stop: hand anything queued for absorption back to the
      // composer — text AND attachments — instead of discarding it. Done in
      // the event (like ChatStream) so a stop issued from the main window
      // recalls here too.
      turnIdRef.current = "";
      recallSteers();
    },
    onSocketReopen: () => {
      // Re-send what is still queued (brief §3.1): a reconnect is exactly when
      // an entry stashed into a dropped socket would otherwise be lost. The
      // pin's own sendFrame (not the socket-layer silent one) keeps the
      // "dead socket is reported" contract.
      steerRef.current.onResume(sessionId, sendFrame);
    },
    // Pin 特有事件（steer / context.* / history.cleared / team.*）。
    onExtraEvent: handleExtraEvent,
  });
  const {
    messages,
    setMessages: loopSetMessages,
    liveId,
    busy,
    permission,
    sendError,
    setSendError,
    wsStatus,
    sendFrame: rawSendFrame,
    postTurn,
    stopTurn,
    respond: loopRespond,
    mid: loopMid,
  } = loop;

  // Tool display labels come from settings (module-cached — see toolLabels).
  // 语言加载由根部 I18nProvider 负责（原 loadPromptLang 已移除）。
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

  // P2-24：徽标语义与主窗口团队上卷一致——blocked（等我代答）→ permission
  // （黄），working → busy（脉冲）。team.changed 只广播在团队主会话 socket，
  // 成员会话仍由 turn 事件驱动，两路互不覆盖。
  const teamBlocked = (teamRollup?.blocked ?? 0) > 0;
  const teamWorking = (teamRollup?.working ?? 0) > 0;
  const status: PinStreamStatus =
    permission || teamBlocked ? "permission" : busy || liveId || teamWorking ? "busy" : "idle";
  useEffect(() => {
    onStatus?.(status);
  }, [status, onStatus]);

  /** Raw-frame sender handed to the shared queue — the pin's own socket
   *  (brief §3.1: `send` is per-window). A dead socket is reported, not
   *  swallowed: the entry stays queued and rides the next turn / reconnect. */
  function sendFrame(frame: unknown) {
    if (!rawSendFrame(frame)) setSendError(t("connNotReady"));
  }

  /** Park an absorbed steer in the live bubble as a `steer` band. The hook
   *  drops the entry from the queue when it sees `steer.absorbed`, so the entry
   *  is captured BEFORE delegating; the band itself is display, which brief
   *  §3.1 leaves to the caller (blocks.tsx already renders `kind: "steer"`). */
  function appendSteerBand(entry: SteerItem, ev: { [k: string]: unknown }, api: PinTurnApi<PinMsg>) {
    const id = api.ensureLive();
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
    api.setMessages((prev) =>
      prev.map((m) => (m.id === id ? { ...m, blocks: [...m.blocks, band] } : m)),
    );
  }

  /** Pin 特有事件的分发（消息环公共 case 之外的 default 分支）。
   *  Every frame is fed to the shared queue: it owns the steer acks (and only
   *  those two events — it returns false for the rest). The pin deliberately
   *  does NOT early-return on its `true` ("this event is the queue's"): the
   *  hook only ever touches its own queue state, never this component's
   *  messages, so both can observe the frame. */
  function handleExtraEvent(ev: { event: string; [k: string]: unknown }, api: PinTurnApi<PinMsg>) {
    // steer.absorbed: the hook drops the entry, so capture it first — the band
    // is display, which brief §3.1 leaves to the caller.
    const absorbedEntry =
      ev.event === "steer.absorbed" && ev.steer_id
        ? steerRef.current.itemsFor(sessionId).find((i) => i.steerId === ev.steer_id)
        : undefined;
    steerRef.current.handleEvent(ev, sessionId);
    if (absorbedEntry) appendSteerBand(absorbedEntry, ev, api);

    switch (ev.event) {
      case "context.updated": {
        const changes = ((ev.changes as Array<{ section?: string; summary?: string }>) || []).filter(
          Boolean,
        );
        const visible = changes.filter((c) => c.section !== "environment");
        for (const c of visible) api.addSystemRow(c.summary || "");
        break;
      }
      case "context.microcompacted":
        api.addSystemRow(t("clearedToolOutputs", { count: Number(ev.cleared_tool_outputs ?? 0) }));
        break;
      case "context.compacting":
        // E3 总结 LLM 调用前的瞬态状态：live 气泡空块时的 spinner 旁显示
        // 「压缩中…」（见下方 streaming && blocks.length===0 渲染分支）。
        setCompacting(true);
        break;
      case "history.cleared":
        // /clear 清空了 checkpoint —— 本地 transcript 整体丢弃，live 气泡
        // 状态复位（紧随其后的收据 notice 会经 ensureLive 新开气泡）。
        turnIdRef.current = "";
        api.closeLive(false);
        api.setMessages([]);
        break;
      // ─── P2-24：AgentTeam 联动（backlog：pin 浮窗此前 team 事件零处理）───
      // team.changed / team.reply 只广播在团队主会话（广场）socket；follow
      // 成员会话时收不到，成员状态仍由 turn 事件驱动。
      case "team.changed": {
        const team = ev.team as
          | { main_session_id?: string; rollup?: { working?: number; blocked?: number } }
          | undefined;
        // 归属守卫：只有本 socket 正是该团队的主会话才采纳 rollup。
        if (team && team.main_session_id === sessionId) {
          setTeamRollup({
            working: Number(team.rollup?.working ?? 0),
            blocked: Number(team.rollup?.blocked ?? 0),
          });
        }
        break;
      }
      case "team.reply": {
        // 成员回帖：与 handle.ts 同契约——服务端 message_id 作消息 id，
        // 实时追加与 history reload 指向同一条，不会双渲染。
        const msgId = (ev.message_id as string) || api.mid();
        const text = (ev.text as string) || "";
        api.setMessages((prev) => {
          if (prev.some((m) => m.id === msgId)) return prev;
          return [
            ...prev,
            {
              id: msgId,
              role: "assistant" as const,
              blocks: [{ kind: "text", text } as Block],
              teamReply: {
                team_id: (ev.team_id as string) || "",
                instance_id: (ev.instance_id as string) || "",
                member_session_id: (ev.member_session_id as string) || "",
                display_name: (ev.display_name as string) || "",
                truncated: Boolean(ev.truncated),
              },
            },
          ];
        });
        break;
      }
      case "team.delivered": {
        // 广场 @ 投递回执（pin 里也能发 @ 投递）。runtime 已补 message.end
        // （P0-1），这里防御性收口 live 气泡 + 三态回执可视化：failed 是
        // 原子拒绝（一个字都没发），必须可见，否则 pin 用户以为发出去了。
        const receipt = (ev.receipt as string) || "delivered";
        api.closeLive(false);
        api.setPermission(null);
        const name = (ev.display_name as string) || (ev.agent_id as string) || "";
        if (receipt === "failed") {
          api.addSystemRow(
            t("deliveryFailed", {
              name,
              reason: (ev.error as string) || (ev.reason as string) || "",
            }),
          );
        } else if (receipt === "stalled") {
          api.addSystemRow(t("deliveryStalled", { name }));
        } else {
          api.addSystemRow(t("deliveryOk", { name }));
        }
        break;
      }
      default:
        // usage / goal.* / run.* / preview.* / todos.changed / … — ignored by
        // design (floating-window-design.md §2.4).
        break;
    }
  }

  // hint timer 的卸载清理（原挂在 socket effect cleanup，socket 生命周期
  // 已绞杀进共享层，这里独立成 effect）。
  useEffect(
    () => () => {
      if (hintTimerRef.current) clearTimeout(hintTimerRef.current);
    },
    [],
  );

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
        showHint(t("imageReadFailed"));
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
        showHint(t("uploadFailed", { name: f.name }));
      }
    }
  }

  function sendText(raw: string) {
    const text = raw.trim();
    const readyFiles = files.filter((f) => !f.uploading);
    // ── in-flight gate (brief §4.3). Both halves used to be a silent return in
    // the main window and read as a dead send button; say why instead. ──
    if (imagesReading > 0) {
      showHint(t("imagesProcessingHint"));
      return;
    }
    if (readyFiles.length !== files.length) {
      showHint(t("filesUploadingHint"));
      return;
    }
    if (!text && images.length === 0 && readyFiles.length === 0) return;

    if (busy) {
      // A turn is running → this is a mid-turn injection, not a new turn: the
      // shared queue owns the frame, the acks and the re-sends. Text AND
      // attachments ride the steer (brief §4.4); an empty-text steer is legal.
      histRef.current = appendEntry(histFor(), text);
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

    postTurnFromComposer(text, images, readyFiles);
  }

  /** 乐观 user 气泡 blocks：file → image → text 的组合（与主 composer 同序）。 */
  function buildUserBlocks(text: string, imgs: ComposerImage[], readyFiles: ComposerFile[]): Block[] {
    return [
      ...readyFiles.map((f) => ({
        kind: "file" as const,
        fileId: f.id,
        name: f.name,
        path: f.path,
        fileKind: f.kind,
      })),
      ...imgs.map((a) => ({ kind: "image" as const, url: a.preview })),
      ...(text ? [{ kind: "text" as const, text }] : []),
    ];
  }

  /** Post one real turn. Called by the composer AND by the queue's flush (the
   *  turn ended with an entry still unacknowledged → the OLDEST becomes the
   *  next turn, design §3.3). Socket gate / optimistic bubbles / rollback live
   *  in the loop; this half owns the pin-specific payload assembly, the input
   *  history, and the composer restore on a send race.
   *
   *  `onFailRestore`: a flushed entry has ALREADY left the queue, so a dead
   *  socket must put it back into the composer or it is gone (idempotent —
   *  called from both the send-race path and the notReady path). */
  function postTurnFromComposer(
    text: string,
    imgs: ComposerImage[],
    readyFiles: ComposerFile[],
    onFailRestore?: () => void,
  ): boolean {
    return postTurn({
      text,
      userBlocks: buildUserBlocks(text, imgs, readyFiles),
      images: imgs.map((a) => ({ data: a.data, media_type: a.mediaType })),
      files: readyFiles.map((f) => ({ id: f.id, name: f.name, path: f.path })),
      notReadyLabel: () => t("connNotReady"),
      failedLabel: () => t("sendFailed"),
      tooLongLabel: () => t("textTooLong"),
      // 记入输入历史(队列 flush 会二次 append,连续去重吸收)。notReady
      // 早退路径不记——内容还在输入框里。
      onSent: (turnId) => {
        // 乐观 turn id：turn.start 到达前 enqueue 的 steer 用它指向在途
        // turn（原版 postTurn 的 turnIdRef.current = turnId）。
        turnIdRef.current = turnId;
        histRef.current = appendEntry(histFor(), text);
        lastUserTextRef.current = text;
        setInput("");
        setImages([]);
        setFiles([]);
        requestAnimationFrame(() => {
          if (textareaRef.current) textareaRef.current.style.height = "auto";
        });
      },
      // Socket flipped to CLOSING between the check and the send: the loop
      // rolled the optimistic bubbles back — hand text and attachments back,
      // a failed send must not eat them.
      onFailed: (failedText) => {
        if (onFailRestore) {
          onFailRestore();
          return;
        }
        if (failedText) setInput((cur) => (cur ? `${failedText}\n${cur}` : failedText));
        if (imgs.length) setImages((a) => [...imgs, ...a]);
        if (readyFiles.length) setFiles((a) => [...readyFiles, ...a]);
      },
    });
  }

  /** The turn ended (or died) with entries still unacknowledged: promote the
   *  OLDEST to the next turn — Claude Code's rule verbatim, same as ChatStream
   *  (design §3.3). The hook pops the head; SENDING it is this half.
   *  Attachments ride it, or they would be lost. */
  function flushSteerQueue() {
    const head = steerRef.current.takeOldest(sessionId);
    if (!head) return;
    let restored = false;
    const restore = () => {
      if (restored) return;
      restored = true;
      if (head.text) setInput((cur) => (cur ? `${head.text}\n${cur}` : head.text));
      if (head.images.length) setImages((a) => [...head.images, ...a]);
      if (head.files.length) setFiles((a) => [...head.files, ...a]);
    };
    // notReady（socket 未就绪）也恢复——entry 已出队，不恢复就静默丢失
    // （composer 直发无需：onSent 未执行，内容仍在输入框）。
    if (!postTurnFromComposer(head.text, head.images, head.files, restore)) restore();
  }

  /** 懒构建历史种子:entries 仍为空时从已加载的 user 消息现拼(text/skill 块,
   *  注入/纯附件消息在此天然滤空)。一旦有任何条目(种子或本地发送)就不再
   *  重建,后续只靠发送时 appendEntry 增量。 */
  function histFor(): HistoryState {
    if (histRef.current.entries.length === 0) {
      histRef.current = initHistory(
        seedEntries(
          messages
            .filter((m) => m.role === "user")
            .map((m) => userTextFromBlocks(m.blocks)),
        ),
      );
    }
    return histRef.current;
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    // IME guard 补全 keyCode 229(对齐 ChatStream:只查 isComposing 会漏掉
    // 部分 CJK 输入法的候选确认)。
    const composing = e.nativeEvent.isComposing || e.keyCode === 229;
    if (e.key === "Enter" && !e.shiftKey && !e.metaKey && !e.ctrlKey && !composing) {
      e.preventDefault();
      sendText(input);
    }
    // ↑/↓ input history(与主 composer 同款;这里无命令菜单、无 steer-↑召回,
    // 只有 IME guard 在前):光标在首行 ↑ / 末行 ↓ 才触发,多行编辑保持原生
    // 光标移动。无内容可导航时放行原生行为。
    if (!composing && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
      const el = textareaRef.current;
      const caret = el?.selectionStart ?? input.length;
      const r =
        e.key === "ArrowUp"
          ? caretOnFirstLine(input, caret)
            ? navUp(histFor(), input)
            : null
          : caretOnLastLine(input, caret)
            ? navDown(histFor(), input)
            : null;
      if (r) {
        e.preventDefault();
        histRef.current = r.state;
        setInput(r.text);
        requestAnimationFrame(() => {
          const el2 = textareaRef.current;
          if (el2) {
            el2.focus();
            el2.setSelectionRange(r.text.length, r.text.length);
            // auto-grow 只挂在 onInput(用户键入)上,程序化回填不会触发,
            // 召回多行长文本时必须手动重算高度,否则显示截断。
            el2.style.height = "auto";
            el2.style.height = `${Math.min(el2.scrollHeight, 112)}px`;
          }
        });
      }
    }
    // Esc / ⌘↵ bubble up to PinApp's window-level handlers (collapse / open main).
  }

  function onInput(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setInput(e.target.value);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 112)}px`;
  }

  /** P2-24：回帖卡的「打开主窗口」——绝不把 pin webview 导航去 /team
   *  （主窗口 TeamReplyCard 的 router.push 会把整个团队空间塞进 360×520
   *  浮窗）。只召唤主窗口：follow 镜像团队会话时用户通常已在 /team 页。 */
  function openMainForTeam() {
    if (isDesktop()) {
      invoke("pin_open_main", { sessionId: null }).catch(() => {
        /* older shell without pin commands — no-op */
      });
    } else {
      window.location.href = "/";
    }
  }

  function respond(decision: "allow" | "deny") {
    // Anything still queued belongs to this turn: re-send it as `steer` BEFORE
    // the resume, so the resumed segment absorbs it (same rule as ChatStream's
    // respond — idempotent by steer_id server-side). The pin has no
    // change-of-mind/deny-and-steer path; only the re-send rule is mirrored.
    steerRef.current.onResume(sessionId, sendFrame);
    loopRespond(decision);
  }

  const connecting = wsStatus !== "live";
  // A text-less send is legal when an attachment rides along (brief §4.3).
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
                  {t("replyFailed")}
                </div>
                <div className="selectable whitespace-pre-wrap break-words text-xs leading-relaxed text-muted">
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
                    {t("retry")}
                  </button>
                )}
              </div>
            );
          }
          // P2-24：团队成员回帖卡（实时 team.reply / history teamReply 同渲染）。
          // 不复用主窗口 TeamReplyCard：它的「进入工作台」会 router.push("/team")，
          // 在 pin webview 里是错误落点；这里用紧凑归属徽标 + 召唤主窗口按钮。
          if (m.teamReply) {
            return (
              <div key={m.id} className="max-w-[94%] rounded-2xl rounded-bl-md border border-line bg-card px-3 py-2 text-[13px] leading-relaxed text-txt">
                <div className="mb-1 flex flex-wrap items-center gap-1.5">
                  <span className="inline-flex items-center gap-1 rounded-full border border-violet/40 bg-violet/10 px-1.5 py-px text-[10px] font-medium text-violet">
                    👥 {m.teamReply.display_name || t("teamMemberFallback")}
                  </span>
                  <span className="rounded-full border border-line px-1 py-px text-[9px] text-faint">
                    {t("teamReplyBadge")}
                  </span>
                  {m.teamReply.truncated && (
                    <span className="text-[9px] text-faint">{t("teamReplyTruncated")}</span>
                  )}
                </div>
                <div className="pin-compact">
                  <InnerBlocks blocks={m.blocks} streaming={false} />
                </div>
                <button
                  type="button"
                  onClick={openMainForTeam}
                  className="mt-1.5 flex items-center gap-1 rounded-lg border border-line2 px-2 py-0.5 text-[10px] text-muted transition-colors hover:border-violet/50 hover:text-violet"
                  title={t("teamOpenMainTitle")}
                >
                  <ExternalLink className="h-3 w-3" />
                  {t("teamOpenMain")}
                </button>
              </div>
            );
          }
          const streaming = m.id === liveId;
          return (
            <div key={m.id} className="max-w-[94%] rounded-2xl rounded-bl-md border border-line bg-card px-3 py-2 text-[13px] leading-relaxed text-txt">
              <div className="pin-compact">
                <InnerBlocks blocks={m.blocks} streaming={streaming} offline={wsStatus !== "live"} />
              </div>
              {streaming && m.blocks.length === 0 && (
                <span className="flex items-center gap-1.5 text-faint">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  {compacting && t("compacting")}
                </span>
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
            {t("permissionRequired")} · <code className="font-mono text-txt">{toolLabel(permission.tool)}</code>
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
              {t("allowOnce")}
            </button>
            <button
              type="button"
              onClick={() => respond("deny")}
              className="rounded-lg border border-line2 px-2.5 py-1 text-xs text-muted transition-colors hover:text-txt"
            >
              {t("deny")}
            </button>
          </div>
        </div>
      )}

      {/* connection / send hint strip */}
      {(connecting || sendError || hint) && (
        <div className="flex items-center gap-1.5 px-3 pb-1 text-[11px] text-faint">
          {connecting && <Loader2 className="h-3 w-3 animate-spin" />}
          {sendError ?? hint ?? (wsStatus === "reconnecting" ? t("reconnecting") : t("connecting"))}
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
                  alt={a.name ?? t("imageAlt")}
                  title={a.name ?? t("imageAlt")}
                  className="h-10 w-10 rounded-lg border border-line object-cover"
                />
                <button
                  type="button"
                  onClick={() => setImages((l) => l.filter((_, j) => j !== i))}
                  aria-label={t("removeImage", { name: a.name ?? t("imageAlt") })}
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
                {f.uploading && <span className="text-faint">{t("uploading")}</span>}
                <button
                  type="button"
                  onClick={() => setFiles((l) => l.filter((_, j) => j !== i))}
                  aria-label={t("removeFile", { name: f.name })}
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
            {t("processingImages")}
          </div>
        )}
        {steerItems.length > 0 && (
          // Compact queue badge — the main window's full queue bar is too wide
          // for the pin (brief §4.4). Details live in the tooltip; ⏹ takes them
          // back.
          <div
            className="mb-1.5 flex items-center gap-1.5 px-0.5 text-[11px] text-muted"
            title={`${t("pendingTooltip", { count: steerItems.length })}\n${steerItems
              .map((it) => it.text || t("attachmentsOnly"))
              .join("\n")}`}
          >
            {steerItems.some((it) => it.status === "sending") ? (
              <Loader2 className="h-3 w-3 shrink-0 animate-spin text-faint" />
            ) : (
              <span className="shrink-0">⏳</span>
            )}
            <span className="truncate">{t("pendingCount", { count: steerItems.length })}</span>
            <span className="shrink-0 text-faint">{t("stopToRecall")}</span>
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
            placeholder={t("placeholder")}
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
            title={t("addAttachmentTitle")}
            aria-label={t("addAttachment")}
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
            title={busy ? t("injectTitle") : t("sendTitle")}
            className="mb-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-violet text-white transition-opacity hover:opacity-90 disabled:opacity-30"
          >
            <ArrowUp className="h-3.5 w-3.5" />
          </button>
          {busy && (
            <button
              type="button"
              onClick={stopTurn}
              title={t("stopTitle")}
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
