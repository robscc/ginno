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
import { notifyNative } from "./desktop";
import type { AgentConfig, Artifact, ArtifactPatch, FileEntry, Goal, GoalStatus, Providers, SessionMeta, SkillSummary, Todo, WorkflowDef, WorkflowRun } from "./types";

export type RightTab = "todo" | "workflow" | "artifacts" | "memory";

// Right panel width bounds (right-panel-redesign.md §3.4). The panel renders
// at `rightPanelWidth`; dragging clamps into this range, double-click resets.
export const PANEL_WIDTH_MIN = 280;
export const PANEL_WIDTH_MAX = 560;
export const PANEL_WIDTH_DEFAULT = 380;

// localStorage key for the persisted right-panel prefs ({open, width}).
const PANEL_PREFS_KEY = "ginno-right-panel";
// Last active session, restored on boot (open-experience redesign). Only real
// ids are stored; visiting home (null) keeps the previous id so a relaunch
// still resumes where the user left off.
export const LAST_SESSION_KEY = "ginno-last-session";

export interface PreviewFile {
  id: string;
  name: string;
  path: string;
  kind?: string;
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
  // Master gate (Settings → Notifications). _prevRunStatus keeps updating even
  // while disabled so re-enabling doesn't fire a burst of stale transitions.
  let enabled = true;
  try {
    enabled = typeof localStorage !== "undefined" && localStorage.getItem("ginno-notify") !== "0";
  } catch {
    /* ignore */
  }
  const hidden = typeof document !== "undefined" && document.visibilityState !== "visible";
  for (const r of runs) {
    const prev = _prevRunStatus[r.id];
    _prevRunStatus[r.id] = r.status;
    if (prev === undefined || prev === r.status) continue;
    if (!enabled) continue;
    if (!hidden) continue; // user is looking — the badges already cover it
    let body: string;
    if (r.status === "done") {
      body = `已完成 · 用时 ${fmtRunElapsed(r)}`;
    } else if (r.status === "failed") {
      const failed = (r.steps || []).find((s) => s.status === "failed");
      body = failed?.title ? `失败于「${failed.title}」` : "执行失败";
    } else {
      continue;
    }
    const title = `${r.status === "done" ? "✓" : "✕"} ${r.name || "Workflow"}`;
    // Desktop: the Tauri shell fires a real macOS notification (WKWebView has
    // no window.Notification). Click → window focus + open the Workflow panel.
    void notifyNative({ kind: "workflow-run", id: r.id, title, body }).then((sent) => {
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
  rightPanelWidth: number; // px, clamped to [PANEL_WIDTH_MIN, PANEL_WIDTH_MAX]
  // Unread counts accumulated while the panel was collapsed (v1: artifacts).
  panelBadge: Partial<Record<RightTab, number>>;
  setRightPanelOpen: (open: boolean) => void;
  setRightPanelWidth: (w: number) => void;
  clearPanelBadge: (tab?: RightTab) => void; // omit tab → clear all
  openPreview: (f: PreviewFile) => void;
  closePreview: () => void;
  notifyPreviewInvalidate: (fileId: string) => void;
  reloadAgents: () => Promise<void>;
  reloadSkills: () => Promise<void>;
  reloadSessions: () => Promise<void>;
  reloadTodos: () => Promise<void>;
  reloadProviders: () => Promise<void>;
  reloadWorkflows: () => Promise<void>;
  reloadWorkflowRuns: () => Promise<void>;
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
    opts?: { title?: string; provider?: string; model?: string },
  ) => Promise<SessionMeta | null>;
  setSessionAgent: (id: string, agentId: string) => void;
  removeSession: (id: string) => Promise<void>;
  renameSession: (id: string, title: string) => Promise<void>;
  // Merge a server-pushed or optimistic partial into one session's meta
  // (session_title WS events, model-switch reconcile).
  applySessionPatch: (id: string, patch: Partial<SessionMeta>) => void;
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
  const [rightPanelWidth, setRightPanelWidthState] = useState(PANEL_WIDTH_DEFAULT);
  const [panelBadge, setPanelBadge] = useState<Partial<Record<RightTab, number>>>({});
  const rightPanelOpenRef = useRef(true);
  const rightPanelWidthRef = useRef(PANEL_WIDTH_DEFAULT);
  // Artifact ids that arrived while collapsed — replayed as a pulse on reopen
  // so the highlight isn't lost to the hidden window.
  const pendingFlashRef = useRef<string[]>([]);

  const persistPanelPrefs = useCallback(() => {
    try {
      localStorage.setItem(
        PANEL_PREFS_KEY,
        JSON.stringify({ open: rightPanelOpenRef.current, width: rightPanelWidthRef.current }),
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
      const v = JSON.parse(raw) as { open?: unknown; width?: unknown };
      if (typeof v.open === "boolean") {
        rightPanelOpenRef.current = v.open;
        setRightPanelOpenState(v.open);
      }
      if (typeof v.width === "number") {
        const w = Math.min(PANEL_WIDTH_MAX, Math.max(PANEL_WIDTH_MIN, v.width));
        rightPanelWidthRef.current = w;
        setRightPanelWidthState(w);
      }
    } catch {
      /* corrupted prefs — keep defaults */
    }
  }, []);

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
      const cw = Math.min(PANEL_WIDTH_MAX, Math.max(PANEL_WIDTH_MIN, Math.round(w)));
      rightPanelWidthRef.current = cw;
      setRightPanelWidthState(cw);
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

  const activeSessionRef = useRef<string | null>(null);
  useEffect(() => {
    activeSessionRef.current = activeSessionId;
  }, [activeSessionId]);

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
      setRightTabState(tab);
    },
    [markFailedRunsSeen],
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
        setRightTabState("workflow");
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
        if (artifactsFollowRef.current) {
          // Silent when collapsed: pre-select the tab so expanding lands on
          // Artifacts, but never yank the panel open.
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
      if (!r.ok) throw new Error(r.error || "update failed");
      if (r.artifact) {
        const canonical = r.artifact;
        setArtifacts((prev) => prev.map((a) => (a.id === id ? { ...a, ...canonical } : a)));
      }
      return { ok: true };
    } catch (e) {
      if (snapshot) setArtifacts(snapshot);
      return { ok: false, error: e instanceof Error ? e.message : "更新失败" };
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
        reloadArtifacts(),
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
    reloadArtifacts,
  ]);

  const creatingRef = useRef(false);
  const newSession = useCallback(
    async (
      agent_id?: string,
      opts?: { title?: string; provider?: string; model?: string },
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
        });
        if (s && s.ok !== false && s.id) {
          setSessions((prev) => [s, ...prev.filter((x) => x.id !== s.id)]);
          setActiveSessionId(s.id);
          setSessionError(null);
          return s;
        }
        // server returned ok:false (e.g. no provider enabled / missing key)
        setSessionError(s?.error || "新建会话失败：请在 设置 → 模型 API 启用一个模型提供商");
      } catch {
        setSessionError("新建会话失败：无法连接运行时（sidecar 未启动？）");
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
      if (!r.ok) throw new Error(r.error || "update failed");
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

  const removeSession = useCallback(async (id: string) => {
    // optimistic remove; if it was active, land on home (lazy creation will
    // make the next send start a fresh session — no phantom auto-session)
    setSessions((prev) => {
      const next = prev.filter((s) => s.id !== id);
      setActiveSessionId((cur) => {
        if (cur !== id) return cur;
        return null;
      });
      return next;
    });
    try {
      await api.deleteSession(id);
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

  const applySessionPatch = useCallback((id: string, patch: Partial<SessionMeta>) => {
    setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, ...patch } : s)));
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
        return { ok: false, error: "无法连接运行时" };
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
      return { ok: false, error: "无法连接运行时" };
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
    setActiveSession: setActiveSessionId,
    setRightTab,
    rightPanelOpen,
    rightPanelWidth,
    panelBadge,
    setRightPanelOpen,
    setRightPanelWidth,
    clearPanelBadge,
    openPreview,
    closePreview,
    notifyPreviewInvalidate,
    reloadAgents,
    reloadSkills,
    reloadSessions,
    reloadTodos,
    reloadProviders,
    reloadWorkflows,
    reloadWorkflowRuns,
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
    renameSession,
    applySessionPatch,
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
