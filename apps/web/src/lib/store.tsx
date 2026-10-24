"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import * as api from "./runtime";
import type { SynthesisCaseSummary } from "./runtime";
import { loadNotifyPrefs } from "./notifyPrefs";
import type { AgentConfig, Artifact, ArtifactPatch, Goal, GoalStatus, Plan, Providers, SessionMeta, SkillSummary, SubagentSpawnEvent, SubagentStatusEvent, Team, Todo, WorkflowDef, WorkflowRun } from "./types";
import { useRightPanel, type RightTab } from "./store/useRightPanel";
import {
  useCodePanel,
  type CodeOpenRequest,
  type CodeTouch,
  type ComposerInsertRequest,
} from "./store/useCodePanel";
import { usePreview, type PreviewFile } from "./store/usePreview";
import { useWorkflows, type RunToolActivity } from "./store/useWorkflows";
import { useSessions } from "./store/useSessions";
import { useSidebarTree } from "./store/useSidebarTree";
import { useSubagents } from "./store/useSubagents";
import { useArtifacts } from "./store/useArtifacts";
import { useTodos } from "./store/useTodos";
import { useProviders } from "./store/useProviders";
import { useGoals } from "./store/useGoals";
import { useTeams } from "./store/useTeams";
import { usePlans } from "./store/usePlans";

export {
  PANEL_WIDTH_MIN,
  PANEL_WIDTH_MAX,
  PANEL_WIDTH_DEFAULT,
  PANEL_WIDTH_MAX_CODE,
  defaultPanelWidths,
  panelWidthMax,
} from "./store/useRightPanel";
export type { RightTab } from "./store/useRightPanel";
export type { CodeTouch, CodeOpenRequest, ComposerInsertRequest } from "./store/useCodePanel";
export type { PreviewFile } from "./store/usePreview";
export type { RunToolActivity } from "./store/useWorkflows";
export { LAST_SESSION_KEY } from "./store/useSessions";
export { notifyScheduleRunFinished } from "./store/notifications";

