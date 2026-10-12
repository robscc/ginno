"use client";

import { useEffect, useRef, useState } from "react";
import { Paperclip, Keyboard, ArrowUp, X, AlertCircle, Square, Check, RotateCcw } from "lucide-react";
import { useGinno } from "@/lib/store";
import { useSteerQueue } from "@/lib/steerQueue";
import { loadToolLabels } from "@/lib/toolLabels";
import { useTranslations } from "next-intl";
import { t } from "@/i18n/provider";
import { agentHex } from "@/lib/theme";
import { greeting } from "@/lib/utils";
import { Icon } from "@/components/icons";
import { ContextFoldersChip } from "@/components/shell/ContextFoldersChip";
import { hasPendingTool, type Block, type QuestionBlock } from "@/components/chat/blocks";
import { LiveRunBlock } from "./RunBlocks";
import { SummarizeModal } from "./SummarizeModal";
import { ConfirmModal } from "@/components/ConfirmModal";
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
import type { WorkflowRun } from "@/lib/types";
import type { SessionMeta, SessionUsage } from "@/lib/types";
import {
  appendEntry,
  caretOnFirstLine,
  caretOnLastLine,
  initHistory,
  navDown,
  navUp,
  seedEntries,
  type HistoryState,
} from "./inputHistory";
import {
  mid,
  newTurnId,
  recommendAgentId,
  type Attachment,
  type ChatMsg,
  type FileAttachment,
  type PermissionPrompt,
  type SendPayload,
  type VersionPropose,
} from "./streamCore";
import { ProposeCard, SubagentTopBar } from "./streamCards";
import { PlanBoard } from "./PlanBoard";
import { SubagentBoard } from "./SubagentBoard";
import { useChatStreamEngine, HOME_SLOT } from "./useChatStreamEngine";
import { useComposerFiles } from "./useComposerFiles";
import { useRunActions } from "./useRunActions";
import { useModelPick } from "./useModelPick";
import { useRouter } from "next/navigation";
import { useSummarizeFlow } from "./useSummarizeFlow";
import { ModBand } from "./mod/ModBand";
import { ModAskCard, type ModAskState } from "./mod/ModAskCard";
import type { ModBandGroup } from "./mod/ModBand";
import { QueueBar } from "./stream/QueueBar";
import { ModelChipMenu } from "./stream/ModelChipMenu";
import { ConnStatusChip } from "./stream/ConnStatusChip";
import { ComposerAttachmentRows, ImagesReadingRow } from "./stream/ComposerAttachmentRows";
import { DelegationStatusBar } from "./stream/DelegationStatusBar";
import { PermissionPromptCard } from "./stream/PermissionPromptCard";
import { SummarizeMenu } from "./stream/SummarizeMenu";
import { TranscriptList } from "./stream/TranscriptList";

