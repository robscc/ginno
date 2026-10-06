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
import { ALL_RIGHT_TAB_IDS, canonicalTabOrder, visibleTabOrder } from "./rightTabs";
import type { SynthesisCaseSummary } from "./runtime";
import { notifyNative } from "./desktop";
import { loadNotifyPrefs, notifyPrefs } from "./notifyPrefs";
import { uiText } from "../i18n/uiText";
import type { AgentConfig, Artifact, ArtifactPatch, FileEntry, Goal, GoalStatus, Providers, SessionMeta, SkillSummary, SubagentSpawnEvent, SubagentStatusEvent, Todo, WorkflowDef, WorkflowRun } from "./types";

export type RightTab = "todo" | "workflow" | "artifacts" | "memory" | "synthesis" | "code";

/** An agent write/edit observed on the session socket (design §4.7 S3). */
export interface CodeTouch {
  op: "write" | "edit";
  /** The file's version right after the agent's write (``size:mtime_ns``). The
   *  code panel reuses it as the conflict baseline for a DIRTY tab, which is
   *  why the WS event carries it at all (S3 brief §0). */
  version: string;
  /** Epoch ms — newest-wins when several changes land in one turn. */
  at: number;
}

// Right panel width bounds (right-panel-redesign.md §3.4). The panel renders
// at `rightPanelWidth`; dragging clamps into this range, double-click resets.
export const PANEL_WIDTH_MIN = 280;
export const PANEL_WIDTH_MAX = 560;
export const PANEL_WIDTH_DEFAULT = 380;
// The code tab is allowed a much wider panel — it holds a tree + editor
// (docs/code-panel-design.md §4.5): min(50vw, this cap).
export const PANEL_WIDTH_MAX_CODE = 1100;

/** Per-tab right-panel widths, all starting at the shared default. Width is
 *  remembered per tab (design §4.5) so switching tabs restores that tab's own
 *  width instead of forcing one global number. */
export function defaultPanelWidths(): Record<RightTab, number> {
  return {
    todo: PANEL_WIDTH_DEFAULT,
    workflow: PANEL_WIDTH_DEFAULT,
    artifacts: PANEL_WIDTH_DEFAULT,
    memory: PANEL_WIDTH_DEFAULT,
    synthesis: PANEL_WIDTH_DEFAULT,
    code: PANEL_WIDTH_DEFAULT,
  };
}

/** Max right-panel width for a tab (design §4.5): the code tab may grow to
 *  half the viewport (capped at PANEL_WIDTH_MAX_CODE); every other tab keeps
 *  the historical 560px cap. Evaluated on demand so it tracks the window size
 *  while dragging. */
export function panelWidthMax(tab: RightTab): number {
  if (tab !== "code") return PANEL_WIDTH_MAX;
  const vw = typeof window !== "undefined" ? window.innerWidth : 0;
  if (!vw) return PANEL_WIDTH_MAX_CODE;
  return Math.max(PANEL_WIDTH_MIN, Math.min(0.5 * vw, PANEL_WIDTH_MAX_CODE));
}

// localStorage key for the persisted right-panel prefs.
// Current shape: {open, widths: {tab: px}}. Legacy {open, width} is still read
// (the single width migrates onto the tab active at hydration time).
const PANEL_PREFS_KEY = "ginno-right-panel";
// Last active session, restored on boot (open-experience redesign). Only real
// ids are stored; visiting home (null) keeps the previous id so a relaunch
// still resumes where the user left off.
export const LAST_SESSION_KEY = "ginno-last-session";
// 侧栏子树折叠覆盖（subagent-design.md §6.1，从 AppShell 本地状态上收持久化）。
// shape: {session_id: boolean}——只存用户显式点过 chevron 的会话，其余走默认。
const TREE_COLLAPSED_KEY = "ginno-sidebar-tree";

export interface PreviewFile {
  id: string;
  name: string;
  path: string;
  kind?: string;
  mtime?: number;
}

/** A pending "open this file in the code panel" request, published by the chat
 *  jump path (openInCode) and consumed by the panel (docs/code-panel-design.md
 *  §3.4). The nonce bumps on every call so re-opening the same path still
 *  re-triggers reveal/line positioning. `line` is S1-only state: the panel
 *  records it and Monaco highlights it once wired. */
export interface CodeOpenRequest {
  rootId: string;
  path: string;
  line?: number;
  nonce: number;
}

/** Live in-flight tool call for a workflow run step (workflow-ux-redesign P1):
 *  shown under the running step in LiveRunBlock; cleared by tool_result /
 *  node_exit / terminal events. Ephemeral — never persisted. */
export interface RunToolActivity {
  nodeId: string;
  toolName: string;
  argsPreview: string;
}

// ---- Notifications for run completion (P3) ----
// Module-level prev-status map: reloadWorkflowRuns diffs against it to catch
// done/failed transitions while the tab is hidden.
const _prevRunStatus: Record<string, string> = {};

function fmtRunElapsed(r: { started: number; finished?: number | null }): string {
  const s = Math.max(0, Math.round((r.finished ?? Date.now() / 1000) - r.started));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

function notifyRunTransitions(
  runs: Array<{ id: string; name?: string; status: string; steps?: Array<{ id: string; title?: string; status: string }>; started: number; finished?: number | null }>,
  onOpenPanel?: () => void,
) {
  // Master gate (Settings → Notifications, persisted in settings.json; the
  // sync cache avoids stale closures — see lib/notifyPrefs.ts). _prevRunStatus
  // keeps updating even while disabled so re-enabling doesn't fire a burst of
  // stale transitions.
  const prefs = notifyPrefs();
  const hidden = typeof document !== "undefined" && document.visibilityState !== "visible";
  for (const r of runs) {
    const prev = _prevRunStatus[r.id];
    _prevRunStatus[r.id] = r.status;
    if (prev === undefined || prev === r.status) continue;
    if (!prefs.enabled) continue;
    if (!hidden) continue; // user is looking — the badges already cover it
    let body: string;
    if (r.status === "done") {
      // 通知模板走 ui 域（模块级函数无 hook，uiText 同步读当前 locale）
      body = uiText("notifyRun.completed", { duration: fmtRunElapsed(r) });
    } else if (r.status === "failed") {
      const failed = (r.steps || []).find((s) => s.status === "failed");
      body = failed?.title
        ? uiText("notifyRun.failedAt", { step: failed.title })
        : uiText("notifyRun.failed");
    } else {
      continue;
    }
    const title = uiText(r.status === "done" ? "notifyRun.titleDone" : "notifyRun.titleFailed", {
      name: r.name || uiText("notifyRun.defaultName"),
    });
    // Desktop: the Tauri shell fires a real macOS notification (WKWebView has
    // no window.Notification). Click → window focus + open the Workflow panel.
    void notifyNative({
      kind: "workflow-run",
      id: r.id,
      title,
      body,
      sound: prefs.sound ? prefs.soundName : undefined,
    }).then((sent) => {
      if (sent) return;
      // Plain-browser dev fallback.
      if (typeof Notification === "undefined") return;
      if (Notification.permission === "default") {
        // First time anything runs: ask for permission once (user-initiated
        // work is happening, so the prompt is contextually reasonable).
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
          onOpenPanel?.(); // land on the Workflow tab so the run is visible
          n.close();
        };
      } catch {
        /* notification blocked/unsupported */
      }
    });
  }
}

