"use client";

import { useCallback, useState } from "react";
import * as api from "../runtime";
import type { Plan } from "../types";

export function usePlans() {
  // ---- 任务清单编排（§2/P3）：同 goal 的 (a) 打开会话拉取 + (b) WS 实时刷 ----
  const [plansBySession, setPlansBySession] = useState<Record<string, Plan[]>>({});

  const notifyPlanUpdated = useCallback((plan: Plan) => {
    if (!plan?.plan_id || !plan.session_id) return;
    setPlansBySession((prev) => {
      const cur = prev[plan.session_id] ?? [];
      const i = cur.findIndex((p) => p.plan_id === plan.plan_id);
      const next =
        i >= 0 ? cur.map((p, j) => (j === i ? plan : p)) : [...cur, plan];
      return { ...prev, [plan.session_id]: next };
    });
  }, []);

  const loadPlans = useCallback(async (sessionId: string) => {
    try {
      const r = await api.listSessionPlans(sessionId);
      setPlansBySession((prev) => ({ ...prev, [sessionId]: r ?? [] }));
    } catch {
      /* sidecar down */
    }
  }, []);

  return { plansBySession, loadPlans, notifyPlanUpdated };
}
