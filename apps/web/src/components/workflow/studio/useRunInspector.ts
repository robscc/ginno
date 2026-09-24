"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as api from "@/lib/runtime";
import type { WorkflowRun, WorkflowRunEvent } from "@/lib/types";

export interface NodeStat {
  latencyMs?: number;
  tokens?: number;
}

/**
 * Live view of one run: status + event log + per-node telemetry.
 *
 * Polls every 1.5s while the run is active (the pre-existing behaviour, shared
 * with the in-chat run cards) and stops as soon as the run reaches a terminal
 * status. Phase 2 replaces this polling with the run-scoped WebSocket; the
 * returned shape is what the observer renders either way.
 */
export function useRunInspector(runId: string | null) {
  const [run, setRun] = useState<WorkflowRun | null>(null);
  const [events, setEvents] = useState<WorkflowRunEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    if (!runId) return;
    const [ev, one] = await Promise.all([
      api.getWorkflowRunEvents(runId).catch(() => null),
      api.getWorkflowRun(runId).catch(() => null),
    ]);
    if (!aliveRef.current) return;
    if (ev) setEvents(ev.events || []);
    if (one?.run) setRun(one.run);
  }, [runId]);

  useEffect(() => {
    if (!runId) {
      setRun(null);
      setEvents([]);
      return;
    }
    let alive = true;
    let timer: ReturnType<typeof setInterval> | undefined;
    setLoading(true);
    const tick = async () => {
      try {
        const [ev, one] = await Promise.all([
          api.getWorkflowRunEvents(runId),
          api.getWorkflowRun(runId),
        ]);
        if (!alive) return;
        setEvents(ev.events || []);
        const r = one.run ?? null;
        setRun(r);
        setLoading(false);
        if (r && r.status !== "running" && timer) {
          clearInterval(timer); // terminal → stop ticking
          timer = undefined;
        }
      } catch {
        if (alive) setLoading(false); // sidecar down: keep the last snapshot
      }
    };
    void tick();
    timer = setInterval(tick, 1500);
    return () => {
      alive = false;
      if (timer) clearInterval(timer);
    };
  }, [runId]);

  /** Status per node id, for colouring the canvas. */
  const nodeStatus = useMemo(() => {
    const out: Record<string, string> = {};
    for (const s of run?.steps || []) out[s.id] = s.status;
    return out;
  }, [run]);

  /** Per-node latency (node_enter→node_exit) and tokens (node_exit.usage). */
  const nodeStats = useMemo(() => {
    const enter: Record<string, number> = {};
    const stats: Record<string, NodeStat> = {};
    for (const e of events) {
      const nid = e.node_id;
      if (!nid) continue;
      if (e.kind === "node_enter" && typeof e.ts === "number") enter[nid] = e.ts;
      if (e.kind === "node_exit" && typeof e.ts === "number") {
        const s = (stats[nid] ??= {});
        if (enter[nid] !== undefined) s.latencyMs = Math.max(0, (e.ts - enter[nid]) * 1000);
        const u = e.usage;
        if (u) s.tokens = (s.tokens || 0) + (u.input_tokens || 0) + (u.output_tokens || 0);
      }
    }
    return stats;
  }, [events]);

  return { run, events, loading, nodeStatus, nodeStats, refresh };
}