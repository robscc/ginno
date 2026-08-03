"use client";

import { useRouter } from "next/navigation";
import { Check, Circle, Loader2, Square, Workflow } from "lucide-react";
import type { WorkflowRun } from "@/lib/types";

const STATUS_COLOR: Record<string, string> = {
  done: "#22c55e",
  ok: "#22c55e",
  running: "#3b82f6",
  paused: "#f59e0b",
  pending: "#71717a",
  failed: "#ef4444",
  cancelled: "#71717a",
};

function Glyph({ status }: { status?: string }) {
  const c = STATUS_COLOR[status || "pending"] || STATUS_COLOR.pending;
  if (status === "done" || status === "ok") return <Check className="h-3.5 w-3.5" style={{ color: c }} />;
  if (status === "running") return <Loader2 className="h-3.5 w-3.5 animate-spin" style={{ color: c }} />;
  return <Circle className="h-3.5 w-3.5" style={{ color: c }} />;
}

/**
 * In-chat live run block (design A): renders a workflow run bound to this session,
 * driven by run.event/run.status push events. Shows live step states + controls
 * (cancel while running, continue when paused) + a link to the workflow page.
 */
export function LiveRunBlock({
  run,
  onCancel,
  onContinue,
}: {
  run: WorkflowRun;
  onCancel?: (runId: string) => void;
  onContinue?: (runId: string) => void;
}) {
  const router = useRouter();
  const done = run.steps.filter((s) => s.status === "done").length;
  const total = run.steps.length;
  const c = STATUS_COLOR[run.status] || STATUS_COLOR.pending;
  return (
    <div className="my-2 rounded-lg border border-violet/40 bg-violet/[0.06] p-3">
      <div
        onClick={() => router.push("/workflows")}
        title="打开工作流详情"
        className="mb-2 flex cursor-pointer items-center gap-1.5 text-sm font-medium text-txt hover:text-violet"
      >
        <Workflow className="h-3.5 w-3.5 text-violet" />
        {run.name || "Workflow"}
        <span className="ml-auto flex items-center gap-1.5 text-xs font-normal" style={{ color: c }}>
          <span className="inline-block h-2 w-2 rounded-full" style={{ background: c }} />
          {run.status} · {done}/{total}
        </span>
      </div>
      <div className="space-y-1">
        {run.steps.map((s) => (
          <div key={s.id} className="flex items-center gap-2 text-xs">
            <Glyph status={s.status} />
            <span className={s.status === "done" ? "text-muted line-through" : "text-txt"}>{s.title}</span>
          </div>
        ))}
      </div>
      {(run.status === "running" || run.status === "paused") && (
        <div className="mt-2 flex gap-2">
          {run.status === "running" && onCancel && (
            <button
              onClick={() => onCancel(run.id)}
              className="flex items-center gap-1 rounded-md border border-red/40 px-2 py-1 text-xs text-red hover:bg-red/10"
            >
              <Square className="h-3 w-3" /> 取消
            </button>
          )}
          {run.status === "paused" && onContinue && (
            <button
              onClick={() => onContinue(run.id)}
              className="flex items-center gap-1 rounded-md border border-yellow/40 px-2 py-1 text-xs text-yellow hover:bg-yellow/10"
            >
              <Check className="h-3 w-3" /> 继续（human/supervisor）
            </button>
          )}
        </div>
      )}
    </div>
  );
}
