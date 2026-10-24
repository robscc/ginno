"use client";

import { useCallback, useState } from "react";
import * as api from "../runtime";
import type { Goal, GoalStatus } from "../types";
import { uiText } from "../../i18n/uiText";

export function useGoals() {
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

  return {
    goalBySession,
    notifyGoal,
    loadGoal,
    setGoalObjective,
    setGoalStatus,
    clearGoal,
  };
}