interface GinnoState {
  agents: AgentConfig[];
  skills: SkillSummary[];
  sessions: SessionMeta[];
  todos: Todo[];
  workflows: WorkflowDef[];
  workflowRuns: WorkflowRun[];
  artifacts: Artifact[];
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
  removeArtifact: (id: string) => Promise<void>;
  patchArtifact: (id: string, patch: ArtifactPatch) => Promise<{ ok: boolean; error?: string }>;
  newSession: (
    agent_id?: string,
    opts?: { title?: string; provider?: string; model?: string; workflow_id?: string },
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
  const [agents, setAgents] = useState<AgentConfig[]>([]);
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [todos, setTodos] = useState<Todo[]>([]);
  const [workflows, setWorkflows] = useState<WorkflowDef[]>([]);
  const [workflowRuns, setWorkflowRuns] = useState<WorkflowRun[]>([]);
  // Synthesis cases (「总结成流程」记录): full list incl. the `running` flag.
  // Fed at boot, by synthesis.event WS pushes (ChatStream) and a 1.5s fallback
  // poll in the right-panel 总结 tab while a case is in flight.
  const [synthesisCases, setSynthesisCases] = useState<SynthesisCaseSummary[]>([]);
  // Tab-badge bookkeeping (work item E): ids of failed runs the user has
  // already seen. Bootstrapped with the boot-time failures on first load so a
  // restart doesn't light up the red badge for stale history; new failures
  // stay unseen until the Workflow tab is visited.
  const [failedSeen, setFailedSeen] = useState<Set<string>>(new Set());
  const failedSeedRef = useRef(false);
  // Live tool-call activity per run (workflow-ux-redesign P1). Fed by the
  // run.event WS handler (ChatStream): tool_call sets it, tool_result /
  // node_exit / terminal events clear it.
  const [liveToolActivity, setLiveToolActivity] = useState<Record<string, RunToolActivity>>({});
  const notifyRunToolActivity = useCallback((runId: string, act: RunToolActivity | null) => {
    setLiveToolActivity((prev) => {
      if (act === null) {
        if (!(runId in prev)) return prev;
        const next = { ...prev };
        delete next[runId];
        return next;
      }
      return { ...prev, [runId]: act };
    });
  }, []);
  const workflowRunsRef = useRef<WorkflowRun[]>([]);
  useEffect(() => {
    workflowRunsRef.current = workflowRuns;
  }, [workflowRuns]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  // Ref mirror of the committed artifacts list: reloadArtifacts diffs against
  // it OUTSIDE the setState updater (side effects inside updaters double-fire
  // under dev StrictMode).
  const artifactsListRef = useRef<Artifact[]>([]);
  useEffect(() => {
    artifactsListRef.current = artifacts;
  }, [artifacts]);
  const [providers, setProviders] = useState<Providers>({});
  const [defaultProvider, setDefaultProvider] = useState("custom");
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  // 中央区当前打开的 workflow run（侧栏运行伪条目）。窗口内临时状态：不持久
  // 化，随会话切换/清空一起清掉。
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [ready, setReady] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);

  // Persist the last real session so boot can resume it (AppShell reads
  // LAST_SESSION_KEY). Home (null) intentionally keeps the previous id.
  useEffect(() => {
    try {
      if (activeSessionId) localStorage.setItem(LAST_SESSION_KEY, activeSessionId);
    } catch {
      /* storage unavailable */
    }
    // Floating quick-chat "follow" mode (docs/floating-window-design.md §2.3):
    // announce the main window's active session so the pin window can mirror
    // it. The pin's OWN provider instance must never rebroadcast — it would
    // clobber the followed id with its (always null) activeSessionId.
    if (typeof window !== "undefined" && window.location.pathname !== "/pin") {
      try {
        const bc = new BroadcastChannel("ginno-active-session");
        bc.postMessage({ type: "active", sessionId: activeSessionId });
        bc.close();
      } catch {
        /* BroadcastChannel unavailable */
      }
    }
  }, [activeSessionId]);

  // Right panel: tab is store-owned so chat events can auto-switch to
  // Artifacts when the active session gains one (docs §7.6). Manual clicks
  // turn autoFollow off (sticky) so the agent can't yank focus repeatedly;
  // visiting Artifacts manually or starting fresh re-enables it. Default is
  // Artifacts — first tab of the reordered bar (right-panel-redesign.md §3.1).
  const [rightTab, setRightTabState] = useState<RightTab>("artifacts");
  const [artifactsFollow, setArtifactsFollow] = useState(true);
  const artifactsFollowRef = useRef(true);
  useEffect(() => {
    artifactsFollowRef.current = artifactsFollow;
  }, [artifactsFollow]);
  const [flashArtifactIds, setFlashArtifactIds] = useState<string[]>([]);
  const [previewFile, setPreviewFile] = useState<PreviewFile | null>(null);
  const [previewNonce, setPreviewNonce] = useState(0);

  // ---- right panel open/width + collapsed badges (right-panel-redesign.md) ----
  // Defaults match the pre-redesign behavior (open, 380px) so upgrading users
  // see no sudden change. Persisted as one JSON blob (`ginno-right-panel`).
  const [rightPanelOpen, setRightPanelOpenState] = useState(true);
  // Width is per tab (design §4.5): the active tab's entry is what the panel
  // renders, so a switch reads the new tab's remembered width. The ref mirrors
  // the record for the persist callback (writes must not read stale state).
  const [rightPanelWidthByTab, setRightPanelWidthByTab] = useState<Record<RightTab, number>>(
    defaultPanelWidths,
  );
  const rightPanelWidth = rightPanelWidthByTab[rightTab];
  const [panelBadge, setPanelBadge] = useState<Partial<Record<RightTab, number>>>({});
  // Tab order + visibility (Settings → 通用设置). The order holds EVERY tab —
  // a hidden one keeps its slot, so unhiding restores its position instead of
  // dropping it at the end. Visibility is a separate set.
  const [rightTabOrder, setRightTabOrderState] = useState<RightTab[]>(ALL_RIGHT_TAB_IDS);
  const [rightTabsHidden, setRightTabsHiddenState] = useState<RightTab[]>([]);
  const rightTabOrderRef = useRef<RightTab[]>(ALL_RIGHT_TAB_IDS);
  const rightTabsHiddenRef = useRef<RightTab[]>([]);
  // Cheap enough to recompute per render (six ids) — no memo needed.
  const visibleRightTabs = visibleTabOrder(rightTabOrder, rightTabsHidden);
  const rightPanelOpenRef = useRef(true);
  const rightPanelWidthByTabRef = useRef<Record<RightTab, number>>(rightPanelWidthByTab);
  // Active tab mirror for callbacks that act on "the current tab" without
  // re-creating on every tab switch (persist / setWidth / legacy migration).
  const rightTabRef = useRef<RightTab>(rightTab);
  useEffect(() => {
    rightTabRef.current = rightTab;
  }, [rightTab]);

  // ---- code panel (docs/code-panel-design.md §4.5) ----
  const [codeRootId, setCodeRootIdState] = useState<string | null>(null);
  const [codePanelMode, setCodePanelModeState] = useState<"file" | "side">("file");
  const [codeTreeOpen, setCodeTreeOpenState] = useState(false);
  const [codeOpenRequest, setCodeOpenRequest] = useState<CodeOpenRequest | null>(null);
  // Agent file changes, pushed over the session socket (design §4.7 S3). The
  // cumulative map feeds the tree's marks; `codeLastChange` exists so a
  // consumer reacts once per event rather than rescanning the map.
  const [codeTouched, setCodeTouched] = useState<Record<string, CodeTouch>>({});
  const [codeLastChange, setCodeLastChange] = useState<
    (CodeTouch & { path: string; nonce: number }) | null
  >(null);
  const codeChangeNonceRef = useRef(0);
  const notifyCodeChange = useCallback(
    (change: { path: string; op: "write" | "edit"; version: string }) => {
      const touch: CodeTouch = { op: change.op, version: change.version, at: Date.now() };
      setCodeTouched((prev) => ({ ...prev, [change.path]: touch }));
      codeChangeNonceRef.current += 1;
      setCodeLastChange({ ...touch, path: change.path, nonce: codeChangeNonceRef.current });
    },
    [],
  );
  const codeOpenNonceRef = useRef(0);
  // Artifact ids that arrived while collapsed — replayed as a pulse on reopen
  // so the highlight isn't lost to the hidden window.
  const pendingFlashRef = useRef<string[]>([]);

  const persistPanelPrefs = useCallback(() => {
    try {
      localStorage.setItem(
        PANEL_PREFS_KEY,
        JSON.stringify({
          open: rightPanelOpenRef.current,
          widths: rightPanelWidthByTabRef.current,
          order: rightTabOrderRef.current,
          hidden: rightTabsHiddenRef.current,
        }),
      );
    } catch {
      /* storage unavailable */
    }
  }, []);

  // Hydrate persisted prefs once on the client (SSR renders defaults, the
  // effect fixes them up after mount — same pattern as ginno-theme).
  useEffect(() => {
    try {
      const raw = localStorage.getItem(PANEL_PREFS_KEY);
      if (!raw) return;
      const v = JSON.parse(raw) as {
        open?: unknown;
        width?: unknown;
        widths?: unknown;
        order?: unknown;
        hidden?: unknown;
      };
      if (typeof v.open === "boolean") {
        rightPanelOpenRef.current = v.open;
        setRightPanelOpenState(v.open);
      }
      if (v.widths && typeof v.widths === "object") {
        // Current format {open, widths:{tab:px}}.
        const saved = v.widths as Partial<Record<RightTab, number>>;
        const next = defaultPanelWidths();
        (Object.keys(next) as RightTab[]).forEach((tab) => {
          const w = saved[tab];
          if (typeof w === "number" && Number.isFinite(w)) {
            next[tab] = Math.min(panelWidthMax(tab), Math.max(PANEL_WIDTH_MIN, Math.round(w)));
          }
        });
        rightPanelWidthByTabRef.current = next;
        setRightPanelWidthByTab(next);
      } else if (typeof v.width === "number") {
        // Legacy format {open, width}: one width for every tab. Migrate it onto
        // the tab active at hydration time so the user keeps their setting;
        // the other tabs start at the default.
        const w = Math.min(PANEL_WIDTH_MAX, Math.max(PANEL_WIDTH_MIN, Math.round(v.width)));
        const tab = rightTabRef.current;
        const next = { ...rightPanelWidthByTabRef.current, [tab]: w };
        rightPanelWidthByTabRef.current = next;
        setRightPanelWidthByTab(next);
      }
      // Tab order/visibility. Canonicalised, so a stale preference (a tab that
      // no longer exists, or one shipped since it was saved) resolves safely.
      const order = canonicalTabOrder(v.order);
      const hidden = Array.isArray(v.hidden)
        ? v.hidden.filter((h): h is RightTab => typeof h === "string")
        : [];
      rightTabOrderRef.current = order;
      rightTabsHiddenRef.current = hidden;
      setRightTabOrderState(order);
      setRightTabsHiddenState(hidden);
      // The default tab may be hidden — land on the first visible one, or the
      // panel would open onto an empty strip.
      const vis = visibleTabOrder(order, hidden);
      if (!vis.includes(rightTabRef.current)) {
        rightTabRef.current = vis[0];
        setRightTabState(vis[0]);
      }
    } catch {
      /* corrupted prefs — keep defaults */
    }
  }, []);

  // ---- 侧栏子树折叠覆盖（ginno-sidebar-tree，自 AppShell 本地状态上收）----
  const [treeCollapsed, setTreeCollapsedState] = useState<Record<string, boolean>>({});
  // Ref mirror for write-time reads（persist/prune 不能读过期 state——与
  // rightPanelWidthByTabRef 同理），外加一份 sessions 镜像供写时裁剪。
  const treeCollapsedRef = useRef<Record<string, boolean>>({});
  const sessionsRef = useRef<SessionMeta[]>([]);
  useEffect(() => {
    sessionsRef.current = sessions;
  }, [sessions]);

  // Hydrate once on mount（与上方面板偏好同一模式：SSR 渲染默认值，挂载后修）。
  useEffect(() => {
    try {
      const raw = localStorage.getItem(TREE_COLLAPSED_KEY);
      if (!raw) return;
      const v = JSON.parse(raw) as Record<string, unknown>;
      if (!v || typeof v !== "object") return;
      const next: Record<string, boolean> = {};
      for (const [k, val] of Object.entries(v)) {
        if (typeof val === "boolean") next[k] = val;
      }
      treeCollapsedRef.current = next;
      setTreeCollapsedState(next);
    } catch {
      /* corrupted prefs — keep defaults */
    }
  }, []);

  // Persist a write，顺带裁掉已不存在的会话键（删除后不留陈旧残留）。boot 尚未
  // 拿到会话列表时跳过裁剪——空镜像会把整张表清光。
  const persistTreeCollapsed = useCallback((map: Record<string, boolean>) => {
    let next = map;
    if (sessionsRef.current.length) {
      const alive = new Set(sessionsRef.current.map((s) => s.id));
      const pruned: Record<string, boolean> = {};
      for (const [k, v] of Object.entries(map)) {
        if (alive.has(k)) pruned[k] = v;
      }
      next = pruned;
    }
    treeCollapsedRef.current = next;
    setTreeCollapsedState(next);
    try {
      localStorage.setItem(TREE_COLLAPSED_KEY, JSON.stringify(next));
    } catch {
      /* storage unavailable */
    }
  }, []);

  const setTreeCollapsed = useCallback(
    (id: string, collapsed: boolean) => {
      persistTreeCollapsed({ ...treeCollapsedRef.current, [id]: collapsed });
    },
    [persistTreeCollapsed],
  );

  const setRightPanelOpen = useCallback(
    (open: boolean) => {
      rightPanelOpenRef.current = open;
      setRightPanelOpenState(open);
      if (open) {
        // Reopening consumes the badges and replays the missed-arrival pulse.
        setPanelBadge({});
        if (pendingFlashRef.current.length) {
          const ids = pendingFlashRef.current;
          pendingFlashRef.current = [];
          setFlashArtifactIds(ids);
          window.setTimeout(() => setFlashArtifactIds([]), 2500);
        }
      }
      persistPanelPrefs();
    },
    [persistPanelPrefs],
  );

  const setRightPanelWidth = useCallback(
    (w: number) => {
      const tab = rightTabRef.current;
      const cw = Math.min(panelWidthMax(tab), Math.max(PANEL_WIDTH_MIN, Math.round(w)));
      // Update the ref eagerly (persistPanelPrefs reads it synchronously).
      const next = { ...rightPanelWidthByTabRef.current, [tab]: cw };
      rightPanelWidthByTabRef.current = next;
      setRightPanelWidthByTab(next);
      persistPanelPrefs();
    },
    [persistPanelPrefs],
  );

  const clearPanelBadge = useCallback((tab?: RightTab) => {
    setPanelBadge((prev) => {
      if (!tab) return Object.keys(prev).length ? {} : prev;
      if (!(tab in prev)) return prev;
      const next = { ...prev };
      delete next[tab];
      return next;
    });
  }, []);

  /** Persist a new tab order. Canonicalised, so callers may pass a partial or
   *  stale list safely (the settings UI always passes a complete one). */
  const setRightTabOrder = useCallback(
    (order: RightTab[]) => {
      const next = canonicalTabOrder(order);
      rightTabOrderRef.current = next;
      setRightTabOrderState(next);
      persistPanelPrefs();
    },
    [persistPanelPrefs],
  );

  const setRightTabHidden = useCallback(
    (id: RightTab, hidden: boolean) => {
      const visibleNow = visibleTabOrder(rightTabOrderRef.current, rightTabsHiddenRef.current);
      // Refuse to hide the last visible tab: an empty strip leaves the panel
      // with nothing to click, so it could never be brought back.
      if (hidden && visibleNow.length <= 1 && visibleNow[0] === id) return;
      const next = hidden
        ? Array.from(new Set([...rightTabsHiddenRef.current, id]))
        : rightTabsHiddenRef.current.filter((t) => t !== id);
      rightTabsHiddenRef.current = next;
      setRightTabsHiddenState(next);
      // A hidden tab must not stay active, or the body would render a panel
      // whose tab is no longer in the strip.
      if (hidden && rightTabRef.current === id) {
        const fallback = visibleTabOrder(rightTabOrderRef.current, next)[0];
        rightTabRef.current = fallback;
        setRightTabState(fallback);
      }
      persistPanelPrefs();
    },
    [persistPanelPrefs],
  );

  /** Back to the shipped order with every tab shown. */
  const resetRightTabs = useCallback(() => {
    rightTabOrderRef.current = ALL_RIGHT_TAB_IDS;
    rightTabsHiddenRef.current = [];
    setRightTabOrderState(ALL_RIGHT_TAB_IDS);
    setRightTabsHiddenState([]);
    persistPanelPrefs();
  }, [persistPanelPrefs]);

  /**
   * Activate a tab, un-hiding it first if the user had hidden it.
   *
   * Every PROGRAMMATIC switch goes through here (chat jump, notification click,
   * artifacts auto-follow). Activating a hidden tab would render a panel body
   * whose tab is absent from the strip, leaving the strip with no selection at
   * all. An explicit jump is a strong enough signal of intent that re-showing
   * the tab is the right resolution — silently doing nothing would be worse.
   * User clicks from the strip always name a visible tab, so this is a no-op
   * on that path.
   */
  const activateRightTab = useCallback(
    (tab: RightTab) => {
      if (rightTabsHiddenRef.current.includes(tab)) {
        const next = rightTabsHiddenRef.current.filter((t) => t !== tab);
        rightTabsHiddenRef.current = next;
        setRightTabsHiddenState(next);
        persistPanelPrefs();
      }
      setRightTabState(tab);
    },
    [persistPanelPrefs],
  );

  const activeSessionRef = useRef<string | null>(null);
  useEffect(() => {
    activeSessionRef.current = activeSessionId;
  }, [activeSessionId]);

  // ---- workflow run 视图 ----
  const openRunView = useCallback((runId: string) => setActiveRunId(runId), []);
  const closeRunView = useCallback(() => setActiveRunId(null), []);

  // ---- 定时任务回放（与 run 视图互斥：互相打开时关掉对方）----
  const [scheduleRun, setScheduleRun] = useState<import("./types").ScheduleRun | null>(null);
  const openScheduleRun = useCallback((run: import("./types").ScheduleRun) => {
    setScheduleRun(run);
    setActiveRunId(null);
  }, []);
  const closeScheduleRun = useCallback(() => setScheduleRun(null), []);

  // Consumer-facing setter：切换/清空会话时同步关掉 run 视图（中央区二选一）。
  // 内部 boot 路径仍用裸 setActiveSessionId；newSession / removeSession 里也
  // 各自清一次。
  const setActiveSession = useCallback((id: string | null) => {
    setActiveSessionId(id);
    setActiveRunId(null);
    setScheduleRun(null);
  }, []);

  // The session scope artifacts were last loaded for. When the scope changes
  // (session switch/create/delete) the whole list swaps, which must NOT trigger
  // the fresh-artifact auto-follow/pulse (that's only for genuinely new rows
  // arriving within the same session).
  const artifactScopeRef = useRef<string | null | undefined>(undefined);

  // Visiting the Workflow tab marks every currently-failed run as seen, which
  // clears the red badge. New failures arriving afterwards light it up again.
  const markFailedRunsSeen = useCallback(() => {
    setFailedSeen((prev) => {
      const next = new Set(prev);
      for (const r of workflowRunsRef.current) {
        if (r.status === "failed") next.add(r.id);
      }
      return next;
    });
  }, []);

  const setRightTab = useCallback(
    (tab: RightTab, opts?: { manual?: boolean }) => {
      if (opts?.manual) {
        // explicit user choice: stop auto-following unless they picked Artifacts
        setArtifactsFollow(tab === "artifacts");
      }
      if (tab === "workflow") markFailedRunsSeen();
      activateRightTab(tab);
    },
    [markFailedRunsSeen, activateRightTab],
  );

  // ---- code panel actions (docs/code-panel-design.md §3.4) ----
  const setCodeRootId = useCallback((id: string) => {
    setCodeRootIdState(id);
  }, []);
  const setCodePanelMode = useCallback((m: "file" | "side") => {
    setCodePanelModeState(m);
  }, []);
  const setCodeTreeOpen = useCallback((v: boolean) => {
    setCodeTreeOpenState(v);
  }, []);
  const clearCodeOpenRequest = useCallback(() => {
    setCodeOpenRequest(null);
  }, []);
  // Unified jump-from-chat entry point: switch to the code tab, open the panel
  // if collapsed, and publish an open request. Deliberately does NOT touch the
  // width (design §3.4: respecting the user's width; the tree auto-collapses so
  // a narrow panel stays usable). The panel expands the tree to the file and
  // opens it; `line` rides along for Monaco's reveal.
  const openInCode = useCallback(
    (target: { rootId: string; path: string; line?: number }) => {
      setCodeRootIdState(target.rootId);
      setRightTab("code");
      if (!rightPanelOpenRef.current) setRightPanelOpen(true);
      codeOpenNonceRef.current += 1;
      setCodeOpenRequest({ ...target, nonce: codeOpenNonceRef.current });
    },
    [setRightTab, setRightPanelOpen],
  );

  const openPreview = useCallback((f: PreviewFile) => {
    setPreviewFile(f);
    setPreviewNonce((n) => n + 1);
  }, []);
  const closePreview = useCallback(() => setPreviewFile(null), []);
  // Only refetch when the invalidated file is the one being viewed.
  const notifyPreviewInvalidate = useCallback((fileId: string) => {
    setPreviewFile((cur) => {
      if (cur && cur.id === fileId) setPreviewNonce((n) => n + 1);
      return cur;
    });
  }, []);

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
      setPanelBadge((p) => ({ ...p, memory: m.draft_pending ? 1 : 0 }));
    } catch {
      /* sidecar down */
    }
  }, []);
  const reloadSessions = useCallback(async () => {
    try {
      setSessions(await api.listSessions());
    } catch {
      /* ignore */
    }
  }, []);
  const reloadTodos = useCallback(async () => {
    try {
      setTodos(await api.listTodos());
    } catch {
      /* ignore */
    }
  }, []);
  const reloadProviders = useCallback(async () => {
    try {
      const r = await api.getProviders();
      setProviders(r.providers);
      setDefaultProvider(r.default_provider);
    } catch {
      /* ignore */
    }
  }, []);
  const reloadWorkflows = useCallback(async () => {
    try {
      setWorkflows(await api.listWorkflows());
    } catch {
      /* ignore */
    }
  }, []);
  const reloadWorkflowRuns = useCallback(async () => {
    try {
      const runs = await api.listWorkflowRuns();
      setWorkflowRuns(runs);
      // Browser notifications (workflow-ux-redesign P3): when the tab is in
      // the background, announce terminal transitions the user would otherwise
      // miss. Permission is requested lazily the first time any run goes
      // active (never a cold-start prompt). Clicking opens the Workflow tab.
      notifyRunTransitions(runs, () => {
        // Explicit user intent (they clicked the notification) — reveal the
        // tab even if they had hidden it. Contrast reloadArtifacts below,
        // where a BACKGROUND event must respect the hidden preference.
        activateRightTab("workflow");
        if (!rightPanelOpenRef.current) setRightPanelOpen(true);
      });
      // Prune stale tool-activity entries (deleted runs / runs that finished
      // between WS events — e.g. the poll path misses a tool_result).
      setLiveToolActivity((prev) => {
        const alive = new Set(runs.filter((r) => r.status === "running").map((r) => r.id));
        const stale = Object.keys(prev).filter((id) => !alive.has(id));
        if (!stale.length) return prev;
        const next = { ...prev };
        for (const id of stale) delete next[id];
        return next;
      });
      // Maintain the failed-seen set for the red badge: seed once at boot with
      // the existing failures (they're history, not news), then keep the set
      // intersected with runs that still exist (deleted runs drop off). New
      // failures are deliberately NOT marked seen here.
      const failedIds = runs.filter((r) => r.status === "failed").map((r) => r.id);
      setFailedSeen((prev) => {
        if (!failedSeedRef.current) {
          failedSeedRef.current = true;
          return new Set(failedIds);
        }
        const next = new Set<string>();
        for (const id of prev) if (failedIds.includes(id)) next.add(id);
        return next;
      });
    } catch {
      /* ignore */
    }
  }, []);

  const reloadSynthesisCases = useCallback(async () => {
    try {
      const r = await api.listSynthesisCases(200);
      setSynthesisCases(r.cases || []);
    } catch {
      /* sidecar down */
    }
  }, []);

  // Fallback poll (work item E): the session WS pushes run.status /
  // workflows.changed and keeps the badge fresh in normal operation. This slow
  // 30s sweep only covers the gaps — no session WS connected (headless runs at
  // boot) or the reconnect window. Cheap: one small JSON read.
  useEffect(() => {
    const t = setInterval(() => void reloadWorkflowRuns(), 30000);
    return () => clearInterval(t);
  }, [reloadWorkflowRuns]);
  const reloadArtifacts = useCallback(async () => {
    try {
      // Artifacts belong to the session: scope the fetch to the active session
      // (null → unscoped, which only happens in the boot gap before a session
      // is selected).
      const scope = activeSessionRef.current;
      const next = await api.listArtifacts("default", scope ?? undefined);
      const scopeChanged = artifactScopeRef.current !== scope;
      artifactScopeRef.current = scope;

      // §7.6 auto-follow: new artifact for the ACTIVE session → switch to the
      // Artifacts tab (if follow is on) and pulse-highlight the rows; when the
      // panel is collapsed, badge the edge dock instead and queue the pulse
      // for the next reopen (right-panel-redesign.md §3.6). Skip on a scope
      // change (session switch) — that swaps the whole list, it isn't a fresh
      // arrival. Diff + side effects stay OUTSIDE the setState updater:
      // StrictMode double-invokes updaters in dev and would double-count
      // badges/flashes.
      const prevIds = new Set(artifactsListRef.current.map((a) => a.id));
      const fresh = next.filter((a) => !prevIds.has(a.id));
      const mine = fresh.filter(
        (a) => !a.session_id || a.session_id === activeSessionRef.current,
      );
      setArtifacts(next);
      if (!scopeChanged && mine.length) {
        if (!rightPanelOpenRef.current) {
          setPanelBadge((prev) => ({
            ...prev,
            artifacts: (prev.artifacts ?? 0) + mine.length,
          }));
          pendingFlashRef.current.push(...mine.map((a) => a.id));
        }
        if (artifactsFollowRef.current && !rightTabsHiddenRef.current.includes("artifacts")) {
          // Silent when collapsed: pre-select the tab so expanding lands on
          // Artifacts, but never yank the panel open.
          //
          // A HIDDEN Artifacts tab stays hidden: this is a background arrival,
          // not user intent, so un-hiding here would silently undo a
          // deliberate preference. The badge/flash path above still carries
          // the signal.
          setRightTabState("artifacts");
          if (rightPanelOpenRef.current) {
            setFlashArtifactIds(mine.map((a) => a.id));
            window.setTimeout(() => setFlashArtifactIds([]), 2500);
          }
        }
      }
    } catch {
      /* ignore */
    }
  }, []);

  // Rescope the artifacts panel whenever the active session changes (switch /
  // create / delete), so it always shows the current session's artifacts.
  useEffect(() => {
    reloadArtifacts();
  }, [activeSessionId, reloadArtifacts]);

  // Reference-only delete: the file on disk is untouched, so a mistaken
  // delete is recoverable. Optimistic remove with rollback if the sidecar
  // rejects or is unreachable, so the panel never diverges from disk.
  const removeArtifact = useCallback(async (id: string) => {
    let snapshot: Artifact[] | null = null;
    setArtifacts((prev) => {
      snapshot = prev;
      return prev.filter((a) => a.id !== id);
    });
    try {
      const r = await api.deleteArtifact(id);
      if (!r.ok) throw new Error("delete failed");
    } catch {
      if (snapshot) setArtifacts(snapshot);
    }
  }, []);

  // Inspector edits: optimistic update, server round-trip, rollback on
  // rejection (e.g. blank name) — and reconcile with the canonical record
  // the server returns (whitelisted + trimmed).
  const patchArtifact = useCallback(async (id: string, patch: ArtifactPatch) => {
    const artPatch: Partial<Artifact> = {};
    if (patch.name !== undefined) artPatch.name = patch.name;
    if (patch.kind !== undefined) artPatch.kind = patch.kind;
    if (patch.schema !== undefined) artPatch.schema = patch.schema;
    let snapshot: Artifact[] | null = null;
    setArtifacts((prev) => {
      snapshot = prev;
      return prev.map((a) => (a.id === id ? { ...a, ...artPatch } : a));
    });
    try {
      const r = await api.updateArtifact(id, patch);
      if (!r.ok) throw new Error(r.error || uiText("net.updateFailed"));
      if (r.artifact) {
        const canonical = r.artifact;
        setArtifacts((prev) => prev.map((a) => (a.id === id ? { ...a, ...canonical } : a)));
      }
      return { ok: true };
    } catch (e) {
      if (snapshot) setArtifacts(snapshot);
      return { ok: false, error: e instanceof Error ? e.message : uiText("net.updateFailed") };
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
  ]);

  const creatingRef = useRef(false);
  const newSession = useCallback(
    async (
      agent_id?: string,
      opts?: { title?: string; provider?: string; model?: string; workflow_id?: string },
    ) => {
      if (creatingRef.current) return null;
      creatingRef.current = true;
      setSessionError(null);
      try {
        const s = await api.createSession({
          workspace: process.env.NEXT_PUBLIC_WORKSPACE ?? "/tmp/gw",
          agent_id,
          title: opts?.title,
          provider: opts?.provider,
          model: opts?.model,
          workflow_id: opts?.workflow_id,
        });
        if (s && s.ok !== false && s.id) {
          setSessions((prev) => [s, ...prev.filter((x) => x.id !== s.id)]);
          setActiveSessionId(s.id);
          setActiveRunId(null); // 视图切到新会话，run 视图/定时回放一并关闭
          setScheduleRun(null);
          setSessionError(null);
          return s;
        }
        // server returned ok:false (e.g. no provider enabled / missing key)
        setSessionError(s?.error || "Failed to create session: enable a model provider in Settings → Model API");
      } catch {
        setSessionError("Failed to create session: cannot reach the runtime (is the sidecar running?)");
      } finally {
        creatingRef.current = false;
      }
      return null;
    },
    [],
  );

  const patchTodo = useCallback(async (id: string, patch: Partial<Todo>) => {
    // Optimistic toggle, but roll back if the server rejects or is unreachable
    // so the UI never silently diverges from disk (the old `catch {}` swallowed
    // the failure with no rollback and no feedback).
    let snapshot: Todo[] | null = null;
    setTodos((prev) => {
      snapshot = prev;
      return prev.map((t) =>
        t.id === id
          ? {
              ...t,
              ...patch,
              completed_at: patch.done ? Date.now() / 1000 : patch.done === false ? null : t.completed_at,
            }
          : t,
      );
    });
    try {
      const r = await api.updateTodo(id, patch);
      if (!r.ok) throw new Error(r.error || uiText("net.updateFailed"));
    } catch {
      if (snapshot) setTodos(snapshot);
    }
  }, []);

  const setSessionAgent = useCallback((id: string, agentId: string) => {
    setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, agent_id: agentId } : s)));
    api
      .patchSession(id, { agent_id: agentId })
      .then((r) => {
        // reconcile with the server-computed title (auto titles follow the agent)
        if (r?.session) {
          setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, ...r.session } : s)));
        }
      })
      .catch(() => {
        /* ignore */
      });
  }, []);

  const removeSession = useCallback(async (id: string, opts?: { cascade?: boolean }) => {
    // optimistic remove; if it was active, land on home (lazy creation will
    // make the next send start a fresh session — no phantom auto-session).
    // 级联删除（subagent-design.md §5.8）时把全部后代一并从列表移除——后端沿
    // parent_session_id 反向索引删除，前端这里只做 UI 即时一致性。
    // 被删集合先在 sessions 镜像上算出（含嵌套后代）——折叠覆盖的清理复用它。
    const doomed = new Set<string>([id]);
    if (opts?.cascade) {
      let grew = true;
      while (grew) {
        grew = false;
        for (const s of sessionsRef.current) {
          if (s.parent_session_id && doomed.has(s.parent_session_id) && !doomed.has(s.id)) {
            doomed.add(s.id);
            grew = true;
          }
        }
      }
    }
    // 被删会话的折叠覆盖键一并剪掉并持久化（不走 persistTreeCollapsed：它按
    // sessions 镜像裁剪，而镜像此刻还没更新，会把待删键又加回来）。
    const doomedKeys = Object.keys(treeCollapsedRef.current).filter((k) => doomed.has(k));
    if (doomedKeys.length) {
      const next = { ...treeCollapsedRef.current };
      for (const k of doomedKeys) delete next[k];
      treeCollapsedRef.current = next;
      setTreeCollapsedState(next);
      try {
        localStorage.setItem(TREE_COLLAPSED_KEY, JSON.stringify(next));
      } catch {
        /* storage unavailable */
      }
    }
    // 正在查看的会话（或其祖先被级联删除）没了，run 视图也一并关闭。
    const cur = activeSessionRef.current;
    const activeDoomed = !!cur && (doomed.has(cur) || cur === id);
    setSessions((prev) => {
      const next = prev.filter((s) => !doomed.has(s.id));
      setActiveSessionId((current) => {
        if (current !== id && !(current && doomed.has(current))) return current;
        return null;
      });
      return next;
    });
    if (activeDoomed) setActiveRunId(null);
    if (activeDoomed) setScheduleRun(null);
    try {
      await api.deleteSession(id, opts?.cascade);
    } catch {
      /* ignore — reconcile on next reload */
    }
  }, []);

  const renameSession = useCallback(async (id: string, title: string) => {
    const trimmed = title.trim();
    if (!trimmed) return;
    setSessions((prev) =>
      prev.map((s) => (s.id === id ? { ...s, title: trimmed, title_auto: false } : s)),
    );
    try {
      const r = await api.patchSession(id, { title: trimmed });
      if (r?.session) {
        setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, ...r.session } : s)));
      }
    } catch {
      /* ignore */
    }
  }, []);

  const pinSession = useCallback(async (id: string, pinned: boolean) => {
    setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, pinned } : s)));
    try {
      const r = await api.patchSession(id, { pinned });
      if (r?.session) {
        setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, ...r.session } : s)));
      }
    } catch {
      /* ignore — reconcile on next reload */
    }
  }, []);

  const applySessionPatch = useCallback((id: string, patch: Partial<SessionMeta>) => {
    setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, ...patch } : s)));
  }, []);

  // ---- subagent（subagent-design.md §4.1/§6.1）----
  const notifySubagentSpawned = useCallback((ev: SubagentSpawnEvent) => {
    if (!ev.session_id) return;
    const now = Date.now() / 1000;
    setSessions((prev) => {
      const idx = prev.findIndex((s) => s.id === ev.session_id);
      const patch: Partial<SessionMeta> = {
        type: (ev.type as SessionMeta["type"]) || "subagent",
        parent_session_id: ev.parent_session_id,
        depth: typeof ev.depth === "number" ? ev.depth : 0,
        subagent: {
          goal: ev.goal ?? "",
          constraints: ev.constraints ?? "",
          acceptance: ev.acceptance ?? "",
          origin: ev.origin ?? "agent",
          status: "running",
          result_summary: "",
          agent_type: ev.agent_type ?? "",
        },
      };
      if (idx >= 0) {
        // 已在列表（boot reload 先到）：只叠加子代字段与标题。
        return prev.map((s, i) =>
          i === idx ? { ...s, ...patch, title: ev.title || s.title } : s,
        );
      }
      // 即时事件先于任何 reload：合成一行占位，服务端字段（provider/model 等）
      // 由下一次 reloadSessions 补齐。图标用 boxes（子代理组语义），渲染时子行
      // 以状态 emoji 为主，图标只是兜底。
      return [
        {
          id: ev.session_id,
          title: ev.title || ev.goal || uiText("subtask.fallbackTitle"),
          icon: ev.icon ?? "boxes",
          agent_id: null,
          provider: "",
          model: "",
          created: now,
          updated: now,
          ...patch,
        },
        ...prev,
      ];
    });
  }, []);

  const notifySubagentStatus = useCallback((ev: SubagentStatusEvent) => {
    if (!ev.session_id || !ev.status) return;
    setSessions((prev) =>
      prev.map((s) =>
        s.id === ev.session_id
          ? {
              ...s,
              updated: Date.now() / 1000,
              // delegation 行的终态打在顶层 stop_reason 上(meta 无 subagent 对象);
              // 不回填则回放页的 3s 轮询永不停、侧栏状态点一直转 running
              stop_reason:
                s.type === "delegation"
                  ? ev.status === "done"
                    ? "success"
                    : ev.status === "failed"
                      ? "error"
                      : s.stop_reason
                  : s.stop_reason,
              subagent: s.subagent
                ? {
                    ...s.subagent,
                    status: ev.status,
                    result_summary: ev.result_summary ?? s.subagent.result_summary,
                  }
                : s.subagent,
            }
          : s,
      ),
    );
  }, []);

  const addTodo = useCallback(async (data: Partial<Todo>) => {
    try {
      const r = await api.createTodo(data);
      if (r.ok && r.todo) setTodos((prev) => [...prev, r.todo!]);
    } catch {
      /* ignore */
    }
  }, []);

  // Optimistic remove with rollback (same contract as patchTodo / removeArtifact).
  const removeTodo = useCallback(async (id: string) => {
    let snapshot: Todo[] | null = null;
    setTodos((prev) => {
      snapshot = prev;
      return prev.filter((t) => t.id !== id);
    });
    try {
      const r = await api.deleteTodo(id);
      if (!r.ok) throw new Error("delete failed");
    } catch {
      if (snapshot) setTodos(snapshot);
    }
  }, []);

  const flashArtifacts = useCallback((ids: string[]) => {
    if (!ids.length) return;
    setFlashArtifactIds(ids);
    window.setTimeout(() => setFlashArtifactIds([]), 2500);
  }, []);

  // ---- session goal (goal-design.md) ----
  // Per-session goal snapshot. Fed by (a) an explicit fetch when a session is
  // opened and (b) the live `goal.updated` / `goal.cleared` WS events handled
  // in ChatStream. The TopBar chip + popover read from here.
  const [goalBySession, setGoalBySession] = useState<Record<string, Goal | null>>({});

  const notifyGoal = useCallback((sessionId: string, goal: Goal | null) => {
    setGoalBySession((prev) => ({ ...prev, [sessionId]: goal }));
  }, []);

  const loadGoal = useCallback(async (sessionId: string) => {
    try {
      const r = await api.getSessionGoal(sessionId);
      setGoalBySession((prev) => ({ ...prev, [sessionId]: r?.goal ?? null }));
    } catch {
      /* sidecar down */
    }
  }, []);

  const setGoalObjective = useCallback(
    async (sessionId: string, objective: string, confirm?: boolean) => {
      try {
        const r = await api.setSessionGoal(sessionId, { objective, confirm });
        if (r?.ok && r.goal) setGoalBySession((prev) => ({ ...prev, [sessionId]: r.goal! }));
        return { ok: !!r?.ok, needs_confirm: !!r?.needs_confirm, error: r?.error };
      } catch {
        return { ok: false, error: uiText("net.unreachable") };
      }
    },
    [],
  );

  const setGoalStatus = useCallback(async (sessionId: string, status: GoalStatus) => {
    try {
      const r = await api.setSessionGoal(sessionId, { status });
      if (r?.ok && r.goal) setGoalBySession((prev) => ({ ...prev, [sessionId]: r.goal! }));
      return { ok: !!r?.ok, error: r?.error };
    } catch {
      return { ok: false, error: uiText("net.unreachable") };
    }
  }, []);

  const clearGoal = useCallback(async (sessionId: string) => {
    try {
      await api.clearSessionGoal(sessionId);
    } catch {
      /* ignore */
    }
    setGoalBySession((prev) => ({ ...prev, [sessionId]: null }));
  }, []);

  // Workflow tab badge counts (work item E), derived on each render.
  const activeRunCount = workflowRuns.filter(
    (r) => r.status === "running" || r.status === "paused",
  ).length;
  const unseenFailedCount = workflowRuns.filter(
    (r) => r.status === "failed" && !failedSeen.has(r.id),
  ).length;
  // Paused runs waiting on a HUMAN answer (workflow-ux-redesign P1) — a
  // stronger signal than "running": somebody must act. Drives the yellow dock
  // badge; version_propose interrupts live in the session graph, not runs.
  const pendingHumanCount = workflowRuns.filter(
    (r) => r.status === "paused" && r.pending_interrupt?.kind === "human",
  ).length;

  // Synthesis cases in flight (no output.json yet AND the in-process task is
  // alive). Restarted/crashed cases read running=false → they show as 未完成
  // and deliberately do NOT light the pulse.
  const synthesisActiveCount = synthesisCases.filter((c) => !c.status && c.running).length;

  // Adaptive stuck detection (P3): recent completed-run durations per workflow
  // (last 10). LiveRunBlock flags a step stuck after max(60s, avg×3) instead
  // of a fixed 5-minute window.
  const runDurationByWorkflow: Record<string, number[]> = {};
  for (const r of workflowRuns) {
    if (r.status === "done" && r.finished) {
      const d = r.finished - r.started;
      if (d > 0) (runDurationByWorkflow[r.workflow_id] ??= []).push(d);
    }
  }
  for (const k of Object.keys(runDurationByWorkflow)) {
    runDurationByWorkflow[k] = runDurationByWorkflow[k].slice(-10);
  }

  const value: GinnoState = {
    agents,
    skills,
    sessions,
    todos,
    workflows,
    workflowRuns,
    artifacts,
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
    setGoalObjective,
    setGoalStatus,
    clearGoal,
  };

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
