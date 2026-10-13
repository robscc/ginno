"use client";

// useChatStreamEngine — ChatStream 的运行时引擎（自 ChatStream.tsx 机械拆出，
// 行为零变化）：per-session 持久 store/socket 的全部 ref、WS 生命周期
// （连接/重连/ping/静默关闭）、session 切换 effect、服务端事件 reducer
// （handle）、steer 队列管线，以及权限/提案/问答/重试等 socket 动作。
//
// 纯结构搬移：函数体与原文件逐字一致。原组件作用域里的状态 setter、共享
// ref 与提升声明的函数（attemptSend/pinToBottom/uploadOneDoc/attachOne/
// recomputeMenu/finishSynthesisWait）改为经 deps 解构注入——名字不变，
// 闭包语义与原来逐 render 重建一致。syncDisplay 所镜像的 React 状态仍
// 由组件持有，这里只负责写入。
//
// 内聚主题拆到 engine/ 子模块（ctx 全量注入，主文件每 render 组装一次）：
// engine/handle（事件 reducer）、engine/connect（socket 就绪/历史对账）、
// engine/steer（steer 队列管线）、engine/actions（权限/重试等 socket 动作）。
// 本文件保留 ref 表、syncDisplay、WS 连接生命周期与 session 切换编排。

import { useEffect, useRef } from "react";
import { useTranslations } from "next-intl";
import { openSessionSocket, getSessionHistory } from "@/lib/runtime";
import { useGinno } from "@/lib/store";
import { useSteerQueue } from "@/lib/steerQueue";
import { pruneMentions, type ResolvedMention } from "@/components/chat/commandMenu";
import { useEventI18nText, type Block } from "@/components/chat/blocks";
import { registerModPanePressSender } from "@/components/chat/mod/paneBus";
import type { ModBandGroup } from "@/components/chat/mod/ModBand";
import type { ModAskState } from "@/components/chat/mod/ModAskCard";
import type {
  SessionMeta,
  SessionUsage,
  WorkflowRun,
} from "@/lib/types";
import {
  type Attachment,
  type ChatMsg,
  type FileAttachment,
  type PermissionPrompt,
  type SendPayload,
  type VersionPropose,
} from "./streamCore";
import {
  MenuState,
  ProposeResult,
  WsStatus,
  type EngineCtx,
} from "./engine/ctx";
import {
  armSocketReady as armSocketReadyImpl,
  waitForSocketOpen as waitForSocketOpenImpl,
  abandonLiveTurn as abandonLiveTurnImpl,
  mapHistory as mapHistoryImpl,
  reconcileTurnFromHistory as reconcileTurnFromHistoryImpl,
} from "./engine/connect";
import {
  dropAbsorbedSteers as dropAbsorbedSteersImpl,
  dropSteer as dropSteerImpl,
  enqueueSteer as enqueueSteerImpl,
  flushSteerQueue as flushSteerQueueImpl,
  liveTurnIdFor as liveTurnIdForImpl,
  recallSteers as recallSteersImpl,
  requeueSteersOnResume as requeueSteersOnResumeImpl,
  steerFrameSender as steerFrameSenderImpl,
} from "./engine/steer";
import {
  markDelivered as markDeliveredImpl,
  adoptOrphanStream as adoptOrphanStreamImpl,
  ensureLive as ensureLiveImpl,
  mutateLive as mutateLiveImpl,
  handle as handleImpl,
} from "./engine/handle";
import {
  retryFailed as retryFailedImpl,
  editResend as editResendImpl,
  dismissFailed as dismissFailedImpl,
  retryError as retryErrorImpl,
  retryFromCheckpoint as retryFromCheckpointImpl,
  respond as respondImpl,
  stopTurn as stopTurnImpl,
  respondPropose as respondProposeImpl,
  answerQuestion as answerQuestionImpl,
  decideSubagentPlan as decideSubagentPlanImpl,
  sendModUiPress as sendModUiPressImpl,
  sendModUiAnswer as sendModUiAnswerImpl,
} from "./engine/actions";