interface GinnoState {
  agents: AgentConfig[];
  skills: SkillSummary[];
  sessions: SessionMeta[];
  todos: Todo[];
  workflows: WorkflowDef[];
  workflowRuns: WorkflowRun[];
  artifacts: Artifact[];
  projectArtifacts: Artifact[];
  providers: Providers;
  defaultProvider: string;
  activeSessionId: string | null;
  connected: boolean;
  ready: boolean;
  sessionError: string | null;
  rightTab: RightTab;
  artifactsFollow: boolean;
  flashArtifactIds: string[];
  previewFile: PreviewFile | null;
  previewNonce: number;
  setConnected: (v: boolean) => void;
  setActiveSession: (id: string | null) => void;
  // ---- 侧栏会话未读小圆点：turn 在用户没盯着该会话时结束（完成/报错/
  // 子代理终态）标记未读；打开或（窗口可见时）回到该会话即清除。----
  unreadSessions: Record<string, number>;
  markSessionUnread: (id: string) => void;
  clearSessionUnread: (id: string) => void;
  // 「正在看」判定（活跃会话 ∧ 窗口可见）：handle.ts 与 reloadSessions 用它
  // 决定 turn 收口后上报已读还是点亮紫点。
  watchingSession: (id: string) => boolean;
  setRightTab: (tab: RightTab, opts?: { manual?: boolean }) => void;
  // ---- right panel open/width/badges (right-panel-redesign.md) ----
  rightPanelOpen: boolean;
  rightPanelWidth: number; // px, clamped to [PANEL_WIDTH_MIN, panelWidthMax(tab)]
  // Per-tab widths (design §4.5): switching tabs restores that tab's width.
  // `rightPanelWidth` mirrors the active tab's entry for existing consumers.
  rightPanelWidthByTab: Record<RightTab, number>;
  // Unread counts accumulated while the panel was collapsed (v1: artifacts).
  panelBadge: Partial<Record<RightTab, number>>;
  setRightPanelOpen: (open: boolean) => void;
  setRightPanelWidth: (w: number) => void;
  clearPanelBadge: (tab?: RightTab) => void; // omit tab → clear all
  // ---- right panel tab order / visibility (Settings → 通用设置) ----
  /** Full order, hidden tabs included — this is what the settings list reorders. */
  rightTabOrder: RightTab[];
  rightTabsHidden: RightTab[];
  /** Derived: ordered AND visible — what the tab strip and the dock render. */
  visibleRightTabs: RightTab[];
  setRightTabOrder: (order: RightTab[]) => void;
  setRightTabHidden: (id: RightTab, hidden: boolean) => void;
  resetRightTabs: () => void;
  // ---- code panel (docs/code-panel-design.md) ----
  // Root currently selected in the tree; null until the panel picks one.
  codeRootId: string | null;
  // Panel layout within the tab: "file" = editor-first (tree collapsed to a
  // breadcrumb, ⌘B expands); "side" = tree + editor side by side.
  codePanelMode: "file" | "side";
  // Whether the tree drawer/sidebar is shown in "file" mode (⌘B).
  codeTreeOpen: boolean;
  setCodeRootId: (id: string) => void;
  setCodePanelMode: (m: "file" | "side") => void;
  setCodeTreeOpen: (v: boolean) => void;
  // Pending open request from the chat-jump path; consumed by the panel.
  codeOpenRequest: CodeOpenRequest | null;
  clearCodeOpenRequest: () => void;
  openInCode: (target: { rootId: string; path: string; line?: number }) => void;
  // ---- composer insert (code tree → composer drag; one-shot bridge) ----
  composerInsert: ComposerInsertRequest | null;
  insertToComposer: (text: string) => void;
  clearComposerInsert: () => void;
  /** True while a tree-file pointer drag hovers the composer (drop highlight).
   *  Set from the tree's drag loop; ChatStream merges it into the same violet
   *  border the Finder HTML5 drop uses. */
  composerDragOver: boolean;
  setComposerDragOver: (v: boolean) => void;
  // ---- agent file changes (design §4.7 S3) ----
  /** Absolute path → the agent's last write/edit, from `code.changed`. Feeds
   *  the tree's agent marks (cumulative). */
  codeTouched: Record<string, CodeTouch>;
  /** Only the NEWEST change, so a consumer can react once per event instead of
   *  re-scanning the cumulative map (and without diffing it). */
  codeLastChange: (CodeTouch & { path: string; nonce: number }) | null;
  /** Called by the session socket handler when `code.changed` arrives. */
  notifyCodeChange: (change: { path: string; op: "write" | "edit"; version: string }) => void;
  openPreview: (f: PreviewFile) => void;
  closePreview: () => void;
  notifyPreviewInvalidate: (fileId: string) => void;
  reloadAgents: () => Promise<void>;
  reloadSkills: () => Promise<void>;
  reloadMemoryBadge: () => Promise<void>;
  reloadSessions: () => Promise<void>;
  reloadTodos: () => Promise<void>;
  reloadProviders: () => Promise<void>;
  reloadWorkflows: () => Promise<void>;
  reloadWorkflowRuns: () => Promise<void>;
  // ---- synthesis cases (「总结成流程」记录,右栏「总结」tab) ----
  synthesisCases: SynthesisCaseSummary[];
  reloadSynthesisCases: () => Promise<void>;
  synthesisActiveCount: number; // 进行中(无 output 且任务存活)→ 蓝色脉冲点
  // Workflow tab badge (work item E): live counts derived from workflowRuns.
  activeRunCount: number; // running + paused → blue pulsing badge
  unseenFailedCount: number; // failed since last visit → red badge
  markFailedRunsSeen: () => void; // called when the Workflow tab is opened
  // Paused-at-human-node count (workflow-ux-redesign P1) → yellow dock badge.
  pendingHumanCount: number;
  // Recent completed-run durations per workflow_id (P3 adaptive stuck).
  runDurationByWorkflow: Record<string, number[]>;
  // Live tool-call visibility (workflow-ux-redesign P1): run_id → activity.
  liveToolActivity: Record<string, RunToolActivity>;
  notifyRunToolActivity: (runId: string, act: RunToolActivity | null) => void;
  reloadArtifacts: () => Promise<void>;
  reloadProjectArtifacts: () => Promise<void>;
  removeArtifact: (id: string) => Promise<void>;
  patchArtifact: (id: string, patch: ArtifactPatch) => Promise<{ ok: boolean; error?: string }>;
  newSession: (
    agent_id?: string,
    opts?: {
      title?: string;
      provider?: string;
      model?: string;
      workflow_id?: string;
      /** 出生即挂载(home-mount-picker-design.md §3)——首页 composer 的 pending 目录 */
      context_folders?: string[];
      primary_folder?: string | null;
    },
  ) => Promise<SessionMeta | null>;
  setSessionAgent: (id: string, agentId: string) => void;
  removeSession: (id: string, opts?: { cascade?: boolean }) => Promise<void>;
  renameSession: (id: string, title: string) => Promise<void>;
  // 侧栏置顶（sidebar pin）：乐观写 pinned，随后提交到后端。
  pinSession: (id: string, pinned: boolean) => Promise<void>;
  // Merge a server-pushed or optimistic partial into one session's meta
  // (session_title WS events, model-switch reconcile).
  applySessionPatch: (id: string, patch: Partial<SessionMeta>) => void;
  // ---- subagent (subagent-design.md §4.1/§6.1；由 ChatStream 的 WS 分发调用) ----
  // subagent.spawned：upsert 子会话行（列表里还没有时合成一行占位，meta 由
  // 事件字段拼出；随后的 reloadSessions 会以服务端权威数据覆盖）。
  notifySubagentSpawned: (ev: SubagentSpawnEvent) => void;
  // subagent.status：回填子会话的 status / result_summary。
  notifySubagentStatus: (ev: SubagentStatusEvent) => void;
  patchTodo: (id: string, patch: Partial<Todo>) => Promise<void>;
  addTodo: (data: Partial<Todo>) => Promise<void>;
  removeTodo: (id: string) => Promise<void>;
  // Pulse-highlight artifacts (e.g. after jumping to a session from a TODO).
  flashArtifacts: (ids: string[]) => void;
  // ---- session goal (goal-design.md) ----
  goalBySession: Record<string, Goal | null>;
  notifyGoal: (sessionId: string, goal: Goal | null) => void;
  loadGoal: (sessionId: string) => Promise<void>;
  // ---- AgentTeam（agent-team-design.md §5.2：@ 投递可见范围等读面）----
  teams: Team[];
  reloadTeams: () => Promise<void>;
  notifyTeamUpdated: (team: Team) => void;
  // ---- 任务清单编排（subagent-plan-orchestration-design.md §2/P3）----
  // Per-session plan 快照：打开会话时 REST 拉取 + plan.updated WS 事件实时
  // 刷（watcher 每次落盘推整张清单）。PlanBoard 消费。
  plansBySession: Record<string, Plan[]>;
  loadPlans: (sessionId: string) => Promise<void>;
  notifyPlanUpdated: (plan: Plan) => void;
  setGoalObjective: (
    sessionId: string,
    objective: string,
    confirm?: boolean,
  ) => Promise<{ ok: boolean; needs_confirm?: boolean; error?: string }>;
  setGoalStatus: (sessionId: string, status: GoalStatus) => Promise<{ ok: boolean; error?: string }>;
  clearGoal: (sessionId: string) => Promise<void>;
  // ---- workflow run 视图（侧栏运行伪条目的点击目标）----
  // 中央区当前打开的 run；null = 正常会话视图。每窗口临时状态，不持久化，
  // 切换/清空会话时一并清掉（见 setActiveSession / removeSession）。
  activeRunId: string | null;
  openRunView: (runId: string) => void;
  closeRunView: () => void;
  // ---- 定时任务回放（scheduled-tasks-design.md §3.6）----
  // 影子会话不进会话列表，回放由 /scheduled 的时间条/执行记录经
  // openScheduleRun 打开；挂载骨架与 activeRunId 分支互斥（AppShell）。
  // 快照随 id 一起存——执行记录没有单条 GET，顶栏的任务名/状态直接取快照。
  activeScheduleRunId: string | null;
  activeScheduleRun: import("./types").ScheduleRun | null;
  openScheduleRun: (run: import("./types").ScheduleRun) => void;
  closeScheduleRun: () => void;
  // ---- 侧栏子树折叠覆盖（从 AppShell 上收，localStorage 持久化）----
  // 语义与原 AppShell 本地状态一致：undefined = 默认（有子代 → 展开），
  // true = 用户折叠，false = 用户显式展开。键为会话 id。
  treeCollapsed: Record<string, boolean>;
  setTreeCollapsed: (id: string, collapsed: boolean) => void;
}

