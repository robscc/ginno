"use client";

import { Workflow, Check, Loader2, Circle } from "lucide-react";
import { useGinno } from "@/lib/store";

const COLOR: Record<string, string> = {
  done: "#22c55e",
  running: "#3b82f6",
  pending: "#71717a",
  failed: "#ef4444",
};

function Glyph({ s }: { s: string }) {
  const c = COLOR[s] || COLOR.pending;
  if (s === "done") return <Check className="h-3 w-3" style={{ color: c }} />;
  if (s === "running") return <Loader2 className="h-3 w-3 animate-spin" style={{ color: c }} />;
  return <Circle className="h-3 w-3" style={{ color: c }} />;
}

export function WorkflowPanel() {
  const g = useGinno();
  const runs = g.workflowRuns;
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center px-4 pb-2 pt-4">
        <Workflow className="mr-2 h-4 w-4 text-muted" />
        <span className="text-sm font-semibold text-txt">Workflow Runs</span>
        <span className="ml-2 rounded-full bg-card2 px-2 py-0.5 text-[11px] text-muted">{runs.length}</span>
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto px-3">
        {runs.length === 0 && (
          <div className="px-1 py-6 text-center text-xs text-faint">
            No runs yet. Ask an agent to run a workflow (e.g. “run the PR Triage workflow”).
          </div>
        )}
        {runs.map((r) => {
          const done = r.steps.filter((s) => s.status === "done").length;
          return (
            <div key={r.id} className="rounded-xl border border-line bg-card p-3">
              <div className="flex items-center gap-1.5 text-sm font-medium text-txt">
                <Workflow className="h-3.5 w-3.5 text-violet" />
                {r.name}
                <span className="ml-auto text-[11px] font-normal text-faint">
                  {r.status} · {done}/{r.steps.length}
                </span>
              </div>
              <div className="mt-2 space-y-1">
                {r.steps.map((s) => (
                  <div key={s.id} className="flex items-center gap-2 text-xs">
                    <Glyph s={s.status} />
                    <span className={s.status === "done" ? "text-muted line-through" : "text-txt"}>
                      {s.title}
                    </span>
                  </div>
                ))}
              </div>
              <div className="mt-2 h-1 w-full overflow-hidden rounded-full bg-card2">
                <div
                  className="h-full bg-violet"
                  style={{ width: `${r.steps.length ? (done / r.steps.length) * 100 : 0}%` }}
                />
              </div>
            </div>
          );
        })}
      </div>
      {g.workflows.length > 0 && (
        <div className="border-t border-line px-4 py-2 text-[11px] text-faint">
          {g.workflows.length} definition(s): {g.workflows.map((w) => w.name).join(", ")}
        </div>
      )}
    </div>
  );
}