export interface EngineDeps {
  g: ReturnType<typeof useGinno>;
  steerQ: ReturnType<typeof useSteerQueue>;
  session: SessionMeta | null;
  onUsageChange?: (u: SessionUsage) => void;
  propose: VersionPropose | null;
  input: string;
  attachments: Attachment[];
  fileAttachments: FileAttachment[];
  setMessages: (v: ChatMsg[] | ((p: ChatMsg[]) => ChatMsg[])) => void;
  setRuns: (v: WorkflowRun[] | ((p: WorkflowRun[]) => WorkflowRun[])) => void;
  setLiveId: (v: string | null | ((p: string | null) => string | null)) => void;
  setWsStatus: (v: WsStatus | ((p: WsStatus) => WsStatus)) => void;
  setPermission: (v: PermissionPrompt | null | ((p: PermissionPrompt | null) => PermissionPrompt | null)) => void;
  setPropose: (v: VersionPropose | null | ((p: VersionPropose | null) => VersionPropose | null)) => void;
  setStreamAgent: (v: string | null | ((p: string | null) => string | null)) => void;
  setServerRunning: (v: boolean | ((p: boolean) => boolean)) => void;
  // context.compacting 状态镜像（E3 压缩中的「压缩中…」提示）：set 仅写
  // 当前显示会话的布尔值，真源是 compactingBySessionRef。
  setCompacting: (v: boolean | ((p: boolean) => boolean)) => void;
  setInput: (v: string | ((p: string) => string)) => void;
  setAttachments: (v: Attachment[] | ((p: Attachment[]) => Attachment[])) => void;
  setTarget: (v: string | null | ((p: string | null) => string | null)) => void;
  setMenu: (v: MenuState | null | ((p: MenuState | null) => MenuState | null)) => void;
  setFileAttachments: (v: FileAttachment[] | ((p: FileAttachment[]) => FileAttachment[])) => void;
  setComposerHint: (v: string | null | ((p: string | null) => string | null)) => void;
  setProposeResult: (
    v: ProposeResult | null | ((p: ProposeResult | null) => ProposeResult | null),
  ) => void;
  // Claude Code Mods band 插槽(claude-code-mods-design.md §7.1):按 sid 存
  // 的 band 分段数组(每 mod 一段,各有 generation),渲染归 ChatStream
  // (composer 内),store 不参与。
  setModBands: (
    v:
      | Record<string, ModBandGroup[]>
      | ((p: Record<string, ModBandGroup[]>) => Record<string, ModBandGroup[]>),
  ) => void;
  // $.ui.ask(claude-code-mods-design.md §7.3):按 sid 存的待答问题(同一会话
  // 同时最多展示最新一条),渲染归 ChatStream 浮层 ModAskCard。
  setModAsks: (
    v:
      | Record<string, ModAskState | null>
      | ((p: Record<string, ModAskState | null>) => Record<string, ModAskState | null>),
  ) => void;
  // Refs owned by the component (shared with composer / scroll code there).
  stickRef: { current: boolean };
  connectRef: { current: () => void };
  focusLatestRef: { current: string | null };
  textareaRef: { current: HTMLTextAreaElement | null };
  sumPendingRef: { current: string | null };
  // Hoisted function declarations from the component body (safe to pass:
  // function declarations are initialized before the hook call runs).
  pinToBottom: (frames?: number) => void;
  uploadOneDoc: (sid: string, f: File, tmpId: string) => Promise<FileAttachment | null>;
  attachOne: (sid: string, p: string, tmpId: string) => Promise<FileAttachment | null>;
  attemptSend: (sid: string, payload: SendPayload, userMsgId?: string) => void;
  recomputeMenu: (text: string) => void;
  finishSynthesisWait: (id: string) => Promise<void>;
}

// Draft slot id for the session-less landing view (ChatStream 共用：上传完成的
// 文件回执要按 slot 归还，不能串会话）。
export const HOME_SLOT = "__home__";