const Ctx = createContext<GinnoState | null>(null);

export function useGinno(): GinnoState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useGinno must be used within GinnoProvider");
  return v;
}

export function GinnoProvider({ children }: { children: ReactNode }) {
  const [connected, setConnected] = useState(false);
  const [ready, setReady] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [agents, setAgents] = useState<AgentConfig[]>([]);
  const [skills, setSkills] = useState<SkillSummary[]>([]);

  const panelApi = useRightPanel();
  const {
    rightTab,
    artifactsFollow,
    flashArtifactIds,
    rightPanelOpen,
    rightPanelWidth,
    rightPanelWidthByTab,
    panelBadge,
    rightTabOrder,
    rightTabsHidden,
    visibleRightTabs,
    setRightPanelOpen,
    setRightPanelWidth,
    clearPanelBadge,
    setRightTabOrder,
    setRightTabHidden,
    resetRightTabs,
    activateRightTab,
    setArtifactsFollow,
    setPanelBadge,
    setRightTabState,
    setFlashArtifactIds,
    rightPanelOpenRef,
    artifactsFollowRef,
    rightTabsHiddenRef,
    pendingFlashRef,
    flashArtifacts,
  } = panelApi;

  // Stable setters/actions that cross the extracted slice boundary are read
  // through refs so every hook keeps the same dependency arrays it had inline.
  const panelHandlesRef = useRef({ setArtifactsFollow, setPanelBadge });

  const workflowApi = useWorkflows({
    activateRightTab,
    rightPanelOpenRef,
    setRightPanelOpen,
  });
  const {
    workflows,
    workflowRuns,
    synthesisCases,
    liveToolActivity,
    notifyRunToolActivity,
    activeRunId,
    openRunView,
    closeRunView,
    scheduleRun,
    openScheduleRun,
    closeScheduleRun,
    markFailedRunsSeen,
    reloadWorkflows,
    reloadWorkflowRuns,
    reloadSynthesisCases,
    activeRunCount,
    unseenFailedCount,
    pendingHumanCount,
    synthesisActiveCount,
    runDurationByWorkflow,
    setActiveRunId,
    setScheduleRun,
  } = workflowApi;

  // Shared mirror of the sessions list: written by useSessions, read at write
  // time by the sidebar-tree slice (prune) and the unread badge overlay.
  const sessionsRef = useRef<SessionMeta[]>([]);
  const { treeCollapsed, setTreeCollapsed, pruneTreeCollapsed } = useSidebarTree(sessionsRef);

  const sessionApi = useSessions({
    sessionsRef,
    setActiveRunId,
    setScheduleRun,
    setSessionError,
    pruneTreeCollapsed,
  });
  const {
    sessions,
    setSessions,
    activeSessionId,
    activeSessionRef,
    unreadSessions,
    markSessionUnread,
    clearSessionUnread,
    watchingSession,
    setActiveSession,
    reloadSessions,
    newSession,
    setSessionAgent,
    removeSession,
    renameSession,
    pinSession,
    applySessionPatch,
  } = sessionApi;

  const { notifySubagentSpawned, notifySubagentStatus } = useSubagents({
    setSessions,
    markSessionUnread,
  });

  const artifactApi = useArtifacts({
    activeSessionId,
    activeSessionRef,
    rightPanelOpenRef,
    artifactsFollowRef,
    rightTabsHiddenRef,
    pendingFlashRef,
    setPanelBadge,
    setRightTabState,
    setFlashArtifactIds,
  });
  const {
    artifacts,
    projectArtifacts,
    reloadArtifacts,
    reloadProjectArtifacts,
    removeArtifact,
    patchArtifact,
  } = artifactApi;

  const setRightTab = useCallback(
    (tab: RightTab, opts?: { manual?: boolean }) => {
      if (opts?.manual) {
        // explicit user choice: stop auto-following unless they picked Artifacts
        panelHandlesRef.current.setArtifactsFollow(tab === "artifacts");
      }
      if (tab === "workflow") markFailedRunsSeen();
      activateRightTab(tab);
    },
    [markFailedRunsSeen, activateRightTab],
  );

  const codeApi = useCodePanel({
    setRightTab,
    rightPanelOpenRef,
    setRightPanelOpen,
  });
  const {
    codeRootId,
    codePanelMode,
    codeTreeOpen,
    setCodeRootId,
    setCodePanelMode,
    setCodeTreeOpen,
    codeOpenRequest,
    codeTouched,
    codeLastChange,
    notifyCodeChange,
    clearCodeOpenRequest,
    openInCode,
    composerInsert,
    insertToComposer,
    clearComposerInsert,
    composerDragOver,
    setComposerDragOver,
  } = codeApi;

  const { previewFile, previewNonce, openPreview, closePreview, notifyPreviewInvalidate } =
    usePreview();

  const { todos, reloadTodos, patchTodo, addTodo, removeTodo } = useTodos();
  const { providers, defaultProvider, reloadProviders } = useProviders();
  const { goalBySession, notifyGoal, loadGoal, setGoalObjective, setGoalStatus, clearGoal } =
    useGoals();
  const { teams, reloadTeams, notifyTeamUpdated } = useTeams();
  const { plansBySession, loadPlans, notifyPlanUpdated } = usePlans();

  const reloadAgents = useCallback(async () => {
    try {
      setAgents(await api.listAgents());
    } catch {
      /* sidecar down */
    }
  }, []);
  const reloadSkills = useCallback(async () => {
    try {
      // The web app is single-project ("default") — same convention as
      // listArtifacts. The server still merges project-scoped overrides.
      setSkills(await api.listSkills("default"));
    } catch {
      /* sidecar down */
    }
  }, []);
  const reloadMemoryBadge = useCallback(async () => {
    try {
      const m = await api.getMemory();
      // Single-slot refinery: badge is boolean-ish (0/1) — a pending draft
      // awaits review in the Memory tab.
      panelHandlesRef.current.setPanelBadge((p) => ({ ...p, memory: m.draft_pending ? 1 : 0 }));
    } catch {
      /* sidecar down */
    }
  }, []);

  useEffect(() => {
    let alive = true;
    (async () => {
      // Wait for the sidecar before loading data. This matters for the
      // packaged desktop app, where the webview can boot before the
      // sidecar has finished starting, and for any tab that opened while
      // the sidecar was restarting.
      for (let i = 0; i < 60; i++) {
        try {
          const h = await api.health();
          if (h?.ok) break;
        } catch {
          /* sidecar not up yet */
        }
        await new Promise((r) => setTimeout(r, 500));
        if (!alive) return;
      }
      if (!alive) return;
      await Promise.all([
        reloadAgents(),
        reloadSkills(),
        reloadSessions(),
        reloadTodos(),
        reloadProviders(),
        reloadWorkflows(),
        reloadWorkflowRuns(),
        reloadSynthesisCases(),
        reloadArtifacts(),
        reloadProjectArtifacts(),
        // Notification gate + sound prefs (settings.json) into the sync cache
        // before any completion event can arrive.
        loadNotifyPrefs(),
      ]);
      if (alive) setReady(true);
    })();
    return () => {
      alive = false;
    };
  }, [
    reloadAgents,
    reloadSkills,
    reloadSessions,
    reloadTodos,
    reloadProviders,
    reloadWorkflows,
    reloadWorkflowRuns,
    reloadSynthesisCases,
    reloadArtifacts,
    reloadProjectArtifacts,
  ]);

  const value: GinnoState = {
    agents,
    skills,
    sessions,
    todos,
    workflows,
    workflowRuns,
    artifacts,
    projectArtifacts,
    providers,
    defaultProvider,
    activeSessionId,
    connected,
    ready,
    sessionError,
    rightTab,
    artifactsFollow,
    flashArtifactIds,
    previewFile,
    previewNonce,
    setConnected,
    setActiveSession,
    unreadSessions,
    markSessionUnread,
    clearSessionUnread,
    watchingSession,
    activeRunId,
    openRunView,
    closeRunView,
    activeScheduleRunId: scheduleRun?.run_id ?? null,
    activeScheduleRun: scheduleRun,
    openScheduleRun,
    closeScheduleRun,
    setRightTab,
    rightPanelOpen,
    rightPanelWidth,
    rightPanelWidthByTab,
    panelBadge,
    setRightPanelOpen,
    setRightPanelWidth,
    clearPanelBadge,
    rightTabOrder,
    rightTabsHidden,
    visibleRightTabs,
    setRightTabOrder,
    setRightTabHidden,
    resetRightTabs,
    codeRootId,
    codePanelMode,
    codeTreeOpen,
    setCodeRootId,
    setCodePanelMode,
    setCodeTreeOpen,
    codeOpenRequest,
    codeTouched,
    codeLastChange,
    notifyCodeChange,
    clearCodeOpenRequest,
    openInCode,
    composerInsert,
    insertToComposer,
    clearComposerInsert,
    composerDragOver,
    setComposerDragOver,
    openPreview,
    closePreview,
    notifyPreviewInvalidate,
    reloadAgents,
    reloadSkills,
    reloadMemoryBadge,
    reloadSessions,
    reloadTodos,
    reloadProviders,
    reloadWorkflows,
    reloadWorkflowRuns,
    synthesisCases,
    reloadSynthesisCases,
    synthesisActiveCount,
    activeRunCount,
    unseenFailedCount,
    markFailedRunsSeen,
    pendingHumanCount,
    runDurationByWorkflow,
    liveToolActivity,
    notifyRunToolActivity,
    reloadArtifacts,
    reloadProjectArtifacts,
    removeArtifact,
    patchArtifact,
    newSession,
    setSessionAgent,
    removeSession,
    treeCollapsed,
    setTreeCollapsed,
    renameSession,
    pinSession,
    applySessionPatch,
    notifySubagentSpawned,
    notifySubagentStatus,
    patchTodo,
    addTodo,
    removeTodo,
    flashArtifacts,
    goalBySession,
    notifyGoal,
    loadGoal,
    plansBySession,
    loadPlans,
    notifyPlanUpdated,
    teams,
    reloadTeams,
    notifyTeamUpdated,
    setGoalObjective,
    setGoalStatus,
    clearGoal,
  };

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
