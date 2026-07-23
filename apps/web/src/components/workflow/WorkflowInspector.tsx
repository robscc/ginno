"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { WorkflowDef, WorkflowRun } from "@/lib/types";
import { WorkflowDag } from "./WorkflowDag";
import { WorkflowLogTimeline } from "./WorkflowLogTimeline";
import { ContextEditor } from "./ContextEditor";

const STEP_COLOR: Record<string, string> = {
  done: "#22c55e",
  running: "#3b82f6",
  failed: "#ef4444",
};

/** Full workflow inspector: editable context, run trigger, live DAG (click a node
 *  to filter the log), step list, and event timeline. Shared by the /workflows
 *  page (P4) and the Settings detail panel. Polls while a run is active. */
export function WorkflowInspector({ wf, runs }: { wf: WorkflowDef; runs: WorkflowRun[] }) {
  const router = useRouter();
  const g = useGinno();
  const [run, setRun] = useState<WorkflowRun | null>(null);
  const [events, setEvents] = useState<Array<Record<string, unknown>>>([]);
  const [busy, setBusy] = useState(false);
  const [ctxOverride, setCtxOverride] = useState<Record<string, unknown>>({});
  const [selNode, setSelNode] = useState<string | null>(null);

  const latest = runs.find((r) => r.workflow_id === wf.id) || null;
  const activeId = run?.id || latest?.id || null;

  useEffect(() => {
    if (!activeId) {
      setEvents([]);
      return;
    }
    let alive = true;
    const load = async () => {
      try {
        const [ev, allRuns] = await Promise.all([
          api.getWorkflowRunEvents(activeId),
          api.listWorkflowRuns(),
        ]);
        if (!alive) return;
        setEvents(ev.events || []);
        const cur = allRuns.find((r) => r.id === activeId);
        if (cur) {
          setRun(cur);
          if (cur.status !== "running") setBusy(false);
        }
      } catch {
        /* sidecar down */
      }
    };
    void load();
    const t = setInterval(load, 1500);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [activeId]);

  const trigger = async () => {
    setBusy(true);
    setRun(null);
    setEvents([]);
    try {
      const r = await api.triggerWorkflowRun(
        wf.id,
        Object.keys(ctxOverride).length ? ctxOverride : undefined,
      );
      if (r.ok && r.run) setRun(r.run);
    } finally {
      /* busy cleared by poll when status != running */
    }
  };

  const openDevSession = async () => {
    // Open a session bound to the workflow-dev agent; the user then asks for the
    // edit in chat. The agent proposes via workflow_propose_edit → diff card.
    const s = await g.newSession("workflow-dev");
    router.push("/");
    return s;
  };

  const nodeStatus: Record<string, string> = {};
  for (const s of run?.steps || []) nodeStatus[s.id] = s.status;

  const filteredEvents = selNode ? events.filter((e) => e.node_id === selNode) : events;

  return (
    <div className="space-y-3">
      <ContextEditor dsl={wf.dsl as never} onChange={setCtxOverride} />

      <div className="flex items-center gap-2">
        <span className="text-sm font-medium text-txt">执行图</span>
        {selNode && (
          <button
            onClick={() => setSelNode(null)}
            className="rounded border border-line2 px-1.5 py-0.5 text-[10px] text-faint hover:text-muted"
          >
            清除节点过滤 [{selNode}]
          </button>
        )}
        <button
          onClick={openDevSession}
          title="打开 Workflow 开发会话，用对话修改 DSL（带 diff 确认）"
          className="rounded-md border border-line2 px-2.5 py-1 text-xs text-muted hover:text-txt"
        >
          开发会话
        </button>
        <button
          onClick={trigger}
          disabled={busy}
          className="ml-auto rounded-md bg-violet px-3 py-1 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
        >
          {busy ? "运行中…" : "运行"}
        </button>
      </div>
      <WorkflowDag
        dsl={wf.dsl as never}
        status={nodeStatus}
        selected={selNode}
        onSelect={setSelNode}
      />

      {run && (
        <div className="text-[11px] text-faint">
          run <span className="font-mono text-muted">{run.id.slice(0, 8)}</span> · v
          {run.dsl_version ?? "?"} ·{" "}
          <span style={{ color: STEP_COLOR[run.status] || "rgb(var(--muted))" }}>{run.status}</span>
        </div>
      )}

      {run && (
        <div className="space-y-1">
          <div className="text-xs font-medium text-txt">步骤清单</div>
          <div className="space-y-0.5">
            {run.steps.map((s) => (
              <button
                key={s.id}
                onClick={() => setSelNode((n) => (n === s.id ? null : s.id))}
                className={`flex w-full items-center gap-2 rounded px-1 py-0.5 text-left text-[11px] transition-colors hover:bg-card2/50 ${
                  selNode === s.id ? "bg-card2/60" : ""
                }`}
              >
                <span
                  className="h-1.5 w-1.5 shrink-0 rounded-full"
                  style={{ background: STEP_COLOR[s.status] || "rgb(var(--faint))" }}
                />
                <span style={{ color: STEP_COLOR[s.status] || "rgb(var(--muted))" }}>{s.status}</span>
                <span className="text-txt">{s.title || s.id}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="space-y-1">
        <div className="text-xs font-medium text-txt">
          执行日志{selNode ? ` · 节点 ${selNode}` : ""}
        </div>
        <WorkflowLogTimeline events={filteredEvents} />
      </div>

      <div className="space-y-1 rounded-lg border border-line bg-base/30 p-2.5">
        <div className="text-xs font-medium text-txt">Supervisor</div>
        {(() => {
          const sup = (wf.dsl as { supervisor?: { enabled?: boolean; mode?: string } } | undefined)
            ?.supervisor;
          const on = !!sup?.enabled;
          return (
            <div className="text-[11px] text-muted">
              状态：
              <span className={on ? "text-violet" : "text-faint"}>
                {on ? `已启用 · 模式 ${sup?.mode ?? "human"}` : "未启用"}
              </span>
              <p className="mt-1 text-faint">
                监控节点（每步后观察并决定 继续/重试/跳过/改上下文/暂停）的 auto 策略与人工决策 UX
                待深入讨论，本期仅占位，尚未接入图执行。
              </p>
            </div>
          );
        })()}
      </div>
    </div>
  );
}