export function useChatStreamEngine(deps: EngineDeps) {
  const {
    g, steerQ, session, onUsageChange, propose,
    input, attachments, fileAttachments,
    setMessages, setRuns, setLiveId, setWsStatus, setPermission, setPropose,
    setStreamAgent, setServerRunning, setCompacting, setInput, setAttachments, setTarget, setMenu,
    setFileAttachments, setComposerHint, setProposeResult, setModBands, setModAsks,
    stickRef, connectRef, focusLatestRef, textareaRef, sumPendingRef,
    pinToBottom, uploadOneDoc, attachOne, attemptSend, recomputeMenu, finishSynthesisWait,
  } = deps;

  // ---- i18n（chat 域 + 事件契约）----
  // 本 hook 处理 runtime 事件文本的落地：error / notice 等事件可带 i18n_key+params
  // （i18n-design.md §3 契约），落地时翻译，未命中回退原文。socket 回调闭包的
  // 生命周期长于单次 render，翻译句柄经 ref 镜像保持最新。
  const tc = useTranslations("chat");
  const tr = tc as unknown as {
    (key: string, values?: Record<string, string | number>): string;
    has(key: string): boolean;
  };
  const evText = useEventI18nText();
  const i18nRef = useRef({ tr, evText });
  i18nRef.current = { tr, evText };

  // liveIdRef mirrors liveId state for use inside callbacks without closure staleness
  const liveIdRef = useRef<string | null>(null);
  // Socket callbacks outlive session switches (per-session sockets stay open),
  // so they capture stale context — anything they need live must come from refs.
  const activeSidRef = useRef<string | null>(g.activeSessionId);
  activeSidRef.current = g.activeSessionId;
  // socket-ready promises per sid (lazy-creation send awaits the socket).
  const socketReadyRef = useRef<
    Record<string, { promise: Promise<void>; resolve: () => void; reject: (e: unknown) => void }>
  >({});
  // Draft-slot tracking incl. the landing home ("__home__"); see switch effect.
  const prevSlotRef = useRef<string | null>(null);
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
  // (The per-session steer queue ref now lives inside useSteerQueue.)
  // Auto-clear timer for the composer hint (one timer, hint is composer-global).
  const hintTimerRef     = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Per-session mirror of the `serverRunning` state (see syncDisplay).
  const serverRunningRef = useRef<Record<string, boolean>>({});
  // E3 压缩进行中（context.compacting 已到、压缩结果/后续流事件未到）。
  // 置位只在总结 LLM 调用前发生,清除由「该会话任何下一个事件」兜底——
  // 压缩期间没有别的流事件,首个到达的事件必属压缩之后。
  const compactingBySessionRef = useRef<Record<string, boolean>>({});
  const streamAgentRef   = useRef<Record<string, string | null>>({});
  // Orphan-stream tracking: this instance adopted an ALREADY-RUNNING turn
  // (remount mid-turn — the user navigated to another page while a reply was
  // streaming). Its turn.start is never seen, so the live bubble ensureLive
  // creates would render as a SECOND section next to the history-rendered
  // partial bubble; message.end heals the split by reconciling from history.
  const orphanStreamRef  = useRef<Record<string, boolean>>({});
  // 孤儿流是否已认领服务端 turn id（见 mutateLive 的认领逻辑）
  const adoptedTurnRef   = useRef<Record<string, boolean>>({});
  // auto-retry 瞬态提示（stream.auto_retry notice）当前落在 live 气泡里的
  // 精确文本块（含分隔符）。新一轮重试到来时原位替换（不 append 堆叠）；
  // 恢复出流（token/thinking delta）即撤行。见 notice case。
  const retryNoticeRef   = useRef<Record<string, string | null>>({});
  const seenTurnStartRef = useRef<Record<string, boolean>>({});
  const pingTimerRef     = useRef<Record<string, ReturnType<typeof setInterval> | null>>({});
  const watchTimerRef    = useRef<Record<string, ReturnType<typeof setInterval> | null>>({});
  // Post-reconnect turn_state fallback: if the server never answers (older
  // runtime), the in-flight turn is abandoned after a grace period.
  const reconcileTimerRef = useRef<Record<string, ReturnType<typeof setTimeout> | null>>({});
  const reconnTimerRef   = useRef<Record<string, ReturnType<typeof setTimeout> | null>>({});
  const lastSeenRef      = useRef<Record<string, number>>({});
  // Who spoke last on this session's socket — our `turn_state` probe or our
  // invoke? A turn.state answer describes the session as of the moment the
  // SERVER handled the probe, so a send that went out after the probe cannot be
  // judged by it. Without this ordering, the first message of a brand-new
  // session (socket opened by the send itself: probe at t0, invoke at t0+13ms,
  // both before the turn registers server-side) is reconciled against a
  // /history that has no checkpoint yet and stamped 「未送达」 while it is in
  // fact running (2026-09-21, session 52d54d…). Monotonic counter, not
  // Date.now() — the two can land in the same millisecond.
  const ioSeqRef         = useRef(0);
  const probeSeqRef      = useRef<Record<string, number>>({});
  const sendSeqRef       = useRef<Record<string, number>>({});
  // Unsent input + attachments + resolved mentions saved per session on switch.
  // `files` joined the draft with steer-attachments v2: a recalled steer whose
  // session is not the displayed one parks its file chips here too, so switching
  // back restores them instead of dropping them on the floor.
  const draftCacheRef    = useRef<Record<string, { input: string; attachments: Attachment[]; files?: FileAttachment[]; mentions?: ResolvedMention[] }>>({});
  // Resolved @mentions picked from the autocomplete menu, keyed by session id.
  // Pruned on every input change (edited-away token → dropped mention) and
  // sent along with the invoke payload as the authoritative structured list.
  const mentionsRef      = useRef<Record<string, ResolvedMention[]>>({});
  // In-chat live workflow runs bound to each session (design A: run 回到对话)
  const runsBySessionRef = useRef<Record<string, WorkflowRun[]>>({});
  // Docs/native paths dropped while on the landing home (no session yet):
  // buffered with optimistic chips, uploaded right after lazy creation.
  const pendingDocsRef = useRef<Array<{ file: File; tmpId: string }>>([]);
  const pendingPathsRef = useRef<Array<{ path: string; tmpId: string }>>([]);

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
    setStreamAgent(streamAgentRef.current[sid] ?? null);
    setServerRunning(!!serverRunningRef.current[sid]);
    setCompacting(!!compactingBySessionRef.current[sid]);
    // The steer queue is no longer mirrored here — useSteerQueue re-renders the
    // component itself when the queue changes, and `steerItems` reads it live.
  };

  // 子模块共用的 ctx：每 render 组装一次，闭包语义与拆分前逐 render 重建一致。
  const ctx: EngineCtx = {
    g, steerQ, session, onUsageChange, propose,
    input, attachments, fileAttachments,
    setMessages, setRuns, setLiveId, setWsStatus, setPermission, setPropose,
    setStreamAgent, setServerRunning, setCompacting, setInput, setAttachments, setTarget, setMenu,
    setFileAttachments, setComposerHint, setProposeResult, setModBands, setModAsks,
    stickRef, connectRef, focusLatestRef, textareaRef, sumPendingRef,
    pinToBottom, uploadOneDoc, attachOne, attemptSend, recomputeMenu, finishSynthesisWait,
    i18nRef,
    liveIdRef, activeSidRef, socketReadyRef, prevSlotRef, curSessionIdRef,
    storeRef, liveBySessionRef, socketsRef, statusRef, permsRef, proposeRef,
    busyBySessionRef, hintTimerRef, serverRunningRef, compactingBySessionRef, streamAgentRef,
    orphanStreamRef, adoptedTurnRef, retryNoticeRef, seenTurnStartRef, pingTimerRef, watchTimerRef,
    reconcileTimerRef, reconnTimerRef, lastSeenRef, ioSeqRef, probeSeqRef,
    sendSeqRef, draftCacheRef, mentionsRef, runsBySessionRef,
    pendingDocsRef, pendingPathsRef,
    syncDisplay,
  };

  // 运行中的委托回放页（P1 实时流前端侧）：后端 bg 每 3s 节流回填部分转录，
  // 这里同步轮询 history 替换消息——回放内容随委托进度"长"出来，终态即停。
  const delegationReplayLive =
    session?.type === "delegation" && session?.stop_reason === "running";
  useEffect(() => {
    if (!delegationReplayLive || !session?.id) return;
    let alive = true;
    const tick = async () => {
      try {
        const res = await getSessionHistory(session.id);
        if (!alive) return;
        const mapped = mapHistory(res ?? {});
        // 必须同步写 per-session store:只写显示层的话,任何 syncDisplay
        // (WS 状态变化/权限事件等)都会把视图弹回进入会话时的旧 store 快照
        // ——运行中表现为"时不时闪白";结束后轮询停止,重进会话又被
        // `if (!storeRef[sid])` 非空守卫挡住不再拉 history,视图冻结在旧
        // 内容上,只有重启(清空 store)才恢复。
        storeRef.current[session.id] = mapped;
        setMessages(mapped);
        pinToBottom();
      } catch {
        /* 轮询失败静默——下一轮再试 */
      }
    };
    void tick();
    const iv = setInterval(tick, 3000);
    return () => {
      alive = false;
      clearInterval(iv);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [delegationReplayLive, session?.id]);
  // 终态补一拍:轮询随 stop_reason 翻转立即停止,而终态 finalize(完整转录+
  // 结果注记)恰在最后一次轮询之后落盘——不补拉,回放会冻结在倒数第二个
  // 快照上;同时覆盖"结束后切走再切回"的入口(store 非空不再走初始加载,
  // 由这里刷新到最终内容)。
  useEffect(() => {
    if (delegationReplayLive || session?.type !== "delegation" || !session?.id)
      return;
    const sid = session.id;
    let alive = true;
    getSessionHistory(sid)
      .then((res) => {
        if (!alive) return;
        const mapped = mapHistory(res ?? {});
        storeRef.current[sid] = mapped;
        setMessages(mapped);
        pinToBottom();
      })
      .catch(() => {
        /* 补拍失败静默——终态内容不随轮询自愈,这里也别打扰 */
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [delegationReplayLive, session?.id, session?.type, session?.stop_reason]);

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
        delete busyBySessionRef.current[id]; delete streamAgentRef.current[id];
        steerQ.clear(id);
        delete serverRunningRef.current[id];
        delete compactingBySessionRef.current[id];
        delete draftCacheRef.current[id]; delete pingTimerRef.current[id];
        delete watchTimerRef.current[id]; delete reconnTimerRef.current[id];
        delete lastSeenRef.current[id];   delete reconcileTimerRef.current[id];
        delete probeSeqRef.current[id];   delete sendSeqRef.current[id];
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
      // ALWAYS probe, not only when this client believes a turn is in flight:
      // after a full page reload no ref remembers the turn, yet the server may
      // still be parked on an interrupt (ask_user). The answer is the
      // server-sourced liveness flag that keeps a history-rebuilt question
      // card interactive.
      try {
        probeSeqRef.current[sid] = ++ioSeqRef.current;
        sock.send(JSON.stringify({ type: "turn_state" }));
      } catch { /* ignore */ }
      if (liveBySessionRef.current[sid] || busyBySessionRef.current[sid]) {
        // A turn was in flight when this socket's predecessor dropped. Turn
        // events broadcast to EVERY socket of the session, so the running
        // stream resumes into the same live bubble automatically; if the
        // server says it's gone (finished while we were away, or the runtime
        // restarted), the answer's handler reconciles from history.
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
      try {
        const ev = JSON.parse(e.data) as { event: string; frame_session?: string };
        // Frame-ownership guard (2026-10-01 渲染串线报告）：runtime 在每帧带上
        // frame_session（独立键——subagent 事件的 session_id 表示事件主题的
        // 子会话，不能混用）；不属于本会话的帧一律丢弃并告警。
        if (ev.frame_session && sid && ev.frame_session !== sid) {
          console.warn(
            `[ginno] dropped cross-session frame event=${ev.event} frame_session=${ev.frame_session} socket_session=${sid}`,
          );
          try {
            sock.send(JSON.stringify({
              type: "client_diag",
              diag_kind: "frame_owner_mismatch",
              detail: { event: ev.event, frame_session: ev.frame_session, socket_session: sid },
            }));
          } catch { /* socket may be closing */ }
          return;
        }
        handle(sid, ev);
      } catch { /* ignore */ }
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

  // When the active session changes: save draft, restore draft, connect socket,
  // load history, and sync display state from refs → React state.
  useEffect(() => {
    const sid = session?.id ?? null;
    // Draft slots include the landing home so ⌘N → type → open session → ⌘N
    // round-trips keep the text.
    const prevSlot = prevSlotRef.current;
    const nextSlot = sid ?? HOME_SLOT;

    // Save outgoing draft (before any early return)
    if (prevSlot && prevSlot !== nextSlot) {
      draftCacheRef.current[prevSlot] = {
        input,
        attachments,
        files: fileAttachments,
        mentions: pruneMentions(
          mentionsRef.current[prevSlot === HOME_SLOT ? "" : prevSlot] ?? [],
          input,
        ),
      };
      setInput("");
      setAttachments([]);
      setFileAttachments([]); // file chips belong to their session's draft, never leak across
      setTarget(null);
      setMenu(null); // menu is composer-global state; never leak across sessions
      // Deferred home attachments follow the user into the session they land
      // in: start the uploads now so the chips flip to ready instead of
      // blocking sends forever (they can only flush into a real session).
      if (prevSlot === HOME_SLOT && sid) {
        const docs = pendingDocsRef.current;
        pendingDocsRef.current = [];
        const natives = pendingPathsRef.current;
        pendingPathsRef.current = [];
        // The chips ride along too: move them from the home draft into the
        // landing session's draft so the restore below shows them (upload
        // completions target sid and flip them ready there). pendingRefs are
        // the source of truth — the home-creating send (ChatStream) drains
        // them itself, so merging by those ids can't resurrect sent chips.
        const pendingIds = new Set([
          ...docs.map((d) => d.tmpId),
          ...natives.map((n) => n.tmpId),
        ]);
        const home = draftCacheRef.current[HOME_SLOT];
        if (home?.files?.length && pendingIds.size) {
          const moving = home.files.filter((f) => pendingIds.has(f.id));
          const d = draftCacheRef.current[sid] ?? (draftCacheRef.current[sid] = {
            input: "", attachments: [],
          });
          d.files = [...(d.files ?? []), ...moving];
          home.files = home.files.filter((f) => !pendingIds.has(f.id));
        }
        for (const d of docs) void uploadOneDoc(sid, d.file, d.tmpId);
        for (const n of natives) void attachOne(sid, n.path, n.tmpId);
      }
    }
    prevSlotRef.current = nextSlot;

    if (!session || !sid) {
      curSessionIdRef.current = null;
      const draft = draftCacheRef.current[HOME_SLOT];
      if (draft) {
        setInput(draft.input);
        setAttachments(draft.attachments);
        setFileAttachments(draft.files ?? []);
      }
      return;
    }

    curSessionIdRef.current = sid;
    // Entering a session always lands on the LATEST message. stickRef carries
    // the PREVIOUS session's read position (scrolled up while reading back =
    // false) — without this reset the [messages] auto-scroll skips the newly
    // loaded history and the transcript opens at the TOP (用户反馈 2026-09-26).
    stickRef.current = true;
    connectRef.current = () => connectSession(sid);

    connectSession(sid);

    // Load history if this session has no messages yet
    if (!storeRef.current[sid]) {
      storeRef.current[sid] = [];
      getSessionHistory(sid).then((res) => {
        if (!res?.messages?.length) return;
        const mapped = mapHistory(res);
        // A reloaded session rebuilds from the checkpoint: entries whose
        // steer_id is already in history were absorbed (their acks died with
        // the old page), so drop them rather than re-sending (design §3.3).
        dropAbsorbedSteers(sid, mapped);
        // A live event can land DURING the fetch (the parked-question re-emit
        // on socket open, or in-flight tokens after a quick reload): keep the
        // bubble ensureLive created instead of clobbering it — liveBySessionRef
        // still points at it, and dropping it would silently discard every
        // later event of the turn. The duplicate section is the known orphan
        // cosmetic; message.end reconciles it away.
        const lid = liveBySessionRef.current[sid];
        const liveMsg = lid ? (storeRef.current[sid] ?? []).find((m) => m.id === lid) : null;
        if (liveMsg) {
          // The parked-question re-emit on socket open can create a live
          // bubble for a turn history ALSO rebuilt (same question/tool-call
          // id in both). Keeping both showed the ask_user card TWICE for as
          // long as the turn stays parked — message.end never comes to
          // reconcile. Adopt history's message as the live target instead
          // (2026-09-25); genuinely in-flight turns (no matching question
          // id) keep their live bubble as before.
          const qId = (b: Block) => (b.kind === "question" ? b.id : undefined);
          const liveQIds = new Set(
            liveMsg.blocks.map(qId).filter((x): x is string => !!x)
          );
          const dupe = mapped.find(
            (m) =>
              m.id === liveMsg.id ||
              m.blocks.some((b) => qId(b) !== undefined && liveQIds.has(qId(b)!))
          );
          if (dupe) {
            liveBySessionRef.current[sid] = dupe.id;
            storeRef.current[sid] = mapped;
          } else {
            storeRef.current[sid] = [...mapped, liveMsg];
          }
        } else {
          storeRef.current[sid] = mapped;
        }
        syncDisplay(sid);
      });
    }

    // Load the session's goal snapshot for the TopBar chip (goal-design.md).
    // Live updates then arrive via goal.updated / goal.cleared WS events.
    g.loadGoal(sid);
    // 任务清单（plan-orchestration §2/P3）：同款——打开时 REST 拉取，之后
    // plan.updated WS 事件实时刷。
    g.loadPlans(sid);
    // AgentTeam：@ 投递菜单可见范围需要 teams（team.changed 事件持续回流）。
    void g.reloadTeams();

    // Restore draft if any (mentions re-pruned against the restored text so a
    // token the user deleted before switching stays deleted)
    const draft = draftCacheRef.current[sid];
    if (draft) {
      setInput(draft.input);
      setAttachments(draft.attachments);
      // File chips come back with the draft too — a recalled steer parked in
      // this session's draft must reappear, not vanish.
      setFileAttachments(draft.files ?? []);
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
      pinToBottom();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.id]);

  /** Transient, visible feedback in the composer (never a silent no-op). */
  function showComposerHint(msg: string) {
    setComposerHint(msg);
    if (hintTimerRef.current) clearTimeout(hintTimerRef.current);
    hintTimerRef.current = setTimeout(() => setComposerHint(null), 4000);
  }

  // Pane 的 press 回发沿本 hook 的 session socket(§7.4):注册进 paneBus,
  // 右栏 ModPaneHost 不需要自己的连接。挂载一次即可——sender 是稳定闭包,
  // 经 socketsRef 间接取当前 socket。
  useEffect(() => {
    registerModPanePressSender((sid, generation, actionId, value) => sendModUiPress(sid, generation, actionId, value));
  }, []);

  // ─── 子模块包装（engine/*）：名字与签名保持拆分前原样，实现见各子模块 ──────
  function mapHistory(res: Parameters<typeof mapHistoryImpl>[1]): ChatMsg[] {
    return mapHistoryImpl(ctx, res);
  }
  function armSocketReady(sid: string) {
    armSocketReadyImpl(ctx, sid);
  }
  function waitForSocketOpen(sid: string, timeoutMs?: number): Promise<boolean> {
    return waitForSocketOpenImpl(ctx, sid, timeoutMs);
  }
  function abandonLiveTurn(sid: string) {
    abandonLiveTurnImpl(ctx, sid);
  }
  function reconcileTurnFromHistory(sid: string) {
    reconcileTurnFromHistoryImpl(ctx, sid);
  }
  function markDelivered(sid: string) {
    markDeliveredImpl(ctx, sid);
  }
  function adoptOrphanStream(sid: string) {
    adoptOrphanStreamImpl(ctx, sid);
  }
  function ensureLive(sid: string, evTurn?: string): string {
    return ensureLiveImpl(ctx, sid, evTurn);
  }
  function mutateLive(sid: string, ev: { event: string; [k: string]: unknown }) {
    mutateLiveImpl(ctx, sid, ev);
  }
  function handle(sid: string, ev: { event: string; [k: string]: unknown }) {
    handleImpl(ctx, sid, ev);
  }
  function liveTurnIdFor(sid: string): string {
    return liveTurnIdForImpl(ctx, sid);
  }
  function dropSteer(sid: string, steerId: string) {
    dropSteerImpl(ctx, sid, steerId);
  }
  function enqueueSteer(
    sid: string,
    text: string,
    agentId: string | null,
    images: Attachment[] = [],
    files: FileAttachment[] = [],
  ) {
    enqueueSteerImpl(ctx, sid, text, agentId, images, files);
  }
  function steerFrameSender(sid: string): (frame: unknown) => void {
    return steerFrameSenderImpl(ctx, sid);
  }
  function requeueSteersOnResume(sid: string) {
    requeueSteersOnResumeImpl(ctx, sid);
  }
  function recallSteers(sid: string) {
    recallSteersImpl(ctx, sid);
  }
  function flushSteerQueue(sid: string) {
    flushSteerQueueImpl(ctx, sid);
  }
  function dropAbsorbedSteers(sid: string, mapped: ChatMsg[]) {
    dropAbsorbedSteersImpl(ctx, sid, mapped);
  }
  function retryFailed(msgId: string) {
    retryFailedImpl(ctx, msgId);
  }
  function editResend(msgId: string) {
    editResendImpl(ctx, msgId);
  }
  function dismissFailed(msgId: string) {
    dismissFailedImpl(ctx, msgId);
  }
  function retryError(msgId: string) {
    retryErrorImpl(ctx, msgId);
  }
  function retryFromCheckpoint(msgId: string) {
    retryFromCheckpointImpl(ctx, msgId);
  }
  function respond(decision: "allow" | "deny") {
    respondImpl(ctx, decision);
  }
  function stopTurn() {
    stopTurnImpl(ctx);
  }
  function respondPropose(decision: "allow" | "deny") {
    respondProposeImpl(ctx, decision);
  }
  function answerQuestion(id: string, answer: string, optionIndex: number | null, skip: boolean) {
    answerQuestionImpl(ctx, id, answer, optionIndex, skip);
  }
  function decideSubagentPlan(
    planId: string,
    decision: "confirm" | "cancel",
    subtasks?: import("@/lib/types").SubagentPlanSubtask[],
  ) {
    decideSubagentPlanImpl(ctx, planId, decision, subtasks);
  }
  function sendModUiPress(sid: string, generation: number, actionId: string, value?: string) {
    sendModUiPressImpl(ctx, sid, generation, actionId, value);
  }
  function sendModUiAnswer(sid: string, id: string, value: string) {
    sendModUiAnswerImpl(ctx, sid, id, value);
  }

  return {
    // refs
    liveIdRef, activeSidRef, socketReadyRef, prevSlotRef, curSessionIdRef,
    storeRef, liveBySessionRef, socketsRef, statusRef, permsRef, proposeRef,
    busyBySessionRef, hintTimerRef, serverRunningRef, compactingBySessionRef, streamAgentRef,
    orphanStreamRef, seenTurnStartRef, pingTimerRef, watchTimerRef,
    reconcileTimerRef, reconnTimerRef, lastSeenRef, ioSeqRef, probeSeqRef,
    sendSeqRef, draftCacheRef, mentionsRef, runsBySessionRef,
    pendingDocsRef, pendingPathsRef,
    // functions
    syncDisplay, armSocketReady, waitForSocketOpen, abandonLiveTurn, mapHistory,
    reconcileTurnFromHistory, connectSession, cycleSessionSocket, markDelivered,
    adoptOrphanStream, ensureLive, mutateLive, handle, liveTurnIdFor, dropSteer,
    showComposerHint, enqueueSteer, steerFrameSender, requeueSteersOnResume,
    recallSteers, flushSteerQueue, dropAbsorbedSteers,
    respond, stopTurn, respondPropose, answerQuestion, decideSubagentPlan,
    retryFailed, editResend, dismissFailed, retryError, retryFromCheckpoint,
    sendModUiPress, sendModUiAnswer,
  };
}
