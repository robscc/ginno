"use client";

import { useCallback, useState } from "react";
import * as api from "../runtime";
import type { Todo } from "../types";
import { uiText } from "../../i18n/uiText";

export function useTodos() {
  const [todos, setTodos] = useState<Todo[]>([]);

  const reloadTodos = useCallback(async () => {
    try {
      setTodos(await api.listTodos());
    } catch {
      /* ignore */
    }
  }, []);

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

  return { todos, reloadTodos, patchTodo, addTodo, removeTodo };
}
