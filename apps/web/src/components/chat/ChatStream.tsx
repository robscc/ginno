"use client";

import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { Paperclip, Keyboard, ArrowUp, X, AlertCircle, Loader2, Square, Zap, ChevronDown, Check, RotateCcw, Globe } from "lucide-react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import { debugLog, attachFilePath } from "@/lib/runtime";
import { useSteerQueue } from "@/lib/steerQueue";
import {
  readImage,
  uploadDoc,
  isImageFile,
} from "@/lib/composerAttachments";
import { loadToolLabels } from "@/lib/toolLabels";
import { agentHex } from "@/lib/theme";
import { greeting, relTime } from "@/lib/utils";
import { Icon } from "@/components/icons";
import { ContextBlocks, SteerBand, SubagentBlocks, SubagentGroupCard, UserBlocks, hasPendingTool, type Block, type QuestionBlock } from "@/components/chat/blocks";
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
import {
  cancelWorkflowRun,
  decideWorkflowRun,
  deleteWorkflowRun,
  pauseWorkflowRun,
  retryWorkflowRun,
  retryWorkflowRunFromCheckpoint,
} from "@/lib/runtime";
import type { WorkflowRun } from "@/lib/types";
import type { SessionMeta, SessionUsage } from "@/lib/types";
import {
  foldConsecutiveSpawnCards,
  mid,
  newTurnId,
  recommendAgentId,
  TABLE_KINDS,
  type Attachment,
  type ChatMsg,
  type FileAttachment,
  type PermissionPrompt,
  type SendPayload,
  type VersionPropose,
} from "./streamCore";
import {
  AssistantBubble,
  ErrorCard,
  HandoffDivider,
  ProposeCard,
  SubagentTopBar,
  TurnIdChip,
} from "./streamCards";
import { useChatStreamEngine } from "./useChatStreamEngine";
import { useSummarizeFlow } from "./useSummarizeFlow";
import { SubagentPlanCard } from "./subagentPlanCard";

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
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  // 连续的子代理发起卡并成一张委派卡（2026-10-01 空间优化）：并行委派 3 个
  // 子代理时，主对话原本出现 3 张同构卡，把主 agent 的内容挤下去。
  const spawnFold = useMemo(() => foldConsecutiveSpawnCards(messages), [messages]);
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
  const [wsStatus, setWsStatus] = useState<"connecting" | "live" | "reconnecting" | "offline">("connecting");
  // Composer height: undefined = auto-grow (capped); a number = user-dragged size.
  const [composerH, setComposerH] = useState<number | undefined>(undefined);
  // Command/mention autocomplete: open menu (items + active index + trigger).
  const [menu, setMenu] = useState<{ items: MenuItem[]; active: number; trigger: Trigger } | null>(null);
  // Mid-turn steering queue (docs/steering-design.md). Shared with the floating
  // quick-chat window; the hook owns the queue + acks, this component renders
  // it. `steerItems` (below) is read during render.
  const steerQ = useSteerQueue();
  // Images currently being read/compressed by readImage. The Attachment only
  // reaches `attachments` when that async read resolves, so a send fired in the
  // meantime would silently drop the picture. This counter gates the send.
  const [imagesReading, setImagesReading] = useState(0);
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

  const {
    activeSidRef, curSessionIdRef, storeRef, liveBySessionRef,
    socketsRef, permsRef, proposeRef, busyBySessionRef, streamAgentRef,
    seenTurnStartRef, ioSeqRef, sendSeqRef, draftCacheRef, mentionsRef,
    runsBySessionRef, pendingDocsRef, pendingPathsRef,
    syncDisplay, connectSession, cycleSessionSocket, waitForSocketOpen,
    dropSteer, showComposerHint, enqueueSteer, recallSteers,
    respond, stopTurn, respondPropose, answerQuestion, decideSubagentPlan,
    retryFailed, editResend, dismissFailed, retryError, retryFromCheckpoint,
  } = useChatStreamEngine({
    g, steerQ, session, onUsageChange, propose,
    input, attachments, fileAttachments,
    setMessages, setRuns, setLiveId, setWsStatus, setPermission, setPropose,
    setStreamAgent, setServerRunning, setInput, setAttachments, setTarget, setMenu,
    setFileAttachments, setComposerHint, setProposeResult,
    stickRef, connectRef, focusLatestRef, textareaRef, sumPendingRef,
    pinToBottom, uploadOneDoc, attachOne, attemptSend, recomputeMenu,
    finishSynthesisWait,
  });
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


  // Pre-load tool display labels from settings (cached at module level).
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
      const goal = d.goal || "子任务";
      const brief = (d.summary || "").trim().slice(0, 300);
      const text = `子代理任务「${goal}」（session ${d.sessionId}）的结果经人工检查有问题。请核对该子代理的结论并决定下一步（重新拆分、在原会话追问，或自己接手）。其结果摘要：${brief}${brief.length >= 300 ? "…" : ""}`;
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




  async function uploadOneDoc(sid: string, f: File, tmpId: string): Promise<FileAttachment | null> {
    try {
      // The upload + response telemetry live in the shared composerAttachments
      // module (used by the floating window too); it throws on failure.
      const entry = await uploadDoc(sid, f);
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
      return { id: entry.id, name: entry.name, path: entry.path, kind: entry.kind };
    } catch (e) {
      void debugLog({ where: "addFiles:upload-error", name: f.name, error: String(e) });
      setFileAttachments((a) => a.filter((x) => x.id !== tmpId));
      return null;
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
    const images = list.filter(isImageFile);
    const docs = list.filter((f) => !isImageFile(f));
    void debugLog({ where: "addFiles:split", images: images.length, docs: docs.length });
    if (images.length) {
      // Bracket the async read so send() can gate on it: until this resolves,
      // `attachments` does not contain these pictures and sending would drop
      // them silently (see imagesReading).
      setImagesReading((n) => n + 1);
      try {
        const items = await Promise.all(images.map(readImage));
        const ok = items.filter((x): x is Attachment => !!x);
        if (ok.length) setAttachments((a) => [...a, ...ok]);
      } finally {
        setImagesReading((n) => Math.max(0, n - 1));
      }
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

  async function attachOne(sid: string, p: string, tmpId: string): Promise<FileAttachment | null> {
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
        return { id: entry.id, name: entry.name, path: entry.path, kind: entry.kind };
      } else {
        setFileAttachments((a) => a.filter((x) => x.id !== tmpId));
        return null;
      }
    } catch (e) {
      void debugLog({ where: "attachPaths:error", name, error: String(e) });
      setFileAttachments((a) => a.filter((x) => x.id !== tmpId));
      return null;
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
  async function pickModel(pid: string, model: string) {
    setModelOpen(false);
    // Never select an empty model. The chip label falls back to the provider id
    // (`model || provider`), so an empty model leaves the label unchanged and the
    // click reads as "nothing happened" — the exact shape of the "cannot pick a
    // model" report. The menu cannot produce one; this is the guard behind it.
    if (!model) return;
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
  // At home nothing is chosen yet, so the chip should name what WOULD be used —
// the default provider's own model — not the provider id. A bare "custom" there
// reads as a model name and looks unchangeable (the "default is customer"
// report), and the id is the one thing the user cannot act on.
  const defaultProviderCfg = g.providers[g.defaultProvider];
  const modelChipLabel = session
    ? session.model || session.provider
    : homeModel?.model ||
      homeModel?.provider ||
      defaultProviderCfg?.default_model ||
      defaultProviderCfg?.model ||
      defaultProviderCfg?.name ||
      g.defaultProvider;

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
    } finally {
      homeCreatingRef.current = false;
    }
  }


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

  function send() {
    const text = input.trim();
    const readyFiles = fileAttachments.filter((f) => !f.uploading);
    // In-flight gate, image half (pre-existing bug, both plain sends and
    // steers): readImage is async, so `attachments` lags a just-dropped image.
    // Sending now would silently drop it. Visible hint instead of a no-op.
    if (imagesReading > 0) {
      showComposerHint("注意：图片处理中，请稍候再发送");
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
      handleParkedSend(sid, text, target ?? session.agent_id ?? g.agents[0]?.id ?? null);
      setInput("");
      setMenu(null);
      return;
    }
    // In-flight gate, document half: a chip still uploading is not in
    // `readyFiles`, so this used to return SILENTLY — send appeared dead. Show
    // why instead (brief §3).
    if (readyFiles.length !== fileAttachments.length) {
      showComposerHint("注意：文件上传中，请稍候再发送");
      return;
    }
    if (busyBySessionRef.current[sid]) {
      // A turn is running → this is steering, not a new turn: queue it for the
      // next superstep of the turn that is already going (design §3.1). Text
      // AND attachments ride the steer (steer-attachments v2) — with nothing to
      // send, do nothing.
      if (!text && attachments.length === 0 && readyFiles.length === 0) return;
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
          m.id === uid ? { ...m, status: "failed" as const, failReason: "连接中断，未送达" } : m,
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
            {composerHint && (
              // Blocked send, made visible. A silent return here read as a
              // dead send button (the original bug report).
              <div className="mb-2 flex items-center gap-1.5 rounded-lg border border-yellow/40 bg-yellow/10 px-2.5 py-1.5 text-xs text-yellow">
                <AlertCircle className="h-3.5 w-3.5 shrink-0" />
                <span>{composerHint}</span>
              </div>
            )}
            {imagesReading > 0 && (
              <div className="mb-2 flex items-center gap-1.5 text-xs text-faint">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                <span>图片处理中…</span>
              </div>
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
                    {f.uploading && (
                      <span className="text-faint">{session ? "上传中…" : "发送时上传"}</span>
                    )}
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
            {session?.type === "subagent" && running && (
              // 子任务视图的输入语义（subagent-design.md §6.2）：running 时的输入
              // 走既有 steering 通道（useSteerQueue，同主对话），这里只把语义标出来。
              <div className="mb-1 flex items-center gap-1.5 px-0.5 text-[10px] text-faint">
                <RotateCcw className="h-3 w-3 shrink-0 animate-pulse" aria-hidden />
                <span>子任务运行中 · 发送将在下个工具边界注入（steering）</span>
              </div>
            )}
            {session && steerItems.length > 0 && (
              // Queue bar (design §4.1): what the user typed into a running turn,
              // waiting for the next superstep to absorb it. Ordered, single-line,
              // removable, and recallable with ↑ — Claude Code's shape.
              <div className="mb-1 rounded-lg border border-line bg-card2/60 px-2 py-1.5">
                <div className="flex items-center justify-between px-0.5 pb-1 text-[10px] text-muted">
                  <span>待发送 ({steerItems.length})</span>
                  <span className="text-faint">↑ 取回编辑</span>
                </div>
                <ol className="space-y-0.5">
                  {steerItems.map((it, i) => (
                    <li
                      key={it.steerId}
                      className="group flex items-start gap-1.5 px-0.5 text-xs text-txt"
                    >
                      <span className="mt-[1px] shrink-0 text-faint">{i + 1}</span>
                      <span className="min-w-0 flex-1 truncate" title={it.text}>
                        {it.text || (it.images.length + it.files.length ? "（仅附件）" : "")}
                      </span>
                      {it.images.length + it.files.length > 0 && (
                        // The queued entry carries attachments too — surface the
                        // count so the row is not read as text-only.
                        <span
                          className="mt-[1px] flex shrink-0 items-center gap-0.5 text-faint"
                          title={`${it.images.length} 张图片 · ${it.files.length} 个文件`}
                        >
                          <Paperclip className="h-3 w-3" />
                          {it.images.length + it.files.length}
                        </span>
                      )}
                      {it.status === "sending" && (
                        <Loader2
                          className="mt-[1px] h-3 w-3 shrink-0 animate-spin text-faint"
                          aria-label="提交中"
                        />
                      )}
                      <button
                        onClick={() => dropSteer(session.id, it.steerId)}
                        className="shrink-0 rounded p-0.5 text-faint opacity-0 transition-opacity hover:text-red group-hover:opacity-100"
                        title="移除这条待发送消息"
                        aria-label="移除待发送消息"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </li>
                  ))}
                </ol>
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
              placeholder="问点什么…  / 命令 · @ 提及产物/智能体/工作流/记忆 · 可拖入图片 / Excel / Word / PPT / PDF"
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
                      className={`ml-1 flex shrink-0 items-center gap-1 whitespace-nowrap rounded-md px-1.5 py-0.5 text-[10px] transition-colors ${
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
              <div className="ml-auto flex shrink-0 items-center gap-1.5">
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
              <div className="relative">
                <button
                  type="button"
                  disabled={running || parked}
                  onClick={() => setModelOpen((v) => !v)}
                  title={session ? "切换本会话模型" : "选择新会话使用的模型"}
                  className="flex items-center gap-1.5 rounded-md border border-line2 bg-card px-2 py-1 text-xs text-muted hover:border-line hover:bg-card2 hover:text-txt disabled:opacity-50"
                >
                  <Globe className="h-3.5 w-3.5 shrink-0" />
                  <span className="max-w-[160px] truncate font-medium">{modelChipLabel}</span>
                  <ChevronDown className="h-3 w-3 shrink-0 opacity-70" />
                </button>
                {modelOpen && (
                  <>
                    <div className="fixed inset-0 z-40" onClick={() => setModelOpen(false)} />
                    <div className="absolute bottom-full right-0 z-50 mb-1 w-72 max-w-[min(18rem,calc(100vw-2rem))] rounded-lg border border-line bg-card py-1 shadow-xl">
                      {enabledProviders.length === 0 && (
                        <div className="px-3 py-2 text-xs text-faint">
                          无已启用提供商 — 去 设置 → 模型 API 启用
                        </div>
                      )}
                      {enabledProviders.map(([pid, p]) => {
                        // One row per MODEL, not per provider. The store keeps a
                        // list (`models`) and a session can be pinned to any entry,
                        // but showing only `default_model` left every extra model
                        // unreachable from chat — which is exactly the "I cannot
                        // pick GLM" report. Grouped by provider instead, and the
                        // dangling `· ` disappears along with the single-row shape.
                        const models = (
                          Array.isArray(p.models) && p.models.length
                            ? p.models
                            : [p.default_model || p.model || ""]
                        ).filter((m): m is string => !!m);
                        const curProvider = session ? session.provider : homeModel?.provider;
                        const curModel = session ? session.model : homeModel?.model;
                        return (
                          <div key={pid}>
                            <div className="px-3 pb-0.5 pt-1.5 text-[10px] uppercase tracking-wider text-faint">
                              {p.name || pid}
                            </div>
                            {models.length === 0 ? (
                              <div className="px-3 py-1 text-[11px] text-faint">
                                此提供商未配置模型 — 去 设置 → 模型 API 添加
                              </div>
                            ) : (
                              models.map((m) => {
                                const on = curProvider === pid && curModel === m;
                                return (
                                  <button
                                    key={`${pid}:${m}`}
                                    onClick={() => void pickModel(pid, m)}
                                    className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-card2 ${
                                      on ? "text-txt" : "text-muted"
                                    }`}
                                  >
                                    <span className={on ? "" : "opacity-0"}>✓</span>
                                    <span className="min-w-0 flex-1 truncate">{m}</span>
                                  </button>
                                );
                              })
                            )}
                          </div>
                        );
                      })}
                      <div className="mt-1 border-t border-line px-3 pt-1 text-[10px] text-faint">
                        下一轮生效 · 设置页更改会覆盖会话级选择
                      </div>
                    </div>
                  </>
                )}
              </div>
              {running && session && !parked ? (
                <button
                  onClick={stopTurn}
                  title="停止当前回合（保留已输出的内容）"
                  aria-label="停止"
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
            Ask {a.name}
            {rec && (
              <span className="rounded-full border border-yellow/40 bg-yellow/10 px-1.5 text-[10px] leading-4 text-yellow">
                推荐
              </span>
            )}
          </button>
        );
      })}
    </>
  );

  // C+ 方案②「X 接手」分隔线：与上一条 agentId 已知的 assistant 气泡不同
  // agent 时插入。旧会话无标记（两侧任一 agentId 为空）自动跳过，不渲染。
  const handoffBefore: Record<number, true> = {};
  {
    let lastKnown: string | null = null;
    messages.forEach((m, i) => {
      if (m.role !== "assistant" || m.error) return;
      if (m.agentId && lastKnown && m.agentId !== lastKnown) handoffBefore[i] = true;
      if (m.agentId) lastKnown = m.agentId;
    });
  }

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
            <div className="mb-2 flex flex-wrap items-center gap-2">
              {agentChipsEl}
              {!target && (
                <span className="ml-auto self-center text-[11px] text-faint">
                  ⏎ 直接发送 = 默认 {g.agents[0]?.name ?? "Agent"}
                </span>
              )}
            </div>
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
            <button
              onClick={() => onOpenGoal?.()}
              className="inline-flex items-center gap-2 rounded-full border border-line px-4 py-2 text-[12.5px] text-muted transition-colors hover:border-line2 hover:bg-card hover:text-txt"
            >
              <span>🎯</span>
              设定一个长程目标
            </button>
          </div>
          <div className="mt-6 text-[11px] text-faint">
            支持拖入 Excel / Word / PPT / PDF · / 命令 · @ 提及产物 / 智能体 / 工作流 / 记忆
          </div>
        </div>
      ) : (
        <>
      {session.type === "subagent" && <SubagentTopBar session={session} />}
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
        className="flex-1 overflow-y-auto px-6 py-6"
      >
        <div className="mx-auto flex max-w-3xl flex-col gap-5">
          {messages.length === 0 && (
            <div className="py-16 text-center text-sm text-faint">
              开始对话吧，Agent 会使用工具完成任务，并可能就权限询问你。
            </div>
          )}

          {messages.map((m, idx) =>
            spawnFold.hide.has(m.id) ? null : spawnFold.groupAt[m.id] ? (
              <div key={m.id} className="flex flex-col items-center gap-2">
                <div className="w-full max-w-[85%]">
                  <SubagentGroupCard
                    rows={
                      spawnFold.groupAt[m.id] as unknown as Array<
                        Extract<Block, { kind: "subagent_spawn" }>
                      >
                    }
                  />
                </div>
              </div>
            ) : m.role === "system" ? (
              <div key={m.id} className="flex flex-col items-center gap-2">
                <ContextBlocks
                  blocks={m.blocks.filter((b): b is Extract<Block, { kind: "context" }> => b.kind === "context")}
                />
                {/* 子代理发起/结果卡片行（subagent-design.md §6.3）：与 context
                    chips 同层——不是任何一方的气泡，是事件驱动的系统行。拆分方案
                    卡片（P2）同层渲染，决定回调走 engine 的 socket 发送。 */}
                <div className="flex w-full max-w-[85%] flex-col gap-2">
                  {m.blocks
                    .filter((b): b is Extract<Block, { kind: "subagent_plan" }> => b.kind === "subagent_plan")
                    .map((b) => (
                      <SubagentPlanCard key={b.planId} block={b} onDecide={decideSubagentPlan} />
                    ))}
                  <SubagentBlocks
                    blocks={m.blocks.filter(
                      (b): b is Extract<Block, { kind: "subagent_spawn" }> | Extract<Block, { kind: "subagent_result" }> =>
                        b.kind === "subagent_spawn" || b.kind === "subagent_result",
                    )}
                  />
                </div>
              </div>
            ) : m.role === "user" &&
              m.blocks.some(
                (b) => b.kind === "subagent_result" || b.kind === "subagent_brief",
              ) ? (
              // runtime 注入的子代理结果（契约 3）与子会话首条任务简报
              // （<ginno_subagent_brief>）在历史重放中是 HumanMessage，重载后以
              // user 角色回来：折成卡片系统行渲染，而不是落到 UserBlocks
              // （它不认识这些块，会渲染成空气泡）。
              <div key={m.id} className="flex flex-col items-center gap-2">
                <div className="w-full max-w-[85%]">
                  <SubagentBlocks
                    agentType={
                      session?.type === "subagent"
                        ? (session.subagent as { agent_type?: string } | undefined)?.agent_type
                        : undefined
                    }
                    blocks={m.blocks.filter(
                      (
                        b,
                      ): b is Extract<
                        Block,
                        { kind: "subagent_result" | "subagent_brief" }
                      > =>
                        b.kind === "subagent_result" || b.kind === "subagent_brief",
                    )}
                  />
                </div>
              </div>
            ) : m.role === "user" && m.blocks.some((b) => b.kind === "steer") ? (
              // A steered message with no assistant step to attach to (absorbed
              // by the turn's first model request, or the transcript ended on
              // it): a full-width band, not a right-aligned bubble — it is not a
              // turn of its own (design §4.2).
              <div key={m.id} className="flex flex-col gap-1">
                {m.blocks
                  .filter((b): b is Extract<Block, { kind: "steer" }> => b.kind === "steer")
                  .map((b) => (
                    <SteerBand key={b.steerId ?? m.id} block={b} />
                  ))}
              </div>
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
                onRetryFromCheckpoint={() => retryFromCheckpoint(m.id)}
              />
            ) : (
              <Fragment key={m.id}>
                {handoffBefore[idx] && (
                  <HandoffDivider agent={agentById(m.agentId)} agentName={m.agentName} />
                )}
                <AssistantBubble
                  agent={agentById(m.agentId)}
                  agentName={m.agentName}
                  subagentTypeName={
                    session?.type === "subagent"
                      ? (session.subagent as { agent_type?: string } | undefined)?.agent_type
                      : undefined
                  }
                  blocks={m.blocks}
                  streaming={m.id === liveId}
                  turnId={m.turnId}
                  failed={m.failed}
                  onAnswerQuestion={answerQuestion}
                  // questionLive: is this session's turn actually alive? A
                  // parked question keeps the server turn running WITHOUT a
                  // message.end, so two signals cover it: `liveId` (this client
                  // is watching the stream) and `serverRunning` (the answer to
                  // the turn_state probe, which is the ONLY one that survives a
                  // full reload — history rebuilds the card as pending, and
                  // without the probe it would render disabled even though the
                  // backend would accept the answer).
                  // Deliberately not `running`: its questionPending term comes
                  // from the card itself (circular — a stale pending card would
                  // keep itself interactive forever).
                  questionLive={liveId !== null || serverRunning}
                />
              </Fragment>
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
      <div className="px-6 pb-5 pt-2">
        <div className="mx-auto max-w-3xl">
          {switchTarget && (
            <div className="mb-2 flex items-center gap-2 rounded-lg border border-line2 bg-card px-3 py-2 text-xs">
              <span
                className="h-1.5 w-1.5 shrink-0 rounded-full"
                style={{ background: agentHex(switchTarget.color) }}
              />
              <span className="flex-1 text-muted">
                下一条起由{" "}
                <span className="font-medium" style={{ color: agentHex(switchTarget.color) }}>
                  {switchTarget.name}
                </span>{" "}
                应答 · 会话记录保留
              </span>
              <button
                onClick={() => setTarget(null)}
                className="shrink-0 rounded-md border border-line2 px-2 py-0.5 text-[11px] text-muted transition-colors hover:text-txt"
              >
                撤销
              </button>
            </div>
          )}
          <div className="mb-2 flex flex-wrap gap-2">
            {agentChipsEl}
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
                <div className="absolute bottom-full left-0 z-30 mb-1 w-64 max-w-[min(16rem,calc(100vw-2rem))] rounded-lg border border-line bg-card p-1 shadow-2xl">
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

          <div key="composer-session">{composerBoxEl}</div>
        </div>
      </div>
        </>
      )}
    </div>
  );
}

