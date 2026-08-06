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
import type { AgentConfig, Artifact, ArtifactPatch, FileEntry, Goal, GoalStatus, Providers, SessionMeta, SkillSummary, Todo, WorkflowDef, WorkflowRun } from "./types";

export type RightTab = "todo" | "workflow" | "artifacts" | "memory";

export interface PreviewFile {
  id: string;
  name: string;
  path: string;
  kind?: string;
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
  reloadArtifacts: () => Promise<void>;
  removeArtifact: (id: string) => Promise<void>;
  patchArtifact: (id: string, patch: ArtifactPatch) => Promise<{ ok: boolean; error?: string }>;
  newSession: (agent_id?: string, opts?: { title?: string }) => Promise<SessionMeta | null>;
  setSessionAgent: (id: string, agentId: string) => void;
  removeSession: (id: string) => Promise<void>;
  renameSession: (id: string, title: string) => Promise<void>;
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
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [providers, setProviders] = useState<Providers>({});
  const [defaultProvider, setDefaultProvider] = useState("custom");
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [ready, setReady] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);

  // Right panel: tab is store-owned so chat events can auto-switch to
  // Artifacts when the active session gains one (docs §7.6). Manual clicks
  // turn autoFollow off (sticky) so the agent can't yank focus repeatedly;
  // visiting Artifacts manually or starting fresh re-enables it.
  const [rightTab, setRightTabState] = useState<RightTab>("todo");
  const [artifactsFollow, setArtifactsFollow] = useState(true);
  const [flashArtifactIds, setFlashArtifactIds] = useState<string[]>([]);
  const [previewFile, setPreviewFile] = useState<PreviewFile | null>(null);
  const [previewNonce, setPreviewNonce] = useState(0);

  const activeSessionRef = useRef<string | null>(null);
  useEffect(() => {
    activeSessionRef.current = activeSessionId;
  }, [activeSessionId]);

  // The session scope artifacts were last loaded for. When the scope changes
  // (session switch/create/delete) the whole list swaps, which must NOT trigger
  // the fresh-artifact auto-follow/pulse (that's only for genuinely new rows
  // arriving within the same session).
  const artifactScopeRef = useRef<string | null | undefined>(undefined);

  const setRightTab = useCallback((tab: RightTab, opts?: { manual?: boolean }) => {
    if (opts?.manual) {
      // explicit user choice: stop auto-following unless they picked Artifacts
      setArtifactsFollow(tab === "artifacts");
    }
    setRightTabState(tab);
  }, []);

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
      setWorkflowRuns(await api.listWorkflowRuns());
    } catch {
      /* ignore */
    }
  }, []);
  const reloadArtifacts = useCallback(async () => {
    try {
      // Artifacts belong to the session: scope the fetch to the active session
      // (null → unscoped, which only happens in the boot gap before a session
      // is selected).
      const scope = activeSessionRef.current;
      const next = await api.listArtifacts("default", scope ?? undefined);
      const scopeChanged = artifactScopeRef.current !== scope;
      artifactScopeRef.current = scope;
      setArtifacts((prev) => {
        // §7.6 auto-follow: new artifact for the ACTIVE session → switch to
        // the Artifacts tab (if follow is on) and pulse-highlight the rows.
        // Skip on a scope change (session switch) — that swaps the whole list,
        // it isn't a fresh arrival.
        const prevIds = new Set(prev.map((a) => a.id));
        const fresh = next.filter((a) => !prevIds.has(a.id));
        const mine = fresh.filter(
          (a) => !a.session_id || a.session_id === activeSessionRef.current,
        );
        if (!scopeChanged && mine.length) {
          setArtifactsFollow((follow) => {
            if (follow) {
              setRightTabState("artifacts");
              setFlashArtifactIds(mine.map((a) => a.id));
              window.setTimeout(() => setFlashArtifactIds([]), 2500);
            }
            return follow;
          });
        }
        return next;
      });
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
    async (agent_id?: string, opts?: { title?: string }) => {
      if (creatingRef.current) return null;
      creatingRef.current = true;
      setSessionError(null);
      try {
        const s = await api.createSession({
          workspace: process.env.NEXT_PUBLIC_WORKSPACE ?? "/tmp/gw",
          agent_id,
          title: opts?.title,
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
    // optimistic remove; if it was active, fall to another session (or none)
    setSessions((prev) => {
      const next = prev.filter((s) => s.id !== id);
      setActiveSessionId((cur) => {
        if (cur !== id) return cur;
        return next[0]?.id ?? null;
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
    reloadArtifacts,
    removeArtifact,
    patchArtifact,
    newSession,
    setSessionAgent,
    removeSession,
    renameSession,
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
