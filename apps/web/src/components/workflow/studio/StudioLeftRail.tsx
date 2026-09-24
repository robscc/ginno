"use client";

import { useMemo, useState } from "react";
import { Loader2, Search } from "lucide-react";
import type { WorkflowDef, WorkflowRun } from "@/lib/types";
import { STATUS_LABEL } from "@/components/chat/RunBlocks";

const RUN_COLOR: Record<string, string> = {
  running: "#3b82f6",
  paused: "#f59e0b",
  done: "#22c55e",
  failed: "#ef4444",
  cancelled: "#71717a",
  interrupted: "#f97316",
};

function pillClass(status: string): string {
  if (status === "running") return "bg-blue/15 text-blue";
  if (status === "paused") return "bg-orange/15 text-orange";
  if (status === "done") return "bg-green/15 text-green";
  if (status === "failed") return "bg-red/15 text-red";
  return "bg-card2 text-faint";
}

function timeLabel(ts?: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** Left rail: the recipe list on top, the runs of the selected recipe below
 *  (design B §3 — the rail is "配方 + Runs", not "会话"). */
export function StudioLeftRail({
  workflows,
  runs,
  runsLoading,
  selWfId,
  selRunId,
  onSelectWorkflow,
  onSelectRun,
}: {
  workflows: WorkflowDef[];
  runs: WorkflowRun[];
  runsLoading?: boolean;
  selWfId: string | null;
  selRunId: string | null;
  onSelectWorkflow: (id: string) => void;
  onSelectRun: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState<"all" | "system" | "user">("all");

  const visible = useMemo(
    () =>
      workflows.filter((w) => {
        if (scope === "system" && !w.system) return false;
        if (scope === "user" && w.system) return false;
        const q = query.trim().toLowerCase();
        if (!q) return true;
        return w.name.toLowerCase().includes(q) || (w.description || "").toLowerCase().includes(q);
      }),
    [workflows, query, scope],
  );

  const activeRuns = runs.filter((r) => r.status === "running" || r.status === "paused").length;

  return (
    <div className="flex h-full w-full flex-col border-r border-line bg-panel">
      <div className="space-y-1.5 border-b border-line px-3 py-2.5">
        <div className="flex items-center gap-1.5">
          <span className="text-[13px] font-semibold text-txt">工作室</span>
          <span className="ml-auto font-mono text-[10px] text-faint">
            {workflows.length} 配方
          </span>
        </div>
        <div className="relative">
          <Search className="pointer-events-none absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-faint" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索配方…"
            className="w-full rounded border border-line bg-base py-1 pl-7 pr-2 text-xs text-txt placeholder:text-faint focus:border-violet/60 focus:outline-none"
          />
        </div>
        <div className="flex gap-2.5 text-[11px]">
          {(
            [
              ["all", "全部"],
              ["system", "系统"],
              ["user", "用户"],
            ] as const
          ).map(([k, label]) => (
            <button
              key={k}
              onClick={() => setScope(k)}
              className={
                scope === k
                  ? "text-violet"
                  : "text-faint transition-colors hover:text-muted"
              }
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="min-h-[120px] flex-1 overflow-y-auto">
        {visible.map((w) => {
          const on = w.id === selWfId;
          return (
            <button
              key={w.id}
              onClick={() => onSelectWorkflow(w.id)}
              className={`block w-full border-l-2 px-3 py-1.5 text-left transition-colors ${
                on ? "border-violet bg-card2/70" : "border-transparent hover:bg-card/60"
              }`}
            >
              <div className="flex items-center gap-1.5">
                <span className={`truncate text-[12.5px] ${on ? "font-semibold text-txt" : "text-txt"}`}>
                  {w.name}
                </span>
                {w.system && (
                  <span
                    className="shrink-0 rounded px-1 py-px text-[9.5px]"
                    style={{ color: "#8b5cf6", background: "#8b5cf61a" }}
                    title="内置配方：随应用提供，不可删除"
                  >
                    内置
                  </span>
                )}
                <span className="ml-auto shrink-0 font-mono text-[10px] text-faint">v{w.version ?? 1}</span>
              </div>
              {w.description && (
                <div className="mt-0.5 truncate text-[10.5px] text-muted">{w.description}</div>
              )}
            </button>
          );
        })}
        {!visible.length && (
          <div className="px-3 py-4 text-[11px] text-faint">
            {workflows.length ? "没有匹配的配方" : "暂无配方。在聊天页用「总结成流程」创建，或在 设置 → 工作流 里写 DSL。"}
          </div>
        )}
      </div>

      <div className="flex max-h-[45%] min-h-[110px] flex-col border-t border-line">
        <div className="flex items-center gap-1.5 px-3 py-2">
          <span className="text-[11px] font-semibold text-txt">运行</span>
          {activeRuns > 0 && (
            <span className="rounded-full bg-blue/15 px-1.5 text-[10px] text-blue">{activeRuns} 进行中</span>
          )}
          {runsLoading && <Loader2 className="h-3 w-3 animate-spin text-faint" />}
          <span className="ml-auto font-mono text-[10px] text-faint">{runs.length}</span>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto pb-1">
          {runs.map((r) => {
            const on = r.id === selRunId;
            const done = r.steps.filter((s) => s.status === "done").length;
            return (
              <button
                key={r.id}
                onClick={() => onSelectRun(r.id)}
                className={`block w-full border-l-2 px-3 py-1.5 text-left transition-colors ${
                  on ? "border-blue bg-card2/70" : "border-transparent hover:bg-card/60"
                }`}
              >
                <div className="flex items-center gap-1.5">
                  <span className={`rounded px-1.5 py-px text-[9.5px] ${pillClass(r.status)}`}>
                    {STATUS_LABEL[r.status] || r.status}
                  </span>
                  <span className="font-mono text-[10.5px] text-muted">#{r.id.slice(0, 6)}</span>
                  <span className="ml-auto font-mono text-[10px] text-faint">{timeLabel(r.started)}</span>
                </div>
                <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-faint">
                  <span className="tabular-nums">
                    {done}/{r.steps.length} 步
                  </span>
                  <span>· v{r.dsl_version ?? "?"}</span>
                  {r.status === "paused" && r.pending_interrupt?.node_id && (
                    <span className="text-orange">@{r.pending_interrupt.node_id}</span>
                  )}
                  {r.error_detail?.node_id && r.status === "failed" && (
                    <span className="text-red">@{r.error_detail.node_id}</span>
                  )}
                </div>
              </button>
            );
          })}
          {!runs.length && (
            <div className="px-3 py-3 text-[11px] text-faint">
              {selWfId ? "该配方还没有运行记录" : "选择一个配方"}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}