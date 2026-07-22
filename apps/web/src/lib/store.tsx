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
import type { AgentConfig, Artifact, Providers, SessionMeta, Todo, WorkflowDef, WorkflowRun } from "./types";

interface GinnoState {
  agents: AgentConfig[];
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
  setConnected: (v: boolean) => void;
  setActiveSession: (id: string | null) => void;
  reloadAgents: () => Promise<void>;
  reloadSessions: () => Promise<void>;
  reloadTodos: () => Promise<void>;
  reloadProviders: () => Promise<void>;
  reloadWorkflows: () => Promise<void>;
  reloadWorkflowRuns: () => Promise<void>;
  reloadArtifacts: () => Promise<void>;
  newSession: (agent_id?: string) => Promise<SessionMeta | null>;
  setSessionAgent: (id: string, agentId: string) => void;
  patchTodo: (id: string, patch: Partial<Todo>) => Promise<void>;
  addTodo: (data: Partial<Todo>) => Promise<void>;
}

const Ctx = createContext<GinnoState | null>(null);

export function useGinno(): GinnoState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useGinno must be used within GinnoProvider");
  return v;
}

export function GinnoProvider({ children }: { children: ReactNode }) {
  const [agents, setAgents] = useState<AgentConfig[]>([]);
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

  const reloadAgents = useCallback(async () => {
    try {
      setAgents(await api.listAgents());
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
      setArtifacts(await api.listArtifacts());
    } catch {
      /* ignore */
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
    reloadSessions,
    reloadTodos,
    reloadProviders,
    reloadWorkflows,
    reloadWorkflowRuns,
    reloadArtifacts,
  ]);

  const creatingRef = useRef(false);
  const newSession = useCallback(
    async (agent_id?: string) => {
      if (creatingRef.current) return null;
      creatingRef.current = true;
      setSessionError(null);
      try {
        const s = await api.createSession({
          workspace: process.env.NEXT_PUBLIC_WORKSPACE ?? "/tmp/gw",
          agent_id,
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

  const addTodo = useCallback(async (data: Partial<Todo>) => {
    try {
      const r = await api.createTodo(data);
      if (r.ok && r.todo) setTodos((prev) => [...prev, r.todo!]);
    } catch {
      /* ignore */
    }
  }, []);

  const value: GinnoState = {
    agents,
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
    setConnected,
    setActiveSession: setActiveSessionId,
    reloadAgents,
    reloadSessions,
    reloadTodos,
    reloadProviders,
    reloadWorkflows,
    reloadWorkflowRuns,
    reloadArtifacts,
    newSession,
    setSessionAgent,
    patchTodo,
    addTodo,
  };

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
