"use client";

import { useEffect } from "react";
import { Workflow } from "lucide-react";
import { useGinno } from "@/lib/store";
import { LiveRunBlock } from "@/components/chat/RunBlocks";
import { cancelWorkflowRun, decideWorkflowRun } from "@/lib/runtime";

/**
 * Right-panel Workflow tab (design A): renders live run blocks (same component as
 * the in-chat blocks) with cancel/continue controls + a jump to the workflow page.
 * Polls while any run is active as a fallback to the run.* WS push.
 */
export function WorkflowPanel() {
  const g = useGinno();
  const runs = g.workflowRuns;
  const active = runs.some((r) => r.status === "running" || r.status === "paused");

  // Live refresh while a run is in flight (WS push handles the immediate cases).
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => g.reloadWorkflowRuns(), 1500);
    return () => clearInterval(t);
  }, [active, g]);

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center px-4 pb-2 pt-4">
        <Workflow className="mr-2 h-4 w-4 text-muted" />
        <span className="text-sm font-semibold text-txt">Workflow Runs</span>
        <span className="ml-2 rounded-full bg-card2 px-2 py-0.5 text-[11px] text-muted">{runs.length}</span>
        {active && (
          <span className="ml-auto flex items-center gap-1.5 text-[11px] text-blue">
            <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-blue" /> 运行中
          </span>
        )}
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto px-3">
        {runs.length === 0 && (
          <div className="px-1 py-6 text-center text-xs text-faint">
            No runs yet. 在聊天里点「⌁ 总结成流程 → 创建并运行」，或让 agent 运行一个 workflow。
          </div>
        )}
        {runs.map((r) => (
          <LiveRunBlock
            key={r.id}
            run={r}
            onCancel={(id) => cancelWorkflowRun(id)}
            onContinue={(id) => decideWorkflowRun(id, "continue")}
          />
        ))}
      </div>
      {g.workflows.length > 0 && (
        <div className="border-t border-line px-4 py-2 text-[11px] text-faint">
          {g.workflows.length} definition(s): {g.workflows.map((w) => w.name).join(", ")}
        </div>
      )}
    </div>
  );
}
