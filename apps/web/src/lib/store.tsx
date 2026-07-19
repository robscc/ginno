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
import type { AgentConfig, Providers, SessionMeta, Todo } from "./types";

interface GinnoState {
  agents: AgentConfig[];
  sessions: SessionMeta[];
  todos: Todo[];
  providers: Providers;
  defaultProvider: string;
  activeSessionId: string | null;
  connected: boolean;
  ready: boolean;
  setConnected: (v: boolean) => void;
  setActiveSession: (id: string | null) => void;
  reloadAgents: () => Promise<void>;
  reloadSessions: () => Promise<void>;
  reloadTodos: () => Promise<void>;
  reloadProviders: () => Promise<void>;
  newSession: (agent_id?: string) => Promise<SessionMeta | null>;
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
  const [providers, setProviders] = useState<Providers>({});
  const [defaultProvider, setDefaultProvider] = useState("custom");
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [ready, setReady] = useState(false);

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
      await Promise.all([reloadAgents(), reloadSessions(), reloadTodos(), reloadProviders()]);
      if (alive) setReady(true);
    })();
    return () => {
      alive = false;
    };
  }, [reloadAgents, reloadSessions, reloadTodos, reloadProviders]);

  const creatingRef = useRef(false);
  const newSession = useCallback(
    async (agent_id?: string) => {
      if (creatingRef.current) return null;
      creatingRef.current = true;
      try {
        const s = await api.createSession({
          workspace: process.env.NEXT_PUBLIC_WORKSPACE ?? "/tmp/gw",
          agent_id,
        });
        if (s && s.ok !== false && s.id) {
          setSessions((prev) => [s, ...prev.filter((x) => x.id !== s.id)]);
          setActiveSessionId(s.id);
          return s;
        }
      } catch {
        /* ignore */
      } finally {
        creatingRef.current = false;
      }
      return null;
    },
    [],
  );

  const patchTodo = useCallback(async (id: string, patch: Partial<Todo>) => {
    setTodos((prev) =>
      prev.map((t) =>
        t.id === id
          ? {
              ...t,
              ...patch,
              completed_at: patch.done ? Date.now() / 1000 : patch.done === false ? null : t.completed_at,
            }
          : t,
      ),
    );
    try {
      await api.updateTodo(id, patch);
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

  const value: GinnoState = {
    agents,
    sessions,
    todos,
    providers,
    defaultProvider,
    activeSessionId,
    connected,
    ready,
    setConnected,
    setActiveSession: setActiveSessionId,
    reloadAgents,
    reloadSessions,
    reloadTodos,
    reloadProviders,
    newSession,
    patchTodo,
    addTodo,
  };

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