export function ChatStream({
  session,
  onRunningChange,
  onUsageChange,
  onOpenGoal,
}: {
  session: SessionMeta | null;
  onRunningChange?: (b: boolean) => void;
  onUsageChange?: (u: SessionUsage) => void;
  onOpenGoal?: () => void;
}) {
  const g = useGinno();
  // chat 域翻译；tr 为动态 key（failReason 稳定 key、状态枚举等）收敛后的
  // string 签名版本，存在性用 has 前置校验（理由见 blocks.tsx useChatT）。
  const tc = useTranslations("chat");
  const router = useRouter();
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
  // Last `turn.state` probe answer: the server says a turn runs/parks for
  // this session. The non-circular liveness source for question cards rebuilt
  // from history — after a full reload liveId is null, yet a parked ask_user
  // still accepts answers (backend resumes them from ANY socket).
  const [serverRunning, setServerRunning] = useState(false);
  // E3 压缩进行中（context.compacting）：live 气泡状态行显示「压缩中…」。
  // 瞬态显示层状态,真源在 engine 的 compactingBySessionRef。
  const [compacting, setCompacting] = useState(false);
  const [wsStatus, setWsStatus] = useState<"connecting" | "live" | "reconnecting" | "offline">("connecting");
  // Composer height: undefined = auto-grow (capped); a number = user-dragged size.
  const [composerH, setComposerH] = useState<number | undefined>(undefined);
  // Command/mention autocomplete: open menu (items + active index + trigger).
  const [menu, setMenu] = useState<{ items: MenuItem[]; active: number; trigger: Trigger } | null>(null);
  // Mid-turn steering queue (docs/steering-design.md). Shared with the floating
  // quick-chat window; the hook owns the queue + acks, this component renders
  // it. `steerItems` (below) is read during render.
  const steerQ = useSteerQueue();
  // Visible feedback for a send blocked by an in-flight attachment (image
  // compression / document upload). Both paths used to return silently — the
  // user pressed send and NOTHING happened. Auto-clears after a few seconds.
  const [composerHint, setComposerHint] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true); // auto-scroll only while the user is near the bottom
  const fileRef = useRef<HTMLInputElement | null>(null);
  const composerBoxRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  // Composer ↑/↓ input history, per session (shell-style recall; the seed
  // comes from the session's already-loaded user messages, so a reload or
  // device switch still recalls the full history). See inputHistory.ts.
  const inputHistoryRef = useRef<Record<string, HistoryState>>({});
  const dragRef = useRef<{ startY: number; startH: number } | null>(null);
  // connectRef: the reconnect button always calls this; updated on each session switch
  const connectRef = useRef<() => void>(() => {});
  // Set when a notification click asked to land on a session's latest message;
  // consumed by the session-switch effect / focus-latest listener below.
  const focusLatestRef = useRef<string | null>(null);
  const [runs, setRuns]   = useState<WorkflowRun[]>([]);
  // Run id pending delete confirmation (ConfirmModal guards the destructive op).
  const [confirmDelRun, setConfirmDelRun] = useState<string | null>(null);
  const sumPendingRef = useRef<string | null>(null);
  // Receipt shown briefly after a version_propose decision (card already unmounted).
  const [proposeResult, setProposeResult] = useState<
    { decision: "allow" | "deny"; workflowId: string; fromVersion: number } | null
  >(null);
  // Claude Code Mods band 插槽(claude-code-mods-design.md §7.1):按 sid 存的
  // band 分段数组(每 mod 一段),composer 上方渲染。会话内瞬态,本地 state
  // 即可,不进全局 store。
  const [modBands, setModBands] = useState<Record<string, ModBandGroup[]>>({});
  // $.ui.ask(§7.3):按 sid 存的待答问题,浮层渲染(见文件尾部 ModAskCard)。
  const [modAsks, setModAsks] = useState<Record<string, ModAskState | null>>({});

  // 文件/图片挂载管线（useComposerFiles）与 engine 互相依赖：engine 的 deps
  // 要 uploadOneDoc/attachOne，而它们要读 engine 的共享 ref。经 holder 打破环
  // ——engine 调用后立即填充，函数体只在调用期读取（见 useComposerFiles 注释）。
  const composerRefsHolder = useRef<{
    curSessionIdRef: { current: string | null };
    draftCacheRef: {
      current: Record<
        string,
        { input: string; attachments: Attachment[]; files?: FileAttachment[]; mentions?: ResolvedMention[] }
      >;
    };
    pendingDocsRef: { current: Array<{ file: File; tmpId: string }> };
    pendingPathsRef: { current: Array<{ path: string; tmpId: string }> };
  } | null>(null);
  const {
    imagesReading, addFiles, attachPaths, uploadOneDoc, attachOne,
  } = useComposerFiles({
    g, session, setAttachments, setFileAttachments, refs: composerRefsHolder,
  });

  const {
    activeSidRef, curSessionIdRef, storeRef, liveBySessionRef,
    socketsRef, permsRef, proposeRef, busyBySessionRef, streamAgentRef,
    seenTurnStartRef, ioSeqRef, sendSeqRef, draftCacheRef, mentionsRef,
    runsBySessionRef, pendingDocsRef, pendingPathsRef,
    syncDisplay, connectSession, waitForSocketOpen,
    dropSteer, showComposerHint, enqueueSteer, recallSteers,
    respond, stopTurn, respondPropose, answerQuestion, decideSubagentPlan,
    retryFailed, editResend, dismissFailed, retryError, retryFromCheckpoint,
    sendModUiPress, sendModUiAnswer,
  } = useChatStreamEngine({
    g, steerQ, session, onUsageChange, propose,
    input, attachments, fileAttachments,
    setMessages, setRuns, setLiveId, setWsStatus, setPermission, setPropose,
    setStreamAgent, setServerRunning, setCompacting, setInput, setAttachments, setTarget, setMenu,
    setFileAttachments, setComposerHint, setProposeResult, setModBands, setModAsks,
    stickRef, connectRef, focusLatestRef, textareaRef, sumPendingRef,
    pinToBottom, uploadOneDoc, attachOne, attemptSend, recomputeMenu,
    finishSynthesisWait,
  });
  composerRefsHolder.current = { curSessionIdRef, draftCacheRef, pendingDocsRef, pendingPathsRef };
  const sumFlow = useSummarizeFlow({
    g, session, sumPendingRef, runsBySessionRef, syncDisplay,
  });
  const {
    summarize, sumBusy, sumErr, sumCreated, sumLoading, sumSource,
    sumMenuOpen, setSumMenuOpen, sumLastN, setSumLastN, savedDraft,
    freshSummarize, openSummarize, openDraftModal, deleteDraft,
    closeSummarize, createFromSummarize, openDevFromSummarize,
  } = sumFlow;
  // summarize hook 在 engine 之后调用，而 engine 的 synthesis.event 分支要
  // 触发它的 resolver：这里包一层提升声明的桥（运行时才调用，无 TDZ 问题）。
  function finishSynthesisWait(id: string) {
    return sumFlow.finishSynthesisWait(id);
  }

  // 运行卡片操作（取消/暂停/继续/重试/删除）。
  const {
    cancelRun, pauseRun, continueRun, retryRun, retryRunFromCheckpoint, deleteRun,
  } = useRunActions({ g, curSessionIdRef, runsBySessionRef, syncDisplay });

  // 模型 chip：home 待生效模型/挂载目录 + 服务端热切。
  const {
    homeModel, homeCreatingRef, pendingMounts, setPendingMounts, resetPendingMounts,
    modelOpen, setModelOpen, pickModel, enabledProviders,
    targetAgent, homeEffective, homeModelSource, modelChipLabel,
  } = useModelPick({ g, session, target });

  // 页面刷新后的 run 卡片水合（run view 方案）：runsBySessionRef 只由
  // run.bind 推送填充，刷新后历史绑定会丢。store 的 workflowRuns 是全量
  // 列表——在该会话的条目还「不存在」时播种一次（复制数组，绝不覆盖已有
  // 条目；之后的增量仍走 engine 里 run.bind / run.event 的原地变更路径）。
  // store 首次加载完成前不播种（空列表播种会把这个会话永久钉成无绑定）。
  useEffect(() => {
    const sid = session?.id;
    if (!sid || !g.workflowRuns.length) return;
    if (runsBySessionRef.current[sid] !== undefined) return;
    runsBySessionRef.current[sid] = g.workflowRuns.filter(
      (r) => r.present_in_session_id === sid,
    );
    syncDisplay(sid);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.id, g.workflowRuns]);


  // Pre-load tool display labels from settings (cached at module level).
  // 语言加载由根部 I18nProvider 负责（原 loadPromptLang 已移除）。
  useEffect(() => { loadToolLabels(); }, []);




  const running =
    liveId !== null || !!permission || !!propose || messages.some((m) => hasPendingTool(m.blocks));
  useEffect(() => {
    onRunningChange?.(running);
  }, [running, onRunningChange]);

  // A pending ask_user card parks the turn on an interrupt exactly like a
  // permission prompt: while parked, a stray Escape must not cancel it and
  // the composer waits — the card's 跳过 is the deliberate exit.
  const questionPending = messages.some((m) =>
    m.blocks.some((b) => b.kind === "question" && b.status === "pending"),
  );
  const parked = !!permission || !!propose || questionPending;

  // The displayed session's queued steer entries, read live from the shared hook
  // (it re-renders this component whenever the queue changes).
  const steerItems = session ? steerQ.itemsFor(session.id) : [];

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
    if (stickRef.current) pinToBottom();
  }, [messages]);

  // Pin the transcript to the bottom across a few frames instead of once.
  //
  // A transcript lays out progressively — markdown tables, collapsible thinking
  // blocks, code blocks, images — so a single `scrollTop = scrollHeight` pins to
  // whatever height the first frame happened to have. Opening a session into a
  // short window then landed MID-TRANSCRIPT or even at the top (measured: the
  // same session landed at scrollTop 0 in a 900px viewport and ~186 in a 560px
  // one), and macOS overlay scrollbars give no hint that anything is above.
  //
  // stickRef flips false the moment the user scrolls away (:onScroll), which
  // aborts the loop — so this never fights someone reading back.
  // A viewport resize reflows the transcript WITHOUT a [messages] change —
  // the 12-frame pin window has long passed, so a shorter window left the
  // latest message a few px (measured: 17) or a page above the fold with
  // stickRef still true. Re-pin on resize while stuck (2026-09-25).
  useEffect(() => {
    const onResize = () => {
      if (stickRef.current) pinToBottom();
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function pinToBottom(frames = 12) {
    let left = frames;
    const attempt = () => {
      const el = scrollRef.current;
      if (!el || !stickRef.current) return;
      el.scrollTop = el.scrollHeight;
      if (--left > 0) requestAnimationFrame(attempt);
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
      if (curSessionIdRef.current === sid) pinToBottom();
    };
    window.addEventListener("ginno:focus-latest", onFocusLatest);
    return () => window.removeEventListener("ginno:focus-latest", onFocusLatest);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 子代理结果卡「去子会话纠偏」的输入框预填（P2 任务 3）。目标会话正在展示
  // 就直接进 composer；不在展示（刚点跳转、切换 effect 还没跑）就停进该会话
  // 的草稿槽——切换 effect 恢复草稿时自然带出来，文本永不丢。
  useEffect(() => {
    const onPrefill = (e: Event) => {
      const d = (e as CustomEvent<{ sessionId?: string; text?: string }>).detail;
      if (!d?.sessionId || !d.text) return;
      if (curSessionIdRef.current === d.sessionId) {
        setInput((cur) => (cur ? `${cur}\n${d.text}` : d.text!));
        requestAnimationFrame(() => textareaRef.current?.focus());
      } else {
        const prev = draftCacheRef.current[d.sessionId];
        draftCacheRef.current[d.sessionId] = {
          input: prev?.input ? `${prev.input}\n${d.text}` : d.text!,
          attachments: prev?.attachments ?? [],
          files: prev?.files ?? [],
          mentions: prev?.mentions ?? [],
        };
      }
    };
    window.addEventListener("ginno:prefill-input", onPrefill);
    return () => window.removeEventListener("ginno:prefill-input", onPrefill);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 子代理结果卡「让主对话处理」：在当前（主）会话以一条引用该结果的消息开工。
  // 忙时走 steer 队列（既有注入管线），闲时 invoke 开新 turn——与用户手打的
  // 消息完全同一条路径。agentId 置 null = 会话默认 agent。
  useEffect(() => {
    const onEscalate = (e: Event) => {
      const d = (e as CustomEvent<{ sessionId?: string; goal?: string; summary?: string }>).detail;
      const sid = curSessionIdRef.current;
      if (!sid || !d?.sessionId) return;
      const goal = d.goal || t("subtask", "子任务");
      const brief = (d.summary || "").trim().slice(0, 300);
      const text = t(
        `A subagent task "${goal}" (session ${d.sessionId}) produced results that failed human review. Verify the subagent's conclusions and decide the next step (re-split, follow up in its session, or take over yourself). Result summary: ${brief}${brief.length >= 300 ? "…" : ""}`,
        `子代理任务「${goal}」（session ${d.sessionId}）的结果经人工检查有问题。请核对该子代理的结论并决定下一步（重新拆分、在原会话追问，或自己接手）。其结果摘要：${brief}${brief.length >= 300 ? "…" : ""}`,
      );
      if (busyBySessionRef.current[sid]) {
        enqueueSteer(sid, text, null);
      } else {
        attemptSend(sid, { text, images: [], files: [], mentions: [], agentId: null });
      }
    };
    window.addEventListener("ginno:subagent-escalate", onEscalate);
    return () => window.removeEventListener("ginno:subagent-escalate", onEscalate);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** The pending ask_user card of the displayed session, if any. */
  function pendingQuestionBlock(): QuestionBlock | null {
    for (const m of storeRef.current[curSessionIdRef.current ?? ""] ?? []) {
      for (const b of m.blocks) {
        if (b.kind === "question" && b.status === "pending") return b;
      }
    }
    return null;
  }

  /** Typing at a parked card means "answer" or "change of mind" (design §3.4):
   *  the turn is waiting on the user, not working, so this never queues. */
  function handleParkedSend(sid: string, text: string, agentId: string | null) {
    if (permission) {
      // Change of mind: don't run this tool, do what I just said instead. The
      // steer is stashed FIRST (one socket is FIFO), so the resumed segment
      // absorbs it — the deny routes straight back to `agent` (graph.py), which
      // is why the parked case needs no second injection mechanism.
      enqueueSteer(sid, text, agentId);
      respond("deny");
      return;
    }
    if (propose) {
      enqueueSteer(sid, text, agentId);
      respondPropose("deny");
      return;
    }
    const q = pendingQuestionBlock();
    if (!q) return;
    if (q.allowFreeText) {
      // Answer: the typed text IS the answer. Never ALSO steer it — the model
      // would see it twice.
      answerQuestion(q.id ?? "", text, null, false);
      return;
    }
    // Fixed choices only: skip the question and steer the text instead, as the
    // instruction the user would rather give.
    enqueueSteer(sid, text, agentId);
    answerQuestion(q.id ?? "", "", null, true);
  }

  // ---- composer ↑/↓ input history (inputHistory.ts) ----------------------
  /** 懒构建该会话的输入历史(首次按 ↑/↓ 或首次发送时调用一次):种子 =
   *  store 已重建的 user 消息文本。payloadFromBlocks 对 subagent 注入/纯附件
   *  消息产出的 text 为空,seedEntries 滤空即天然排除,无需块类型黑名单。 */
  function historyFor(sid: string): HistoryState {
    const st = inputHistoryRef.current[sid];
    if (st) return st;
    const seeded = initHistory(
      seedEntries(
        (storeRef.current[sid] ?? [])
          .filter((m) => m.role === "user")
          .map((m) => m.sendPayload?.text ?? ""),
      ),
    );
    inputHistoryRef.current[sid] = seeded;
    return seeded;
  }

  function send() {
    const text = input.trim();
    const readyFiles = fileAttachments.filter((f) => !f.uploading);
    // In-flight gate, image half (pre-existing bug, both plain sends and
    // steers): readImage is async, so `attachments` lags a just-dropped image.
    // Sending now would silently drop it. Visible hint instead of a no-op.
    if (imagesReading > 0) {
      showComposerHint(tc("composer.hintImagesProcessing"));
      return;
    }
    if (!session) {
      // Home: dropped-file chips are intentionally deferred until the session
      // exists (pendingDocsRef / pendingPathsRef) — they must NOT count as
      // "upload in flight", or send dead-locks on the very uploads it triggers
      // (2026-08-19: 新建会话拖入图片永远发不出去). createAndSend flushes them.
      if (!text && attachments.length === 0 && fileAttachments.length === 0) return;
      void createAndSend({ text, images: attachments, files: readyFiles });
      return;
    }
    const sid = session.id;
    // Parked at a card: the user is answering or changing their mind, not
    // queueing (design §3.4). Checked BEFORE the busy gate because a parked
    // turn is still busy but must never receive a queue entry. (Answering is
    // deliberately not gated on attachments — it carries text only.)
    if (parked) {
      if (!text) return;
      inputHistoryRef.current[sid] = appendEntry(historyFor(sid), text);
      handleParkedSend(sid, text, target ?? session.agent_id ?? g.agents[0]?.id ?? null);
      setInput("");
      setMenu(null);
      return;
    }
    // In-flight gate, document half: a chip still uploading is not in
    // `readyFiles`, so this used to return SILENTLY — send appeared dead. Show
    // why instead (brief §3).
    if (readyFiles.length !== fileAttachments.length) {
      showComposerHint(tc("composer.hintFileUploading"));
      return;
    }
    if (busyBySessionRef.current[sid]) {
      // A turn is running → this is steering, not a new turn: queue it for the
      // next superstep of the turn that is already going (design §3.1). Text
      // AND attachments ride the steer (steer-attachments v2) — with nothing to
      // send, do nothing.
      if (!text && attachments.length === 0 && readyFiles.length === 0) return;
      inputHistoryRef.current[sid] = appendEntry(historyFor(sid), text);
      enqueueSteer(
        sid,
        text,
        target ?? session.agent_id ?? g.agents[0]?.id ?? null,
        attachments,
        readyFiles,
      );
      setInput("");
      setAttachments([]);
      setFileAttachments([]);
      setTarget(null);
      setMenu(null);
      return;
    }
    if (!text && attachments.length === 0 && readyFiles.length === 0) return;
    const agentId = target ?? session.agent_id ?? g.agents[0]?.id ?? null;
    if (agentId && agentId !== session.agent_id) g.setSessionAgent(session.id, agentId);
    // Final prune against the raw (untrimmed) input, deduped — only mentions
    // whose @kind:label token is still present are sent. The server treats this
    // structured list as authoritative (text tokens are its raw-client fallback).
    const mentions = dedupeMentions(pruneMentions(mentionsRef.current[sid] ?? [], input));
    inputHistoryRef.current[sid] = appendEntry(historyFor(sid), text);
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
              ? { ...m, turnId, status: "failed" as const, failReason: "connNotReady" }
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
              failReason: "connNotReady",
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
      ? // Retry: keep the old user bubble and its response history untouched,
        // append a NEW user bubble at the tail with the same payload, followed
        // by a fresh assistant placeholder. This preserves the conversation
        // trail instead of mutating history in place.
        ((store) => {
          const orig = store.find((m) => m.id === userMsgId);
          if (!orig) {
            // Original bubble gone — append fresh user + live
            return [
              ...store,
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
          }
          // Append new user bubble + live bubble at the tail; leave everything
          // before untouched (old turns stay in the conversation trail).
          return [
            ...store,
            { ...orig, id: uid, turnId, status: "sending" as const, failReason: undefined },
            liveBubble,
          ];
        })(storeRef.current[sid] ?? [])
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
      // Only a send that actually went out gets stamped — a turn.state answer
      // to a probe we sent earlier is older than this turn and must not judge
      // it (see probeSeqRef).
      sendSeqRef.current[sid] = ++ioSeqRef.current;
    } catch {
      // sock.send throws when the socket flipped to CLOSING/CLOSED between the
      // readyState check and here. Drop the assistant placeholder and mark the
      // user bubble failed — the payload on it keeps retry/re-edit lossless.
      busyBySessionRef.current[sid] = false;
      liveBySessionRef.current[sid] = null;
      storeRef.current[sid] = (storeRef.current[sid] ?? [])
        .filter((m) => m.id !== live)
        .map((m) =>
          m.id === uid ? { ...m, status: "failed" as const, failReason: "connLost" } : m,
        );
      syncDisplay(sid);
    }
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

  const isHome = !session;

  // Home autofocus: the composer remounts on the home slot (keyed), so focus
  // must be re-armed after the transition.
  useEffect(() => {
    if (!session) textareaRef.current?.focus();
  }, [session]);

  // 拖文件进输入框（code 工作区树 → composer）：消费 store 的一次性插入请求，
  // 在光标（textarea 未聚焦时 selectionStart 仍保留上次位置）处插入路径文本，
  // 前面非空白时补一个分隔空格，然后把焦点和光标落到插入文本之后。
  const composerInsert = g.composerInsert;
  useEffect(() => {
    if (!composerInsert) return;
    g.clearComposerInsert();
    const el = textareaRef.current;
    const caret = el?.selectionStart ?? input.length;
    const before = input.slice(0, caret);
    const sep = before && !/\s$/.test(before) ? " " : "";
    const text = `${before}${sep}${composerInsert.text}${input.slice(caret)}`;
    const newCaret = caret + sep.length + composerInsert.text.length;
    setInput(text);
    // 与 onChange 同步逻辑对齐：插入可能打断已解析的 mention token（拖到
    // 半个 `@agent:xx` 中间），prune 掉不再出现的条目。
    if (session) {
      mentionsRef.current[session.id] = pruneMentions(
        mentionsRef.current[session.id] ?? [],
        text,
      );
    }
    requestAnimationFrame(() => {
      const el2 = textareaRef.current;
      if (el2) {
        el2.focus();
        el2.setSelectionRange(newCaret, newCaret);
      }
      recomputeMenu(text);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [composerInsert]);

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
    // artifact 菜单源 = 本会话条目在前 + 项目内其他会话条目（新会话也能
    // @ 到历史产物；去重按 id，选中走结构化 mention 精确解析）。
    const ownIds = new Set(g.artifacts.map((a) => a.id));
    const others = g.projectArtifacts.filter(
      (a) => !ownIds.has(a.id) && (!a.session_id || a.session_id !== session?.id),
    );
    const items = buildMenuItems(trigger, {
      skills: g.skills,
      agents: g.agents,
      workflows: g.workflows,
      artifacts: [...g.artifacts, ...others],
      sessionId: session?.id ?? null,
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
      const s = await g.newSession(agentId, {
        ...(homeModel ?? {}),
        // 出生即挂载：pending 目录随建会话请求提交(后端 create_session 原生支持)
        context_folders: pendingMounts.ids.length ? pendingMounts.ids : undefined,
        primary_folder: pendingMounts.primary ?? undefined,
      });
      if (!s) return; // sessionError banner carries the reason; composer keeps text
      curSessionIdRef.current = s.id;
      connectSession(s.id);
      const docs = pendingDocsRef.current;
      pendingDocsRef.current = [];
      const natives = pendingPathsRef.current;
      pendingPathsRef.current = [];
      // Collect the resolved entries so they ride in the turn payload — the
      // fileAttachments state update below hasn't rendered yet in this closure.
      const flushed: FileAttachment[] = [];
      for (const d of docs) {
        const e = await uploadOneDoc(s.id, d.file, d.tmpId);
        if (e) flushed.push(e);
      }
      for (const n of natives) {
        const e = await attachOne(s.id, n.path, n.tmpId);
        if (e) flushed.push(e);
      }
      // The socket race fix: await open instead of failing fast. Timeout
      // degrades to attemptSend's retryable failed bubble.
      await waitForSocketOpen(s.id);
      attemptSend(s.id, {
        ...payload,
        files: [...payload.files, ...flushed],
        mentions: [],
        agentId,
      });
      setInput("");
      setAttachments([]);
      setFileAttachments([]);
      setTarget(null);
      setMenu(null);
      // 下一个新会话按库里的 auto_mount 条目重新预填(手动改动不跨会话残留)。
      void resetPendingMounts();
    } finally {
      homeCreatingRef.current = false;
    }
  }

  // Composer box extracted so the landing home can center it. Same single
  // instance; the two keyed slots force a clean remount on home↔session
  // transitions (all composer state lives on ChatStream — only DOM focus is
  // lost, and home autofocuses above).
  const hideDel = session?.type === "delegation"; // 回放会话隐藏 goal 条等会话级控件
        const composerBoxEl = (
          <div
            ref={composerBoxRef}
            /* 树拖拽（pointer events）用 elementFromPoint 识别这里为放置目标 */
            data-composer="1"
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
              dragOver || g.composerDragOver ? "border-violet/70 ring-2 ring-violet/30" : "border-line"
            }`}
          >
            <div
              onPointerDown={onResizeStart}
              title={tc("composer.resizeTitle")}
              onDoubleClick={() => setComposerH(undefined)}
              className="absolute -top-1 left-1/2 z-10 flex h-2 w-12 -translate-x-1/2 cursor-ns-resize items-center justify-center rounded-full hover:bg-line2/60"
              aria-label={tc("composer.resizeAria")}
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
            {session && (modBands[session.id]?.some((g) => g.tree.length > 0) ?? false) && (
              // Claude Code Mods band 插槽(claude-code-mods-design.md §7.1):
              // composerHint 之上、与 hint/steer 条同级;每 mod 一段。空树零渲染。
              <div className="mb-2">
                <ModBand
                  groups={modBands[session.id]!}
                  onPress={(actionId, generation) => sendModUiPress(session.id, generation, actionId)}
                />
              </div>
            )}
            {composerHint && (
              // Blocked send, made visible. A silent return here read as a
              // dead send button (the original bug report).
              <div className="mb-2 flex items-center gap-1.5 rounded-lg border border-yellow/40 bg-yellow/10 px-2.5 py-1.5 text-xs text-yellow">
                <AlertCircle className="h-3.5 w-3.5 shrink-0" />
                <span>{composerHint}</span>
              </div>
            )}
            {imagesReading > 0 && <ImagesReadingRow />}
            <ComposerAttachmentRows
              attachments={attachments}
              fileAttachments={fileAttachments}
              uploadingLabel={session ? tc("composer.uploading") : tc("composer.uploadsOnSend")}
              onRemoveImage={(i) => setAttachments((l) => l.filter((_, j) => j !== i))}
              onRemoveFile={(i) => setFileAttachments((l) => l.filter((_, j) => j !== i))}
            />
            {session?.type === "subagent" && running && (
              // 子任务视图的输入语义（subagent-design.md §6.2）：running 时的输入
              // 走既有 steering 通道（useSteerQueue，同主对话），这里只把语义标出来。
              <div className="mb-1 flex items-center gap-1.5 px-0.5 text-[10px] text-faint">
                <RotateCcw className="h-3 w-3 shrink-0 animate-pulse" aria-hidden />
                <span>{tc("composer.subtaskSteerHint")}</span>
              </div>
            )}
            {session && steerItems.length > 0 && (
              <QueueBar items={steerItems} sessionId={session.id} onDrop={dropSteer} />
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
                // Esc while a turn runs = stop it (same as the ⏹ button);
                // only when the composer is focused and nothing is parked —
                // a parked prompt's own card (Deny / 跳过) is the deliberate
                // exit, a stray Escape must not cancel the whole turn.
                // ↑ on an EMPTY composer takes the queued steer entries back for editing —
                // one per line, in order (Claude Code's "take back what you
                // queued"). Guarded on empty input so it never steals the caret
                // when there is text to navigate.
                if (e.key === "ArrowUp" && !composing && !input && session && steerItems.length > 0) {
                  e.preventDefault();
                  recallSteers(session.id);
                  return;
                }
                // ↑/↓ input history recall (shell-style): only when the caret
                // sits on the first (↑) / last (↓) line — multi-line editing
                // keeps native caret moves. Priority: IME > command menu >
                // steer recall (above) > this. Nothing to navigate → fall
                // through to native behavior (no preventDefault).
                if (!composing && session && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
                  const el = textareaRef.current;
                  const caret = el?.selectionStart ?? input.length;
                  const base = historyFor(session.id);
                  const r =
                    e.key === "ArrowUp"
                      ? caretOnFirstLine(input, caret)
                        ? navUp(base, input)
                        : null
                      : caretOnLastLine(input, caret)
                        ? navDown(base, input)
                        : null;
                  if (r) {
                    e.preventDefault();
                    inputHistoryRef.current[session.id] = r.state;
                    setInput(r.text);
                    setMenu(null); // 程序化回填后菜单必须关(与 pickItem 一致)
                    requestAnimationFrame(() => {
                      const el2 = textareaRef.current;
                      if (el2) {
                        el2.focus();
                        el2.setSelectionRange(r.text.length, r.text.length);
                      }
                    });
                    return;
                  }
                }
                if (e.key === "Escape" && !composing && running && !parked) {
                  e.preventDefault();
                  stopTurn();
                  return;
                }
                if (e.key === "Enter" && !e.shiftKey && !composing) {
                  e.preventDefault();
                  send();
                }
              }}
              rows={2}
              placeholder={tc("composer.placeholder")}
              style={
                composerH != null
                  ? { height: composerH - 56, minHeight: 44, overflowY: "auto" }
                  : { maxHeight: 240 }
              }
              className="w-full resize-none bg-transparent px-1.5 py-1 text-sm text-txt outline-none placeholder:text-faint"
            />
            <div className="flex flex-wrap items-center justify-between gap-1 px-1 pt-1">
              <div className="flex min-w-0 items-center gap-1 text-faint">
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
                  title={tc("composer.addAttachmentsTitle")}
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
                  title={tc("composer.slashTitle")}
                  aria-label={tc("composer.slashAria")}
                >
                  <Keyboard className="h-4 w-4" />
                </button>
                {!isHome && (
                  <ConnStatusChip
                    wsStatus={wsStatus}
                    onReconnect={() => connectRef.current()}
                  />
                )}
              </div>
              <div className="ml-auto flex shrink-0 items-center gap-1.5">
              {/* 首页挂载 chip(home-mount-picker-design.md §2.1)：操作的是「首条
                  消息将生效」的 pending 列表；会话内由 TopBar 的 live chip 负责。 */}
              {!session && (
                <ContextFoldersChip
                  ids={pendingMounts.ids}
                  primary={pendingMounts.primary}
                  onChange={(ids, primary) => setPendingMounts({ ids, primary })}
                />
              )}
              {running && goalActive && session?.type !== "delegation" && (
                <button
                  onClick={() => void g.setGoalStatus(session!.id, "paused")}
                  title={tc("goal.pauseTitle")}
                  aria-label={tc("goal.pauseAria")}
                  className="flex h-8 w-8 items-center justify-center rounded-lg border border-line2 text-muted hover:text-txt"
                >
                  <Square className="h-3.5 w-3.5" />
                </button>
              )}
              {/* model chip: per-session provider/model switch (home: pick for
                  the session that first send will create). Enabled while a
                  turn runs — the switch is in-place server-side and takes
                  effect at the next LLM call (model-assignment-design.md
                  §3.2). */}
              <ModelChipMenu
                session={session}
                label={modelChipLabel}
                homeModelSource={homeModelSource}
                targetAgent={targetAgent}
                homeEffective={homeEffective}
                enabledProviders={enabledProviders}
                open={modelOpen}
                onToggle={() => setModelOpen((v) => !v)}
                onClose={() => setModelOpen(false)}
                onPick={(pid, m) => void pickModel(pid, m)}
              />
              {running && session && !parked ? (
                <button
                  onClick={stopTurn}
                  title={tc("composer.stopTitle")}
                  aria-label={tc("composer.stopAria")}
                  className="flex h-8 w-8 items-center justify-center rounded-lg bg-red text-white transition-opacity hover:opacity-90"
                >
                  <Square className="h-3.5 w-3.5" fill="currentColor" />
                </button>
              ) : (
              <button
                onClick={send}
                disabled={
                  parked ||
                  running ||
                  // In-session uploads run immediately and gate the send; on
                  // home they're deferred until lazy creation, so they must
                  // not disable the button (same deadlock as send()'s guard).
                  (!!session && fileAttachments.some((f) => f.uploading)) ||
                  (!input.trim() && attachments.length === 0 && fileAttachments.length === 0)
                }
                className="flex h-8 w-8 items-center justify-center rounded-lg bg-violet text-white transition-opacity hover:opacity-90 disabled:opacity-40"
              >
                <ArrowUp className="h-4 w-4" />
              </button>
              )}
              </div>
            </div>
          </div>
  );

  // C+ 方案①/④：chip「推荐」标签由草稿关键词派生（render 期，同 sel 模式）；
  // 切换预告条在 target 指向另一个 agent 时出现——发送后 target 自动清零，
  // 无需额外清理。agentChipsEl 抽成变量：composerBoxEl 是单实例双 key 槽，
  // chip 行不能挪进去（会把 session-only 按钮拖进 home），两槽各渲染一份。
  const recAgentId = recommendAgentId(input, new Set(g.agents.map((a) => a.id)));
  const switchTarget =
    target && session && target !== session.agent_id ? agentById(target) : null;

  const agentChipsEl = (
    <>
      {g.agents.map((a) => {
        const sel = (target ?? session?.agent_id) === a.id;
        const hex = agentHex(a.color);
        const rec = recAgentId === a.id && !sel;
        return (
          <button
            key={a.id}
            onClick={() => setTarget(sel ? null : a.id)}
            className={
              sel
                ? "flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs transition-colors"
                : "flex items-center gap-1.5 rounded-lg border border-line bg-card px-2.5 py-1 text-xs text-muted transition-colors hover:border-line2 hover:text-txt"
            }
            style={sel ? { borderColor: hex, background: hex + "1a", color: hex } : undefined}
          >
            <Icon name={a.icon} className="h-3.5 w-3.5" />
            {tc("agentChips.ask", { name: a.name })}
            {rec && (
              <span className="rounded-full border border-yellow/40 bg-yellow/10 px-1.5 text-[10px] leading-4 text-yellow">
                {tc("agentChips.recommended")}
              </span>
            )}
          </button>
        );
      })}
    </>
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
            {tc("home.tagline")}
          </div>
          <div key="composer-home" className="mt-8 w-full max-w-[760px]">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              {agentChipsEl}
              {!target && (
                <span className="ml-auto self-center text-[11px] text-faint">
                  {tc("home.defaultAgentHint", { name: g.agents[0]?.name ?? tc("stream.fallbackAgent") })}
                </span>
              )}
            </div>
            {composerBoxEl}
          </div>
          <div className="mt-6 flex max-w-[820px] flex-wrap justify-center gap-2.5">
            {[
              { icon: "📊", label: tc("home.chipExcelLabel"), fill: tc("home.chipExcelFill") },
              { icon: "🔁", label: tc("home.chipWorkflowLabel"), fill: tc("home.chipWorkflowFill") },
              { icon: "🧠", label: tc("home.chipMemoryLabel"), fill: tc("home.chipMemoryFill") },
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
            <button
              onClick={() => onOpenGoal?.()}
              className="inline-flex items-center gap-2 rounded-full border border-line px-4 py-2 text-[12.5px] text-muted transition-colors hover:border-line2 hover:bg-card hover:text-txt"
            >
              <span>🎯</span>
              {tc("home.setGoal")}
            </button>
          </div>
          <div className="mt-6 text-[11px] text-faint">
            {tc("home.hintLine")}
          </div>
        </div>
      ) : (
        <>
      {session.type === "subagent" && <SubagentTopBar session={session} />}
      {session.type !== "subagent" && <PlanBoard sessionId={session.id} />}
      {session.type !== "subagent" && <SubagentBoard sessionId={session.id} />}
      {!hideDel && goal && goalStalled && !resumeDismissed && (
        <div className="mx-auto mb-2 flex w-full max-w-3xl items-center gap-2 rounded-lg border border-line2 bg-card px-3 py-2 text-xs">
          <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: "#f97316" }} />
          <span className="flex-1 text-muted">
            {tc("goal.stalledLabel", { status: goal.status })}:
            <span className="text-txt">{goal.objective}</span>
          </span>
          <button
            onClick={() => void g.setGoalStatus(session!.id, "active")}
            className="rounded-md bg-violet px-2 py-1 text-[11px] font-medium text-white hover:opacity-90"
          >
            {tc("goal.resume")}
          </button>
          <button
            onClick={() => setResumeDismissed(true)}
            aria-label={tc("goal.dismissAria")}
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
        className="flex-1 overflow-y-auto px-6 py-6"
      >
        <div className="mx-auto flex max-w-3xl flex-col gap-5">
          {messages.length === 0 && (
            <div className="py-16 text-center text-sm text-faint">
              {tc("empty.start")}
            </div>
          )}

          <TranscriptList
            messages={messages}
            session={session}
            liveId={liveId}
            running={running}
            compacting={compacting}
            serverRunning={serverRunning}
            decideSubagentPlan={decideSubagentPlan}
            answerQuestion={answerQuestion}
            editResend={editResend}
            dismissFailed={dismissFailed}
            retryFailed={retryFailed}
            retryError={retryError}
            retryFromCheckpoint={retryFromCheckpoint}
          />

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
          title={tc("confirmDeleteRun.title")}
          message={tc("confirmDeleteRun.message")}
          confirmLabel={tc("confirmDeleteRun.confirm")}
          onConfirm={() => {
            const id = confirmDelRun;
            setConfirmDelRun(null);
            deleteRun(id);
          }}
          onCancel={() => setConfirmDelRun(null)}
        />
      )}

      {permission && (
        <PermissionPromptCard permission={permission} onRespond={respond} />
      )}

      {propose && <ProposeCard propose={propose} onDecide={respondPropose} />}

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
              ? tc("propose.appliedReceipt", { id: proposeResult.workflowId, version: proposeResult.fromVersion })
              : tc("propose.rejectedReceipt", { id: proposeResult.workflowId })}
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
      <div className="px-6 pb-5 pt-2">
        <div className="mx-auto max-w-3xl">
          {switchTarget && (
            <div className="mb-2 flex items-center gap-2 rounded-lg border border-line2 bg-card px-3 py-2 text-xs">
              <span
                className="h-1.5 w-1.5 shrink-0 rounded-full"
                style={{ background: agentHex(switchTarget.color) }}
              />
              <span className="flex-1 text-muted">
                {tc("agentChips.switchPrefix")}{" "}
                <span className="font-medium" style={{ color: agentHex(switchTarget.color) }}>
                  {switchTarget.name}
                </span>{" "}
                {tc("agentChips.switchSuffix")}
              </span>
              <button
                onClick={() => setTarget(null)}
                className="shrink-0 rounded-md border border-line2 px-2 py-0.5 text-[11px] text-muted transition-colors hover:text-txt"
              >
                {tc("agentChips.undo")}
              </button>
            </div>
          )}
          <div className="mb-2 flex flex-wrap gap-2">
            {/* 回放会话（delegation）不显示 agent 切换 chips——静态档案无谓的噪音 */}
            {session?.type !== "delegation" && agentChipsEl}
            <button
              onClick={() => g.setActiveSession(null)}
              className="flex items-center gap-1.5 rounded-lg border border-line bg-card px-2.5 py-1 text-xs text-muted hover:text-txt"
            >
              {tc("composer.newSession")}
            </button>
            {/* S1/S5: summarize entry — session picker + trace range (last N
                messages) live in one dropdown. */}
            <SummarizeMenu
              sessions={g.sessions}
              currentSessionId={session?.id}
              open={sumMenuOpen}
              onToggle={() => setSumMenuOpen((v) => !v)}
              loading={sumLoading}
              savedDraft={savedDraft}
              onOpenDraftModal={openDraftModal}
              onDeleteDraft={deleteDraft}
              onPickSession={(sid) => void openSummarize(sid)}
              lastN={sumLastN}
              onLastN={setSumLastN}
            />
          </div>

          <div key="composer-session">
              {session?.type === "delegation" ? (
                <DelegationStatusBar session={session} />
              ) : (
                composerBoxEl
              )}
            </div>
        </div>
      </div>
        </>
      )}
      {session && modAsks[session.id] && (
        // $.ui.ask 浮层(claude-code-mods-design.md §7.3):mod 的提问独立于
        // turn(interrupt),session 切走即不可见;回答经 engine 回发后乐观清卡。
        <ModAskCard
          ask={modAsks[session.id]!}
          onAnswer={(value) => sendModUiAnswer(session.id, modAsks[session.id]!.id, value)}
        />
      )}
    </div>
  );
}
