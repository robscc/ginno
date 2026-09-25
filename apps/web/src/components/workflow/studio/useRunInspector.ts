"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as api from "@/lib/runtime";
import type { WorkflowRun, WorkflowRunEvent } from "@/lib/types";

export interface NodeStat {
  latencyMs?: number;
  tokens?: number;
}

type WsFrame = {
  event?: string;
  [k: string]: unknown;
};

/**
 * Live view of one run (design B P2): the run-scoped WebSocket is the primary
 * transport — connect yields a full `run.snapshot` (run JSON + events.jsonl
 * replay), then `run.event` frames append by `seq` (dedup makes a reconnect's
 * fresh snapshot harmless). Falls back to 1.5s REST polling after two failed
 * connects, so the observer survives an old sidecar or a blocked WS.
 *
 * Returns the same shape the polling version did — RunObserver doesn't care
 * which transport is live.
 */
export function useRunInspector(runId: string | null) {
  const [run, setRun] = useState<WorkflowRun | null>(null);
  const [events, setEvents] = useState<WorkflowRunEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [live, setLive] = useState(false);
  const lastSeqRef = useRef(0);

  const applyEvents = useCallback((incoming: WorkflowRunEvent[]) => {
    const fresh = incoming.filter((e) => (e.seq ?? 0) > lastSeqRef.current);
    if (!fresh.length) return;
    for (const e of fresh) lastSeqRef.current = Math.max(lastSeqRef.current, e.seq ?? 0);
    setEvents((prev) => {
      // A snapshot may interleave with live frames — merge by seq, keep order.
      const seen = new Set(prev.map((e) => e.seq));
      return [...prev, ...fresh.filter((e) => !seen.has(e.seq))].sort(
        (a, b) => (a.seq ?? 0) - (b.seq ?? 0),
      );
    });
  }, []);

  const applyRun = useCallback((r: WorkflowRun | null | undefined) => {
    if (r) setRun(r);
  }, []);

  useEffect(() => {
    if (!runId) {
      setRun(null);
      setEvents([]);
      setLive(false);
      lastSeqRef.current = 0;
      return;
    }
    let alive = true;
    let ws: WebSocket | null = null;
    let reconnect: ReturnType<typeof setTimeout> | null = null;
    let poll: ReturnType<typeof setInterval> | null = null;
    let fails = 0;
    setLoading(true);

    const stopPolling = () => {
      if (poll) clearInterval(poll);
      poll = null;
    };
    const startPolling = () => {
      if (poll) return;
      const tick = async () => {
        try {
          const [ev, one] = await Promise.all([
            api.getWorkflowRunEvents(runId),
            api.getWorkflowRun(runId),
          ]);
          if (!alive) return;
          applyEvents(ev.events || []);
          applyRun(one.run);
          setLoading(false);
          if (one.run && one.run.status !== "running" && one.run.status !== "paused") {
            stopPolling(); // terminal → stop ticking (last snapshot stays)
          }
        } catch {
          if (alive) setLoading(false); // sidecar down: keep the last snapshot
        }
      };
      void tick();
      poll = setInterval(tick, 1500);
    };

    const handle = (f: WsFrame) => {
      switch (f.event) {
        case "run.snapshot": {
          if (f.run) setRun(f.run as WorkflowRun);
          setEvents([]);
          lastSeqRef.current = 0;
          applyEvents((f.events as WorkflowRunEvent[]) || []);
          setLoading(false);
          break;
        }
        case "run.event":
          applyEvents([f.payload as WorkflowRunEvent]);
          break;
        case "run.steps":
          // mid-run step-status refresh (no status transitions happen while a
          // step executes — without this the table sticks at the connect-time
          // snapshot while events flow around it)
          setRun((prev) => (prev ? { ...prev, steps: f.steps as WorkflowRun["steps"] } : prev));
          break;
        case "run.status":
          // the full run JSON follows as run.snapshot; patch the essentials now
          setRun((prev) =>
            prev
              ? {
                  ...prev,
                  status: (f.status as string) ?? prev.status,
                  error: (f.error as string | undefined) ?? prev.error,
                  pending_interrupt: (f.pending_interrupt as WorkflowRun["pending_interrupt"]) ?? null,
                }
              : prev,
          );
          break;
        default:
          break; // run.pong / run.missing — nothing to render
      }
    };

    const connect = () => {
      if (!alive) return;
      try {
        ws = api.openRunSocket(runId);
      } catch {
        fails += 1;
        if (fails >= 2) startPolling();
        else reconnect = setTimeout(connect, 3000);
        return;
      }
      ws.onopen = () => {
        if (!alive) return;
        fails = 0;
        setLive(true);
        stopPolling(); // WS healthy again → polling standby
      };
      ws.onmessage = (m) => {
        if (!alive) return;
        try {
          handle(JSON.parse(m.data as string) as WsFrame);
        } catch {
          /* malformed frame — ignore */
        }
      };
      ws.onclose = () => {
        if (!alive) return;
        setLive(false);
        fails += 1;
        if (fails >= 2) startPolling();
        else reconnect = setTimeout(connect, 3000);
      };
    };
    connect();

    return () => {
      alive = false;
      stopPolling();
      if (reconnect) clearTimeout(reconnect);
      if (ws) {
        ws.onclose = null;
        ws.close();
      }
    };
  }, [runId, applyEvents, applyRun]);

  const refresh = useCallback(async () => {
    if (!runId) return;
    try {
      const [ev, one] = await Promise.all([
        api.getWorkflowRunEvents(runId),
        api.getWorkflowRun(runId),
      ]);
      applyEvents(ev.events || []);
      applyRun(one.run);
    } catch {
      /* keep the last snapshot */
    }
  }, [runId, applyEvents, applyRun]);

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

  return { run, events, loading, live, nodeStatus, nodeStats, refresh };
}