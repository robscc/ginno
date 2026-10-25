"use client";

import { useEffect, useRef, useState } from "react";
import { X, Check } from "lucide-react";
import { useGinno } from "@/lib/store";
import { useSteerQueue } from "@/lib/steerQueue";
import { loadToolLabels } from "@/lib/toolLabels";
import * as api from "@/lib/runtime";
import { pushNotice } from "@/lib/notice";
import { useTranslations } from "next-intl";
import { agentHex } from "@/lib/theme";
import { hasPendingTool, type Block } from "@/components/chat/blocks";
import { LiveRunBlock } from "./RunBlocks";
import { SummarizeModal } from "./SummarizeModal";
import { ConfirmModal } from "@/components/ConfirmModal";
import {
  applySelection,
  buildMenuItems,
  detectTrigger,
  type MenuItem,
  type ResolvedMention,
  type Trigger,
} from "@/components/chat/commandMenu";
import type { WorkflowRun } from "@/lib/types";
import type { SessionMeta, SessionUsage } from "@/lib/types";
import {
  mid,
  newTurnId,
  type Attachment,
  type ChatMsg,
  type FileAttachment,
  type PermissionPrompt,
  type SendPayload,
  type VersionPropose,
} from "./streamCore";
import { CHAT_COL } from "./columnWidth";
import { ProposeCard, SubagentTopBar } from "./streamCards";
import { PlanBoard } from "./PlanBoard";
import { SubagentBoard } from "./SubagentBoard";
import { useChatStreamEngine, HOME_SLOT } from "./useChatStreamEngine";
import { useComposerFiles } from "./useComposerFiles";
import { useRunActions } from "./useRunActions";
import { useModelPick } from "./useModelPick";
import { useSummarizeFlow } from "./useSummarizeFlow";
import { ModAskCard, type ModAskState } from "./mod/ModAskCard";
import type { ModBandGroup } from "./mod/ModBand";
import { DelegationStatusBar } from "./stream/DelegationStatusBar";
import { PermissionPromptCard } from "./stream/PermissionPromptCard";
import { SummarizeMenu } from "./stream/SummarizeMenu";
import { TranscriptList } from "./stream/TranscriptList";
import { Composer } from "./stream/Composer";
import { AgentChips } from "./stream/AgentChips";
import { HomeView } from "./stream/HomeView";
import { useComposerResize } from "./stream/useComposerResize";
import { useComposerSend } from "./stream/useComposerSend";
import { useSuggestions } from "./stream/useSuggestions";
import { useFirstRun } from "./stream/useFirstRun";
import { useChatEventBridge } from "./stream/useChatEventBridge";
import { SessionSuggestions } from "./SessionSuggestions";
import { DistillMenu, type DistillKind } from "./DistillMenu";

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
  // 「沉淀为…」 request in flight.
  const [distillBusy, setDistillBusy] = useState(false);

  // Composer 高度：undefined = 自适应（有上限）；数字 = 用户拖拽后的固定高度。
  const { composerH, setComposerH, onResizeStart } = useComposerResize({ composerBoxRef });

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
  const modelPick = useModelPick({ g, session, target });
  const {
    homeModel, homeCreatingRef, pendingMounts, setPendingMounts, resetPendingMounts,
    modelOpen, setModelOpen, pickModel, enabledProviders,
    targetAgent, homeEffective, homeModelSource, modelChipLabel,
  } = modelPick;

  // V2 P2-3 首启引导（仅首页、且还没有任何会话）。
  const {
    showFirstRun, firstRunBusy, dismissFirstRun, firstRunAsk,
    firstRunWorkflow, firstRunMountFolder,
  } = useFirstRun({ g, session, textareaRef, homeModel, setPendingMounts });

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

  // V2 P2-1 execution-time suggestions（状态/加载/动作在 useSuggestions 内）。
  const { suggestBusy, visibleSuggestions, dismissSuggestion, runSuggestionAction } =
    useSuggestions({
      g, session, running, target, attemptSend, showComposerHint, setModelOpen, reportDistill,
    });

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

  // Composer 发送管线（输入历史 / parked 分派 / steering 排队 / 首页懒建）。
  const { send, historyFor, inputHistoryRef } = useComposerSend({
    g, session, input, setInput, attachments, setAttachments,
    fileAttachments, setFileAttachments, target, setTarget, setMenu,
    parked, permission, propose, imagesReading, showComposerHint, attemptSend,
    storeRef, busyBySessionRef, mentionsRef, enqueueSteer, respond, respondPropose,
    answerQuestion, homeCreatingRef, homeModel, pendingMounts, resetPendingMounts,
    curSessionIdRef, connectSession, pendingDocsRef, pendingPathsRef,
    uploadOneDoc, attachOne, waitForSocketOpen,
  });

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
  //
  // window 监听、通知跳转、草稿预填、mods 填充、子代理升级、首页聚焦与
  // composerInsert 消费都在 useChatEventBridge 内（效果/依赖逐字保留）。
  useChatEventBridge({
    focusLatestRef, stickRef, curSessionIdRef, draftCacheRef, mentionsRef,
    busyBySessionRef, textareaRef, session, input, setInput, pinToBottom,
    enqueueSteer, attemptSend,
    composerInsert: g.composerInsert, clearComposerInsert: g.clearComposerInsert,
    recomputeMenu,
  });

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


  /** Shared success/error surfacing for both distill entry points. The API
   *  returns `{detail:{error}}` on 4xx/5xx (FastAPI HTTPException body). */
  function reportDistill(
    r: { ok?: boolean; path?: string; detail?: { error?: string } },
    kind: "skill" | "kb",
  ) {
    if (r?.ok && r.path) {
      pushNotice(
        "success",
        tc(kind === "kb" ? "distill.kbDone" : "distill.skillDone", { path: r.path }),
        { source: "general", duration: 8000 },
      );
      return;
    }
    pushNotice("error", r?.detail?.error || tc("distill.failed"), { source: "general" });
  }

  /** Skill / KB distillation (P1-2). Skill is deterministic server-side; KB
   *  fails cleanly when no knowledge vault is configured. */
  async function distillSessionTo(kind: "skill" | "kb") {
    const sid = session?.id;
    if (!sid || distillBusy) return;
    setDistillBusy(true);
    try {
      reportDistill(await api.distillSession(sid, kind), kind);
    } catch {
      pushNotice("error", tc("distill.failed"), { source: "general" });
    } finally {
      setDistillBusy(false);
    }
  }

  /** 「沉淀为…」 picker: workflow reuses the existing summarize-from-session
   *  flow (draft modal + create/run), not a separate implementation. */
  function pickDistill(kind: DistillKind) {
    if (kind === "workflow") {
      if (session) void openSummarize(session.id);
      return;
    }
    void distillSessionTo(kind);
  }

  const agentById = (id?: string | null) => g.agents.find((a) => a.id === id) ?? null;

  const isHome = !session;

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
    const team = session?.type === "team" ? g.teams.find((t) => t.main_session_id === session.id) : undefined;
    const teamMembers = team?.member_rows;
    // @ 产物可见范围（独立空间）：团队主会话只见本团队产物——主会话 + 全部
    // 成员会话（含多会话）；无 session 归属的项目级产物保留。其他会话不变。
    const teamSessionIds = new Set<string>([
      team?.main_session_id ?? "",
      ...(team?.member_rows ?? []).flatMap((r) => r.session_ids?.length ? r.session_ids : [r.session_id]),
    ].filter(Boolean));
    const inTeamScope = (a: { session_id?: string | null }) =>
      !team || !a.session_id || teamSessionIds.has(a.session_id);
    const items = buildMenuItems(trigger, {
      skills: g.skills,
      agents: g.agents,
      workflows: g.workflows,
      artifacts: [...g.artifacts, ...others].filter(inTeamScope),
      sessionId: session?.id ?? null,
      teamMembers,
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

  // Composer box extracted so the landing home can center it. Same single
  // instance; the two keyed slots force a clean remount on home↔session
  // transitions (all composer state lives on ChatStream — only DOM focus is
  // lost, and home autofocuses above).
  const hideDel = session?.type === "delegation"; // 回放会话隐藏 goal 条等会话级控件
  const composerBoxEl = (
    <Composer
      g={g}
      session={session}
      isHome={isHome}
      running={running}
      parked={parked}
      wsStatus={wsStatus}
      onReconnect={() => connectRef.current()}
      composerHint={composerHint}
      imagesReading={imagesReading}
      steerItems={steerItems}
      dropSteer={dropSteer}
      recallSteers={recallSteers}
      composerBoxRef={composerBoxRef}
      textareaRef={textareaRef}
      fileRef={fileRef}
      composerH={composerH}
      setComposerH={setComposerH}
      onResizeStart={onResizeStart}
      dragOver={dragOver}
      setDragOver={setDragOver}
      addFiles={addFiles}
      input={input}
      setInput={setInput}
      attachments={attachments}
      setAttachments={setAttachments}
      fileAttachments={fileAttachments}
      setFileAttachments={setFileAttachments}
      menu={menu}
      setMenu={setMenu}
      pickItem={pickItem}
      recomputeMenu={recomputeMenu}
      mentionsRef={mentionsRef}
      historyFor={historyFor}
      inputHistoryRef={inputHistoryRef}
      send={send}
      stopTurn={stopTurn}
      goalActive={goalActive}
      modBands={session ? modBands[session.id] : undefined}
      onModUiPress={sendModUiPress}
      model={modelPick}
    />
  );

  // C+ 方案①/④：chip「推荐」标签由草稿关键词派生（render 期，同 sel 模式）；
  // 切换预告条在 target 指向另一个 agent 时出现——发送后 target 自动清零，
  // 无需额外清理。agentChipsEl 抽成变量：composerBoxEl 是单实例双 key 槽，
  // chip 行不能挪进去（会把 session-only 按钮拖进 home），两槽各渲染一份。
  const switchTarget =
    target && session && target !== session.agent_id ? agentById(target) : null;

  const agentChipsEl = (
    <AgentChips
      g={g}
      session={session}
      target={target}
      input={input}
      onSelect={setTarget}
    />
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {isHome ? (
        <HomeView
          agents={g.agents}
          target={target}
          agentChips={agentChipsEl}
          composer={composerBoxEl}
          showFirstRun={showFirstRun}
          firstRunBusy={firstRunBusy}
          onMountFolder={() => void firstRunMountFolder()}
          onAsk={firstRunAsk}
          onWorkflow={firstRunWorkflow}
          onDismiss={dismissFirstRun}
          onChipFill={(fill) => {
            setInput(fill);
            requestAnimationFrame(() => textareaRef.current?.focus());
          }}
          onOpenGoal={onOpenGoal}
        />
      ) : (
        <>
      {session.type === "subagent" && <SubagentTopBar session={session} />}
      {session.type !== "subagent" && <PlanBoard sessionId={session.id} />}
      {session.type !== "subagent" && <SubagentBoard sessionId={session.id} />}
      {/* V2 P1-2 「沉淀为…」: turn this session into a reusable skill / KB card
          / workflow. Kept out of the subagent and delegation (read-only) views. */}
      {session.type !== "subagent" && session.type !== "delegation" && (
        <div className={`${CHAT_COL} mb-1 flex justify-end`}>
          <DistillMenu busy={distillBusy} onPick={pickDistill} />
        </div>
      )}
      {!hideDel && goal && goalStalled && !resumeDismissed && (
        <div className={`${CHAT_COL} mb-2 flex items-center gap-2 rounded-lg border border-line2 bg-card px-3 py-2 text-xs`}>
          <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: "#f97316" }} />
          <span className="selectable flex-1 text-muted">
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
        <div className={`${CHAT_COL} flex flex-col gap-5`}>
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
        <div className={`${CHAT_COL} px-6`}>
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
        <div className={CHAT_COL}>
          {/* V2 P2-1: execution-time suggestions sit directly above the
              composer; hidden while a turn runs (their actions would race it). */}
          {session && !running && visibleSuggestions.length > 0 && (
            <SessionSuggestions
              suggestions={visibleSuggestions}
              busyId={suggestBusy}
              onAction={(s) => void runSuggestionAction(s)}
              onDismiss={dismissSuggestion}
            />
          )}
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
