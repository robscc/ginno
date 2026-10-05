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

import { useEffect, useRef } from "react";
import { useTranslations } from "next-intl";
import {
  openSessionSocket,
  getSessionHistory,
  getWorkflowRun,
  subagentPlanCancelFrame,
  subagentPlanConfirmFrame,
} from "@/lib/runtime";
import { notifyNative } from "@/lib/desktop";
import { notifyPrefs } from "@/lib/notifyPrefs";
import { useGinno } from "@/lib/store";
import { useSteerQueue } from "@/lib/steerQueue";
import {
  pruneMentions,
  type MenuItem,
  type ResolvedMention,
  type Trigger,
} from "@/components/chat/commandMenu";
import {
  foldSubagentResultBlocks,
  hasPendingTool,
  parseSubagentResult,
  useEventI18nText,
  type Block,
} from "@/components/chat/blocks";
import type {
  ContextChange,
  Goal,
  SessionMeta,
  SessionUsage,
  SubagentPlanEvent,
  SubagentPlanSubtask,
  SubagentSpawnEvent,
  SubagentStatusEvent,
  WorkflowRun,
} from "@/lib/types";
import {
  applyBlock,
  closePendingBlocks,
  mid,
  newTurnId,
  payloadFromBlocks,
  toolArgsPreview,
  type Attachment,
  type ChatMsg,
  type FileAttachment,
  type PermissionPrompt,
  type SendPayload,
  type VersionPropose,
} from "./streamCore";

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
  setInput: (v: string | ((p: string) => string)) => void;
  setAttachments: (v: Attachment[] | ((p: Attachment[]) => Attachment[])) => void;
  setTarget: (v: string | null | ((p: string | null) => string | null)) => void;
  setMenu: (v: MenuState | null | ((p: MenuState | null) => MenuState | null)) => void;
  setFileAttachments: (v: FileAttachment[] | ((p: FileAttachment[]) => FileAttachment[])) => void;
  setComposerHint: (v: string | null | ((p: string | null) => string | null)) => void;
  setProposeResult: (
    v: ProposeResult | null | ((p: ProposeResult | null) => ProposeResult | null),
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

type WsStatus = "connecting" | "live" | "reconnecting" | "offline";
interface MenuState { items: MenuItem[]; active: number; trigger: Trigger }
interface ProposeResult { decision: "allow" | "deny"; workflowId: string; fromVersion: number }

// Draft slot id for the session-less landing view (ChatStream 共用：上传完成的
// 文件回执要按 slot 归还，不能串会话）。
export const HOME_SLOT = "__home__";

export function useChatStreamEngine(deps: EngineDeps) {
  const {
    g, steerQ, session, onUsageChange, propose,
    input, attachments, fileAttachments,
    setMessages, setRuns, setLiveId, setWsStatus, setPermission, setPropose,
    setStreamAgent, setServerRunning, setInput, setAttachments, setTarget, setMenu,
    setFileAttachments, setComposerHint, setProposeResult,
    stickRef, connectRef, focusLatestRef, textareaRef, sumPendingRef,
    pinToBottom, uploadOneDoc, attachOne, attemptSend, recomputeMenu, finishSynthesisWait,
  } = deps;
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
    serverRunningRef.current[sid] = false;
    storeRef.current[sid] = (storeRef.current[sid] ?? []).map((m) =>
      m.role === "user" && m.status === "sending"
        ? // failReason 存稳定 key（渲染时经 chat.sendFailed.* 翻译），避免把
          // 成品文案烤进状态——语言切换后历史气泡仍能跟上。
          { ...m, status: "failed" as const, failReason: "connLost" }
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
    // 服务端只给 user 消息带 turnId；assistant 消息继承其前一条 user 消息的
    // turnId——中途接入的实时流要靠它认领「历史里本轮已存在的半截气泡」
    // （否则同一轮渲染成两块，2026-10-01 子会话渲染问题）。
    let carryTurn: string | undefined;
    const mapped: ChatMsg[] = (res.messages ?? []).map((m) => {
      const own = m.turnId ?? (m.role === "user" ? m.id : undefined);
      if (m.role === "user" && own) carryTurn = own;
      return {
      id: m.id ?? mid(),
      role: m.role,
      // 运行时注入的子代理结果（契约 3：<ginno_subagent_result> 包裹的
      // HumanMessage）在重放时折成结果卡片块，避免原始标签以文本气泡出现。
      blocks: foldSubagentResultBlocks(m.blocks),
      agentId: m.agentId,
      turnId: own ?? (m.role === "assistant" ? carryTurn : undefined),
      // Rebuild the retry payload for history user bubbles too — without
      // it, a retry that fails again would produce an error card with no
      // payload (no retry button), and the error handler's "last user with
      // payload" lookup would come up empty.
      sendPayload:
        m.role === "user"
          ? payloadFromBlocks(m.blocks, m.agentId ?? session?.agent_id ?? null)
          : undefined,
      };
    });
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
      // Skill blocks (history-replayed slash turns) normalize back to the
      // "/name request" text the live bubble carries, or the two never match
      // and a phantom "undelivered" duplicate appears.
      const textOf = (m: ChatMsg) =>
        m.blocks
          .map((b) =>
            b.kind === "text"
              ? b.text
              : b.kind === "skill"
                ? (b.text ? `/${b.name} ${b.text}` : `/${b.name}`)
                : "",
          )
          .join("\n")
          .replace(/\s+/g, " ")
          .trim();
      const pending = (storeRef.current[sid] ?? []).filter(
        (m) => m.role === "user" && m.status === "sending",
      );
      const mapped = mapHistory(res ?? {});
      // A steer_id present in history was absorbed even if the ack was lost to
      // the socket drop that caused this reconcile — drop those entries instead
      // of re-sending them (design §3.3, exactly-once).
      dropAbsorbedSteers(sid, mapped);
      for (const p of pending) {
        const delivered = mapped.some(
          (m) => m.role === "user" && textOf(m) === textOf(p),
        );
        if (!delivered) {
          mapped.push({ ...p, status: "failed" as const, failReason: "connLost" });
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
  const busyBySessionRef = useRef<Record<string, boolean>>({});
  // (The per-session steer queue ref now lives inside useSteerQueue.)
  // Auto-clear timer for the composer hint (one timer, hint is composer-global).
  const hintTimerRef     = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Per-session mirror of the `serverRunning` state (see syncDisplay).
  const serverRunningRef = useRef<Record<string, boolean>>({});
  const streamAgentRef   = useRef<Record<string, string | null>>({});
  // Orphan-stream tracking: this instance adopted an ALREADY-RUNNING turn
  // (remount mid-turn — the user navigated to another page while a reply was
  // streaming). Its turn.start is never seen, so the live bubble ensureLive
  // creates would render as a SECOND section next to the history-rendered
  // partial bubble; message.end heals the split by reconciling from history.
  const orphanStreamRef  = useRef<Record<string, boolean>>({});
  // 孤儿流是否已认领服务端 turn id（见 mutateLive 的认领逻辑）
  const adoptedTurnRef   = useRef<Record<string, boolean>>({});
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
    // The steer queue is no longer mirrored here — useSteerQueue re-renders the
    // component itself when the queue changes, and `steerItems` reads it live.
  };
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

  function ensureLive(sid: string, evTurn?: string): string {
    const existing = liveBySessionRef.current[sid];
    if (existing) return existing;
    // 承接本轮已存在的气泡（2026-10-01 子会话渲染成两块）：socket 在 turn 中途
    // 才连上时，历史重放里已经有这一轮的半截气泡（带正确的 agent 名与 turnId），
    // 若此时新建气泡，同一轮会渲染成「历史半截 + 无 agent 的实时块」两块。流事件
    // 应当认领承载同一 turnId 的气泡——没有 turnId 可比时才新建。
    if (evTurn) {
      const rows = storeRef.current[sid] ?? [];
      for (let i = rows.length - 1; i >= 0; i--) {
        const m = rows[i];
        if (m.role === "assistant" && m.turnId === evTurn) {
          liveBySessionRef.current[sid] = m.id;
          if (!streamAgentRef.current[sid]) streamAgentRef.current[sid] = m.agentId ?? null;
          return m.id;
        }
      }
    }
    const id = mid();
    storeRef.current[sid] = [
      ...(storeRef.current[sid] ?? []),
      { id, role: "assistant", blocks: [], agentId: streamAgentRef.current[sid], turnId: evTurn || newTurnId() },
    ];
    liveBySessionRef.current[sid] = id;
    return id;
  }

  function mutateLive(sid: string, ev: { event: string; [k: string]: unknown }) {
    const id = ensureLive(sid, ev.turn_id as string | undefined);
    // 气泡归属体检（2026-10-01 串线排查）：流事件带的 turn_id 必须与它落入的
    // 气泡 turnId 一致；不一致说明内容串进了别轮的气泡。release webview 没有
    // 可读控制台，所以同时上报服务端日志（client_diag）留证。
    const evTurn = ev.turn_id as string | undefined;
    const bubble = (storeRef.current[sid] ?? []).find((m) => m.id === id);
    // 孤儿流认领（2026-10-01 串线排查）：客户端在 turn 进行中途才连上 socket
    // （子会话常态——spawn 后隔几十秒才被打开），拿不到 turn.start，气泡带着
    // 本地占位 turnId。首个带 turn_id 的事件到来时认领服务端 id，否则该会话的
    // 每个事件都对不上气泡、（更要紧）steer 帧会带一个服务端不认的 turn_id。
    if (evTurn && bubble && bubble.turnId !== evTurn) {
      if (orphanStreamRef.current[sid] && !adoptedTurnRef.current[sid]) {
        adoptedTurnRef.current[sid] = true;
        storeRef.current[sid] = (storeRef.current[sid] ?? []).map((m) =>
          m.id === id ? { ...m, turnId: evTurn } : m,
        );
      }
    }
    if (evTurn && bubble?.turnId && evTurn !== bubble.turnId && !orphanStreamRef.current[sid]) {
      const detail = {
        event: ev.event,
        ev_turn: evTurn,
        bubble_turn: bubble.turnId,
        session: sid,
        head: String((ev.content as string) ?? "").slice(0, 40),
      };
      console.warn("[ginno] stream event landed in another turn's bubble", detail);
      try {
        socketsRef.current[sid]?.send(
          JSON.stringify({ type: "client_diag", diag_kind: "bubble_turn_mismatch", detail }),
        );
      } catch { /* socket may be closing */ }
    }
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
      // Transcript block, not a ref-based prompt like permission.request: the
      // card lives (and is merged by id) inside the live bubble's blocks.
      case "user.question":
      case "widget.emit":
      case "ref.emit":
      case "image.emit":
      case "workflow.emit":
        adoptOrphanStream(sid);
        mutateLive(sid, ev);
        break;
      case "turn.start": {
        markDelivered(sid);
        seenTurnStartRef.current[sid] = true;
        // Sidebar ordering (reactivated sessions float up): the runtime bumps
        // `updated` on every invoke, but only this socket knows it happened —
        // patch the store's copy so the day-group resort is live, not stale
        // until the next full reload.
        g.applySessionPatch(sid, { updated: Math.floor(Date.now() / 1000) });
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
      case "todos.changed":
        g.reloadTodos();
        break;
      case "skills.changed":
        // A turn (install_skills tool, bash) or the Settings page mutated
        // ~/.ginno/skills — refresh the slash menu's skill list live.
        g.reloadSkills();
        break;
      case "memory.changed":
        // Memory refinery transition (auto-draft ready, applied, discarded) —
        // refresh the Memory tab badge; the panel itself refetches on open.
        g.reloadMemoryBadge();
        break;
      case "agents.changed":
        // Agent CRUD in Settings — keep the picker/mention list in sync.
        g.reloadAgents();
        break;
      case "workflows.changed":
        g.reloadWorkflows();
        g.reloadWorkflowRuns();
        break;
      case "synthesis.event":
        // Background summarization progressed — refresh the 总结 panel list
        // (started/attempt/finished all change what it shows). If THIS is the
        // case the summarize button is waiting on, resolve the wait (the ref
        // guard inside finishSynthesisWait dedupes vs the polling fallback).
        void g.reloadSynthesisCases();
        if (ev.kind === "finished" && ev.synthesis_id === sumPendingRef.current) {
          void finishSynthesisWait(ev.synthesis_id as string);
        }
        break;
      case "run.bind": {
        const runId = ev.run_id as string;
        // 侧栏伪条目即时出现（run view 方案）：in-chat 卡片走下面的拉取，
        // store 的全量运行列表也同步刷新，工作流面板/角标不用等 30s 轮询。
        void g.reloadWorkflowRuns();
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
      case "code.changed":
        // The agent wrote or edited a file (design §4.7 S3). The event carries
        // the POST-WRITE version, which is what lets the code panel reuse its
        // conflict bar for a dirty buffer instead of inventing a second UI —
        // see the S3 brief §0.
        if (typeof ev.path === "string" && (ev.op === "write" || ev.op === "edit")) {
          g.notifyCodeChange({
            path: ev.path,
            op: ev.op as "write" | "edit",
            version: typeof ev.version === "string" ? ev.version : "",
          });
        }
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
      case "steer.accepted":
        // The server stashed the entry (design §3.1). It stays in the queue bar
        // until it is absorbed; this only flips it out of "sending" (the shared
        // hook owns that queue mutation).
        steerQ.handleEvent(ev, sid);
        break;
      case "steer.absorbed": {
        // The steered message was DRAINED into state (the server acks at drain
        // time, not at superstep commit — see docs/steering-design.md): move it
        // out of the queue bar into the transcript as a band at the injection
        // point — appended to the live assistant bubble, because one turn is one
        // bubble (design §4.2/§4.3).
        const absorbedId = ev.steer_id as string | undefined;
        if (!absorbedId) break;
        // Capture the entry BEFORE delegating — the hook drops it from the queue.
        const entry = steerQ.itemsFor(sid).find((i) => i.steerId === absorbedId);
        if (!entry) break; // already dropped — a history reconcile got there first
        // Same timing/order as before: remove from the queue first, then render.
        steerQ.handleEvent(ev, sid);
        // runtime 注入的子代理结果走同一条 steer 通道（契约 3）：折成结果卡片，
        // 而不是把 <ginno_subagent_result> 原始标签渲染成一条注入带。
        const injectedResult = parseSubagentResult(entry.text);
        if (injectedResult) {
          const store = storeRef.current[sid] ?? [];
          const already = store.some((m) =>
            m.blocks.some(
              (b) =>
                b.kind === "subagent_result" && b.sessionId === injectedResult.sessionId,
            ),
          );
          if (!already) {
            storeRef.current[sid] = [
              ...store,
              {
                id: mid(),
                role: "system" as const,
                blocks: [
                  {
                    kind: "subagent_result",
                    sessionId: injectedResult.sessionId,
                    goal: injectedResult.goal,
                    summary: injectedResult.summary,
                  },
                ],
              },
            ];
          }
          syncDisplay(sid);
          break;
        }
        const band: Block = {
          kind: "steer",
          text: entry.text,
          steerId: entry.steerId,
          injectedAt: Number(ev.injected_at) || Math.floor(Date.now() / 1000),
          // Attachments ride the band so the injection point shows what the
          // user actually sent. Images arrive as display URLs (data URL), files
          // as name/path chips — mirroring the replay shape the server emits.
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
        const liveMsgId = liveBySessionRef.current[sid];
        const store = storeRef.current[sid] ?? [];
        if (liveMsgId && store.some((m) => m.id === liveMsgId)) {
          storeRef.current[sid] = store.map((m) =>
            m.id === liveMsgId ? { ...m, blocks: [...m.blocks, band] } : m,
          );
        } else {
          // No live bubble of ours (adopted orphan / goal continuation): the
          // band lands as its own full-width row, and the next history
          // reconcile rebuilds the merged bubble from the checkpoint.
          storeRef.current[sid] = [
            ...store,
            { id: mid(), role: "user" as const, blocks: [band], turnId: entry.turnId },
          ];
        }
        syncDisplay(sid);
        break;
      }
      case "delegate_update": {
        // P1 委托实时进度：运行中的系统行同 id 刷新；running=false 撤行
        //（结果卡由 subagent.status 通道补位，已有 dedup 与跳转）。
        const did = ev.delegation_id as string;
        if (!did) break;
        const liveId = `delegate-live-${did}`;
        const store0 = storeRef.current[sid] ?? [];
        const without = store0.filter((m) => m.id !== liveId);
        if (ev.running === false) {
          storeRef.current[sid] = without;
          // 显示同步必须走 syncDisplay(带当前会话守卫):委托运行期间本事件
          // 持续打在父会话 socket 上,若用户正在看别的会话(典型:运行中的
          // 子会话回放页),裸 setMessages 会把父会话消息刷进当前视图——
          // "subsession 自动刷新主 session 内容"即此。store 照常落,切回
          // 父会话时由 syncDisplay 统一呈现。
          syncDisplay(sid);
        } else {
          const nxt = [
            ...without,
            {
              id: liveId,
              role: "system" as const,
              blocks: [
                {
                  kind: "text",
                  text: `🤖 ${ev.backend ?? "delegation"} · ${ev.text ?? "working…"}`,
                } as Block,
              ],
            },
          ];
          storeRef.current[sid] = nxt;
          syncDisplay(sid);
        }
        break;
      }
      case "notice":
        // Built-in command reply (e.g. /help): no graph turn ran, so the server
        // pushes the rendered text directly into the live bubble as one delta.
        // 事件契约：带 i18n_key 时落地即翻译，否则原样直显 message。
        markDelivered(sid);
        let _txt = i18nRef.current.evText(ev, (ev.message as string) || "");
        // auto-retry 是瞬态系统提示——包成 code span + ⚠️，气泡里呈现为
        // 琥珀色等宽小条而不是混入正文的普通文本（用户反馈样式不显眼）。
        if ((ev.i18n_key as string) === "stream.auto_retry") {
          _txt = "`⚠️ " + _txt + "`";
        }
        mutateLive(sid, {
          event: "token.delta",
          content: _txt,
        });
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
      case "subagent.spawned": {
        // 子代理生命周期事件（契约 2：广播到父 session 与子 session 的所有
        // socket）。Store 侧 upsert 会话列表的子会话行；只有当本会话就是
        // 发起父时，transcript 里追加一张发起卡片（子 socket 收到同帧但不渲染）。
        g.notifySubagentSpawned(ev as unknown as SubagentSpawnEvent);
        // delegation 的 spawned 只为侧栏建行服务:transcript 不加发起卡——
        // 委托的实时进度/结果卡由 delegate_update + subagent.status 通道负责,
        // 叠加发起卡会双渲染。
        if (ev.parent_session_id === sid && ev.type !== "delegation") {
          storeRef.current[sid] = [
            ...(storeRef.current[sid] ?? []),
            {
              id: mid(),
              role: "system" as const,
              blocks: [
                {
                  kind: "subagent_spawn",
                  sessionId: ev.session_id as string,
                  goal: (ev.goal as string) || "",
                  constraints: (ev.constraints as string) || "",
                  acceptance: (ev.acceptance as string) || "",
                  title: (ev.title as string) || "",
                  depth: (ev.depth as number) ?? 0,
                  origin: (ev.origin as "user" | "agent") || "agent",
                  spawnedAt: Math.floor(Date.now() / 1000),
                },
              ],
            },
          ];
        }
        break;
      }
      case "subagent.status": {
        // 状态机推进（running/waiting/done/failed/stopped）：store 的子会话行
        // 即时换挡（侧栏状态点、卡片状态共用）。终态且本会话是发起父时追加
        // 结果卡片——stopped 按契约不注入（用户/级联亲手杀的），跳过。
        g.notifySubagentStatus(ev as unknown as SubagentStatusEvent);
        const st = ev.status as string;
        if (
          ev.parent_session_id === sid &&
          (st === "done" || st === "failed")
        ) {
          const childId = ev.session_id as string;
          const store = storeRef.current[sid] ?? [];
          // 去重：同一子会话的结果卡片只出现一次（注入消息的 steer.absorbed
          // 路径可能已经先渲染过）。
          const already = store.some((m) =>
            m.blocks.some((b) => b.kind === "subagent_result" && b.sessionId === childId),
          );
          if (!already) {
            storeRef.current[sid] = [
              ...store,
              {
                id: mid(),
                role: "system" as const,
                blocks: [
                  {
                    kind: "subagent_result",
                    sessionId: childId,
                    summary: (ev.result_summary as string) || "",
                    error: (ev.error as string) || undefined,
                  },
                ],
              },
            ];
          }
        }
        break;
      }
      case "subagent.plan": {
        // 拆分方案（P2 共享契约 1）：/subagent 拆分 的 LLM decompose 结果，
        // 广播到当前 session 的 socket。折成系统行里的 subagent_plan 卡片；
        // 未确认前再次到来（契约 3 的「覆盖旧 plan」）→ 就地替换还在 pending
        // 的旧卡片，已决定过的卡片保留作为记录。
        const plan = ev as unknown as SubagentPlanEvent;
        const store = storeRef.current[sid] ?? [];
        const newBlock: Block = {
          kind: "subagent_plan",
          planId: plan.plan_id,
          task: plan.task || "",
          subtasks: Array.isArray(plan.subtasks) ? plan.subtasks : [],
          status: "pending",
        };
        const pendingIdx = store.findIndex((m) =>
          m.blocks.some((b) => b.kind === "subagent_plan" && b.status === "pending"),
        );
        if (pendingIdx >= 0) {
          storeRef.current[sid] = store.map((m, i) =>
            i === pendingIdx ? { ...m, blocks: [newBlock] } : m,
          );
        } else {
          storeRef.current[sid] = [
            ...store,
            { id: mid(), role: "system" as const, blocks: [newBlock] },
          ];
        }
        syncDisplay(sid);
        break;
      }
      case "subagent.plan.cancelled": {
        // 拆分方案被取消（P3 共享契约 5）：另一个窗口发了 subagent.plan.cancel，
        // runtime 广播回执——把本窗口里该 plan 还在 pending 的卡片翻成已取消态。
        // 本地发起的取消已在 decideSubagentPlan 里乐观定格，这里幂等（只动 pending）。
        const cancelledPlanId = ev.plan_id as string | undefined;
        if (!cancelledPlanId) break;
        storeRef.current[sid] = (storeRef.current[sid] ?? []).map((m) => ({
          ...m,
          blocks: m.blocks.map((b) =>
            b.kind === "subagent_plan" && b.planId === cancelledPlanId && b.status === "pending"
              ? { ...b, status: "cancelled" as const }
              : b,
          ),
        }));
        break;
      }
      case "context.updated": {
        // WorldState change announcement (world-state-plan §7). Chip display
        // level table: environment-only changes (date rollover) stay SILENT in
        // the UI; everything else gets a centered context row.
        const changes = ((ev.changes as ContextChange[]) || []).filter(Boolean);
        const visible = changes.filter((c) => c.section !== "environment");
        if (!visible.length) break; // environment-only (date rollover) = silent
        // 事件契约（i18n-design.md §3）：单条 change 可带 i18n_key+params，
        // 透传给 ContextBlocks 渲染时翻译；旧 runtime 只有成品 summary，直显。
        const rows = visible.map((c): Block => {
          const extra = c as { i18n_key?: unknown; params?: unknown };
          return {
            kind: "context",
            text: c.summary,
            ...(typeof extra.i18n_key === "string"
              ? {
                  i18n_key: extra.i18n_key,
                  ...(extra.params != null ? { params: extra.params as Record<string, string | number> } : {}),
                }
              : {}),
          };
        });
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
                // 契约：key 命中渲染时翻译（chat.context.microcompacted）；
                // text 是旧 runtime / key 未命中时的英文兜底（与历史版本一致）。
                text: `Cleared ${n} older tool outputs to save context; call the tools again to re-fetch when needed.`,
                i18n_key: "chat.context.microcompacted",
                params: { n },
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
                text: `Conversation compacted: ${n} older messages were replaced with a summary; recent messages are kept verbatim.`,
                i18n_key: "chat.context.compacted",
                params: { n },
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
        if (serverRunningRef.current[sid] !== !!ev.running) {
          serverRunningRef.current[sid] = !!ev.running;
          // Mirror into React state — this is what makes a history-rebuilt
          // pending question card interactive after a reload (questionLive).
          syncDisplay(sid);
        }
        if (ev.running) break; // the broadcast stream resumes on this socket
        // ...unless our newest send is NEWER than the probe this answers. The
        // answer can only describe the session as it was when the server
        // handled the probe; reconciling a send that postdates it reads a
        // /history with no checkpoint yet and stamps the bubble 「未送达」 on a
        // turn that is running fine (first message of a new session: the socket
        // is opened by the send, probe at t0, invoke at t0+13ms). Wait for the
        // stream instead — it always ends in message.end or error, both of
        // which reconcile a genuinely dead turn.
        if ((sendSeqRef.current[sid] ?? 0) > (probeSeqRef.current[sid] ?? 0)) break;
        // Not running: rebuild from persisted history ONLY when this client
        // believed a turn was in flight (the probe now fires on every open —
        // an idle reconnect must not replace the loaded store). This is also
        // what flips a pending question card of a genuinely dead turn into
        // the checkpoint's healed "(interrupted)" → skipped replay.
        if (
          liveBySessionRef.current[sid] ||
          busyBySessionRef.current[sid] ||
          orphanStreamRef.current[sid]
        ) {
          reconcileTurnFromHistory(sid);
        }
        break;
      }
      case "message.end": {
        markDelivered(sid);
        const wasOrphan = !!orphanStreamRef.current[sid];
        orphanStreamRef.current[sid] = false;
        adoptedTurnRef.current[sid] = false;
        liveBySessionRef.current[sid] = null;
        streamAgentRef.current[sid] = null;
        busyBySessionRef.current[sid] = false;
        serverRunningRef.current[sid] = false;
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
            const body = raw || i18nRef.current.tr("notify.replyCompleted");
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
        // The turn ended with steer entries still unacknowledged: the oldest
        // becomes the next turn (design §3.3). Skipped on the orphan path above
        // — its history reconcile owns that decision, and flushing here could
        // re-send an entry the reconcile is about to see as absorbed.
        if (!wasOrphan) flushSteerQueue(sid);
        break;
      }
      case "turn.stopped": {
        // User pressed stop: the server abandoned the in-flight step and
        // healed the persisted state; everything already streamed is kept.
        // Close out the live stream like message.end, force-close pending
        // tool blocks like the error handler — but no error card and no
        // "turn done" notification (the turn didn't complete).
        markDelivered(sid);
        const wasOrphan = !!orphanStreamRef.current[sid];
        orphanStreamRef.current[sid] = false;
        adoptedTurnRef.current[sid] = false;
        const liveMsgId = liveBySessionRef.current[sid];
        liveBySessionRef.current[sid] = null;
        streamAgentRef.current[sid] = null;
        busyBySessionRef.current[sid] = false;
        serverRunningRef.current[sid] = false;
        // ⏹ means stop, so what was queued for absorption is handed back to the
        // composer rather than discarded or auto-sent (design §4.1). Done here,
        // in the event, so a stop from another tab recalls on this one too.
        recallSteers(sid);
        // Stop also clears a parked prompt (server heals those too) — every
        // tab of the session leaves the running state together.
        permsRef.current[sid] = null;
        proposeRef.current[sid] = null;
        setPermission(null);
        setPropose(null);
        if (wasOrphan) {
          reconcileTurnFromHistory(sid);
          break;
        }
        const list = storeRef.current[sid] ?? [];
        storeRef.current[sid] = list
          // An empty live bubble (stopped before the first token) would
          // render as a confusing "（空回复）" — drop it; streamed text stays.
          .filter((m) => !(m.id === liveMsgId && m.blocks.length === 0))
          // Close out any in-flight tool blocks (and pending question cards —
          // the backend heals a parked ask_user to "(interrupted)" too, which
          // replay reads as skipped) so `running` unsticks.
          .map((msg) =>
            hasPendingTool(msg.blocks)
              ? { ...msg, blocks: closePendingBlocks(msg.blocks) }
              : msg,
          );
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
        serverRunningRef.current[sid] = false;
        // Orphaned turn that failed: reconcile from history instead of
        // building a card on the split store — mapHistory re-surfaces the
        // persisted last_error as a proper error card with retry.
        if (orphanStreamRef.current[sid]) {
          orphanStreamRef.current[sid] = false;
          adoptedTurnRef.current[sid] = false;
        adoptedTurnRef.current[sid] = false;
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
            // Close out any in-flight tool blocks as interrupted so `running` unsticks,
            // and mark the live bubble as `failed` so the UI can show a visual
            // indicator (red border + "回复中断" label) instead of looking like
            // a normal completed assistant reply.
            .map((msg) => {
              const marked = msg.id === liveMsgId ? { ...msg, failed: true } : msg;
              if (!hasPendingTool(marked.blocks)) return marked;
              return { ...marked, blocks: closePendingBlocks(marked.blocks) };
            }),
          {
            id: mid(),
            role: "assistant" as const,
            // 事件契约（i18n-design.md §3）：error 事件可带 i18n_key+params，
            // 落地即翻译；未命中/缺失回退服务端成品 message，再退「未知错误」。
            blocks: [
              {
                kind: "text",
                text:
                  i18nRef.current.evText(ev, String(ev.message || "")) ||
                  i18nRef.current.tr("error.unknown"),
              },
            ],
            turnId: liveBubble?.turnId,
            error: true,
            sendPayload: lastUser?.sendPayload,
            sourceMsgId: lastUser?.id,
          },
        ];
        // The turn died with entries still unacknowledged → the oldest becomes
        // the next turn, same rule as a normal end (design §3.3).
        flushSteerQueue(sid);
        break;
      }
    }
    syncDisplay(sid);
  }
  // Docs/native paths dropped while on the landing home (no session yet):
  // buffered with optimistic chips, uploaded right after lazy creation.
  const pendingDocsRef = useRef<Array<{ file: File; tmpId: string }>>([]);
  const pendingPathsRef = useRef<Array<{ path: string; tmpId: string }>>([]);
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
  // ─── Mid-turn steering (docs/steering-design.md) ──────────────────────────────
  /** The running turn's id for a session (the steer rides it). Empty string
   *  when unknown — the server then falls back to its own running-turn id,
   *  which is the only correct answer for a turn nobody here invoked (an
   *  adopted orphan, or a goal continuation). */
  function liveTurnIdFor(sid: string): string {
    const live = liveBySessionRef.current[sid];
    const msg = live ? (storeRef.current[sid] ?? []).find((m) => m.id === live) : undefined;
    return msg?.turnId ?? "";
  }

  /** Drop one queued entry (the ✕ on a queue-bar row). */
  function dropSteer(sid: string, steerId: string) {
    steerQ.remove(sid, [steerId]);
  }

  /** Transient, visible feedback in the composer (never a silent no-op). */
  function showComposerHint(msg: string) {
    setComposerHint(msg);
    if (hintTimerRef.current) clearTimeout(hintTimerRef.current);
    hintTimerRef.current = setTimeout(() => setComposerHint(null), 4000);
  }

  function enqueueSteer(
    sid: string,
    text: string,
    agentId: string | null,
    images: Attachment[] = [],
    files: FileAttachment[] = [],
  ) {
    steerQ.enqueue({
      sessionId: sid,
      turnId: liveTurnIdFor(sid),
      text,
      images,
      files,
      agentId,
      send: steerFrameSender(sid),
    });
  }

  /** Raw-frame sender for a session's socket, handed to the shared queue (which
   *  owns the frame shape and swallows a dead socket so the entry stays queued).
   *  Each window passes its own socket here — the pin has its own. */
  function steerFrameSender(sid: string): (frame: unknown) => void {
    return (frame) => {
      socketsRef.current[sid]?.send(JSON.stringify(frame));
    };
  }

  /** Re-send every queued entry as `steer` — done right before we resume a
   *  parked turn, so an entry stashed in the segment that parked is not lost.
   *  Safe to repeat: server-side enqueue replaces by steer_id (design §3.2). */
  function requeueSteersOnResume(sid: string) {
    steerQ.onResume(sid, steerFrameSender(sid));
  }

  /** ⏹ means stop: hand what is still queued back to the composer — multi-line,
   *  in order, ABOVE whatever is already typed (the ↑ recall's placement) —
   *  rather than silently discarding what the user wrote (design §4.1). */
  function recallSteers(sid: string) {
    const items = steerQ.recall(sid);
    if (!items.length) return;
    const recalled = items.map((i) => i.text).join("\n");
    // Attachments come back too — a recalled steer must not lose its pictures
    // or file chips (the whole reason they are kept on the entry, brief §5.1).
    const recalledImgs = items.flatMap((i) => i.images);
    const recalledFiles = items.flatMap((i) => i.files);
    if (sid !== curSessionIdRef.current) {
      // Background session: park it in that session's draft, so switching back
      // shows it in the composer instead of dropping it on the floor.
      const d = draftCacheRef.current[sid] ?? { input: "", attachments: [] };
      draftCacheRef.current[sid] = {
        ...d,
        input: d.input ? `${recalled}\n${d.input}` : recalled,
        attachments: [...recalledImgs, ...d.attachments],
        files: [...recalledFiles, ...(d.files ?? [])],
      };
      return;
    }
    setInput((cur) => (cur ? `${recalled}\n${cur}` : recalled));
    if (recalledImgs.length) setAttachments((a) => [...recalledImgs, ...a]);
    if (recalledFiles.length) setFileAttachments((f) => [...recalledFiles, ...f]);
  }

  /** The turn ended with entries still unacknowledged: the OLDEST becomes the
   *  next turn — Claude Code's rule verbatim ("when the turn ends with messages
   *  still queued, Claude Code sends only the oldest as the next turn"). The
   *  rest stay queued and follow the same rule on that turn's end (design §3.3). */
  function flushSteerQueue(sid: string) {
    // Pop the oldest out of the queue (the shared hook owns the ref write), then
    // promote it to the next turn here — turning it into a turn is this window's
    // concern, not the queue's.
    const head = steerQ.takeOldest(sid);
    if (!head) return;
    attemptSend(sid, {
      text: head.text,
      // The entry's attachments ride the invoke that becomes the next turn —
      // dropping them here is the other half of the silent-loss bug.
      images: head.images,
      files: head.files,
      mentions: [],
      agentId: head.agentId,
    });
  }

  /** History is authoritative: a steer_id it carries was absorbed even if the
   *  ack was lost to a socket drop, so drop those entries instead of re-sending
   *  them. This is what keeps delivery exactly-once (design §3.3). The history
   *  scan stays here (it reads this window's mapped bubbles); the hook does the
   *  queue write. */
  function dropAbsorbedSteers(sid: string, mapped: ChatMsg[]) {
    const absorbed = new Set<string>();
    for (const m of mapped) {
      for (const b of m.blocks) {
        if (b.kind === "steer" && b.steerId) absorbed.add(b.steerId);
      }
    }
    if (absorbed.size) steerQ.remove(sid, absorbed);
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

  /** Retry on a turn-error card: keep the error card in place as a record of
   * the failure, then append a brand-new user bubble + assistant placeholder
   * at the tail of the chat. The new turn gets a fresh turnId so the server
   * treats it as a new attempt (no checkpoint dedup collision). */
  function retryError(msgId: string) {
    const sid = curSessionIdRef.current;
    if (!sid || busyBySessionRef.current[sid]) return; // one turn at a time
    const list = storeRef.current[sid] ?? [];
    const card = list.find((m) => m.id === msgId);
    if (!card?.error || !card.sendPayload) return;
    // Keep the error card — don't remove it. Just start a fresh turn at the bottom.
    attemptSend(sid, card.sendPayload);
  }

  /** Retry from checkpoint: resume the failed turn from its latest checkpoint
   * instead of re-executing from the start. Preserves tool calls and
   * intermediate results. */
  function retryFromCheckpoint(msgId: string) {
    const sid = curSessionIdRef.current;
    if (!sid || busyBySessionRef.current[sid]) return;
    const list = storeRef.current[sid] ?? [];
    const card = list.find((m) => m.id === msgId);
    if (!card?.error) return;
    // Remove the error card
    storeRef.current[sid] = list.filter((m) => m.id !== msgId);
    syncDisplay(sid);
    // Mark as busy
    busyBySessionRef.current[sid] = true;
    const turnId = card.turnId || crypto.randomUUID();
    const sock = socketsRef.current[sid];
    if (!sock || sock.readyState !== WebSocket.OPEN) {
      busyBySessionRef.current[sid] = false;
      return;
    }
    try {
      sock.send(
        JSON.stringify({
          type: "retry_from_checkpoint",
          turn_id: turnId,
        }),
      );
    } catch {
      busyBySessionRef.current[sid] = false;
    }
  }

  function respond(decision: "allow" | "deny") {
    const sid = curSessionIdRef.current;
    if (!sid) return;
    // Anything still in the steer queue belongs to this turn: re-send it as
    // `steer` BEFORE the resume, so the resumed segment absorbs it (design
    // §3.4). Idempotent by steer_id, so a duplicate can't inject it twice.
    requeueSteersOnResume(sid);
    try {
      socketsRef.current[sid]?.send(JSON.stringify({ type: "permission_response", decision }));
    } catch {
      /* socket gone — reconnect re-emits the prompt if still pending */
    }
    permsRef.current[sid] = null;
    setPermission(null);
  }

  function stopTurn() {
    // Hard-stop the running turn (server abandons the in-flight step, keeps
    // what already streamed, heals state). Idempotent server-side; a stopped
    // turn ends with a turn.stopped broadcast that clears `running`.
    const sid = curSessionIdRef.current;
    if (!sid) return;
    try {
      socketsRef.current[sid]?.send(JSON.stringify({ type: "stop" }));
    } catch {
      /* socket gone — reconnect reconciles via turn_state */
    }
  }

  /** 拆分方案卡片的决定（P2 共享契约 2）：上行 confirm/cancel 帧到该 plan 所在
   *  会话的 socket，并把 store 里对应卡片的 status 定格为回执态。confirm 带
   *  用户编辑过的 subtasks（缺省 = 原样采纳），runtime 逐个 spawn 后经既有
   *  subagent.spawned 事件回流。socket 不必是当前展示的会话——按 planId 反查
   *  所在 store，用户在卡片存活期间切走会话再点仍能发对地方。 */
  function decideSubagentPlan(
    planId: string,
    decision: "confirm" | "cancel",
    subtasks?: SubagentPlanSubtask[],
  ) {
    let sid: string | null = null;
    for (const [k, list] of Object.entries(storeRef.current)) {
      if (list.some((m) => m.blocks.some((b) => b.kind === "subagent_plan" && b.planId === planId))) {
        sid = k;
        break;
      }
    }
    if (!sid) return;
    let sent = false;
    try {
      const sock = socketsRef.current[sid];
      if (sock && sock.readyState === WebSocket.OPEN) {
        sock.send(
          JSON.stringify(
            decision === "confirm"
              ? subagentPlanConfirmFrame(planId, subtasks)
              : subagentPlanCancelFrame(planId),
          ),
        );
        sent = true;
      }
    } catch {
      /* socket flipped CLOSING/CLOSED between check and send */
    }
    // 帧没发出去就不定格：卡片留在 pending，重连后可再点一次——静默丢一次
    // confirm 等于整批子任务凭空消失，比卡片晚一拍定格严重得多。
    if (!sent) return;
    storeRef.current[sid] = (storeRef.current[sid] ?? []).map((m) => ({
      ...m,
      blocks: m.blocks.map((b) =>
        b.kind === "subagent_plan" && b.planId === planId && b.status === "pending"
          ? { ...b, status: decision === "confirm" ? ("confirmed" as const) : ("cancelled" as const) }
          : b,
      ),
    }));
    syncDisplay(sid);
  }

  function respondPropose(decision: "allow" | "deny") {
    const sid = curSessionIdRef.current;
    if (!sid) return;
    // Reuses the permission_response channel; the server resumes the proposal
    // interrupt with {decision}, and the propose_edit tool applies on allow.
    requeueSteersOnResume(sid);
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

  // Answer a parked ask_user card. The server ignores the message unless the
  // session is actually parked on a user_question interrupt (stale/duplicate
  // answers are dropped there, not here). The optimistic fold collapses EVERY
  // pending copy of the card by id — a post-reload re-emit can briefly have
  // the history card and a live one on screen; the authoritative receipt still
  // arrives via the tool's tool.end.
  function answerQuestion(id: string, answer: string, optionIndex: number | null, skip: boolean) {
    const sid = curSessionIdRef.current;
    if (!sid) return;
    // Same rule as the permission path: queued entries belong to this turn, so
    // re-send them as `steer` before the answer resumes it (design §3.4).
    requeueSteersOnResume(sid);
    try {
      socketsRef.current[sid]?.send(
        JSON.stringify({ type: "user_answer", answer, option_index: optionIndex, skip }),
      );
    } catch {
      /* socket gone — reconnect re-emits user.question if still parked */
    }
    storeRef.current[sid] = (storeRef.current[sid] ?? []).map((m) =>
      m.blocks.some((b) => b.kind === "question" && b.id === id && b.status === "pending")
        ? {
            ...m,
            blocks: m.blocks.map((b) =>
              b.kind === "question" && b.id === id && b.status === "pending"
                ? skip
                  ? { ...b, status: "skipped" as const }
                  : { ...b, status: "answered" as const, answer, optionIndex }
                : b,
            ),
          }
        : m,
    );
    syncDisplay(sid);
  }

  return {
    // refs
    liveIdRef, activeSidRef, socketReadyRef, prevSlotRef, curSessionIdRef,
    storeRef, liveBySessionRef, socketsRef, statusRef, permsRef, proposeRef,
    busyBySessionRef, hintTimerRef, serverRunningRef, streamAgentRef,
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
  };
}
