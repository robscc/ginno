"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "../runtime";
import type { SynthesisCaseSummary } from "../runtime";
import type { WorkflowDef, WorkflowRun } from "../types";
import type { RightTab } from "./useRightPanel";
import { notifyRunTransitions } from "./notifications";

/** Live in-flight tool call for a workflow run step (workflow-ux-redesign P1):
 *  shown under the running step in LiveRunBlock; cleared by tool_result /
 *  node_exit / terminal events. Ephemeral — never persisted. */
export interface RunToolActivity {
  nodeId: string;
  toolName: string;
  argsPreview: string;
}

export function useWorkflows({
  activateRightTab,
  rightPanelOpenRef,
  setRightPanelOpen,
}: {
  activateRightTab: (tab: RightTab) => void;
  rightPanelOpenRef: { current: boolean };
  setRightPanelOpen: (open: boolean) => void;
}) {
  // Stable callbacks/refs from the right-panel slice. Bundling them in a
  // hook-local ref keeps the dependency arrays below identical to the
  // pre-split store (the original captured them as same-scope constants).
  const panelHandlesRef = useRef({ activateRightTab, rightPanelOpenRef, setRightPanelOpen });

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
  // 中央区当前打开的 workflow run（侧栏运行伪条目）。窗口内临时状态：不持久
  // 化，随会话切换/清空一起清掉。
  const [activeRunId, setActiveRunId] = useState<string | null>(null);

  // ---- workflow run 视图 ----
  const openRunView = useCallback((runId: string) => setActiveRunId(runId), []);
  const closeRunView = useCallback(() => setActiveRunId(null), []);

  // ---- 定时任务回放（与 run 视图互斥：互相打开时关掉对方）----
  const [scheduleRun, setScheduleRun] = useState<import("../types").ScheduleRun | null>(null);
  const openScheduleRun = useCallback((run: import("../types").ScheduleRun) => {
    setScheduleRun(run);
    setActiveRunId(null);
  }, []);
  const closeScheduleRun = useCallback(() => setScheduleRun(null), []);

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
        panelHandlesRef.current.activateRightTab("workflow");
        if (!panelHandlesRef.current.rightPanelOpenRef.current) {
          panelHandlesRef.current.setRightPanelOpen(true);
        }
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

  return {
    workflows,
    workflowRuns,
    synthesisCases,
    liveToolActivity,
    notifyRunToolActivity,
    activeRunId,
    setActiveRunId,
    openRunView,
    closeRunView,
    scheduleRun,
    setScheduleRun,
    openScheduleRun,
    closeScheduleRun,
    markFailedRunsSeen,
    reloadWorkflows,
    reloadWorkflowRuns,
    reloadSynthesisCases,
    activeRunCount,
    unseenFailedCount,
    pendingHumanCount,
    synthesisActiveCount,
    runDurationByWorkflow,
  };
}
