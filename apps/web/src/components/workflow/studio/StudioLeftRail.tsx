"use client";

import { useMemo, useState } from "react";
import { Search } from "lucide-react";
import type { WorkflowDef, WorkflowRun } from "@/lib/types";

/** Left rail: the recipe list (full height since 方案B 打磨 moved the runs into
 *  the 运行 tab's own column) plus the decision inbox — paused runs across ALL
 *  recipes awaiting a human decision, which is cross-recipe and stays here. */
export function StudioLeftRail({
  workflows,
  inbox,
  selWfId,
  selRunId,
  onSelectWorkflow,
  onOpenDecision,
}: {
  workflows: WorkflowDef[];
  inbox: WorkflowRun[];
  selWfId: string | null;
  selRunId: string | null;
  onSelectWorkflow: (id: string) => void;
  onOpenDecision: (wfId: string, runId: string) => void;
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

      {inbox.length > 0 && (
        <div className="border-t border-line py-1.5">
          <div className="flex items-center gap-1.5 px-3 pb-1">
            <span className="text-[11px] font-semibold text-txt">决策收件箱</span>
            <span className="rounded-full bg-orange/15 px-1.5 text-[10px] text-orange">
              {inbox.length} 待裁决
            </span>
          </div>
          {inbox.map((r) => {
            const kind = r.pending_interrupt?.kind === "supervisor" ? "supervisor" : "human";
            const wfName = workflows.find((w) => w.id === r.workflow_id)?.name || r.workflow_id;
            return (
              <button
                key={r.id}
                onClick={() => onOpenDecision(r.workflow_id, r.id)}
                className={`block w-full border-l-2 px-3 py-1.5 text-left transition-colors ${
                  r.id === selRunId
                    ? "border-orange bg-card2/70"
                    : "border-transparent hover:bg-card/60"
                }`}
              >
                <div className="flex items-center gap-1.5">
                  <span
                    className={`rounded px-1 py-px text-[9.5px] ${
                      kind === "supervisor" ? "bg-violet/15 text-violet" : "bg-orange/15 text-orange"
                    }`}
                  >
                    {kind}
                  </span>
                  <span className="truncate text-[11.5px] text-txt">{wfName}</span>
                  <span className="ml-auto font-mono text-[10px] text-faint">
                    #{r.id.slice(0, 6)}
                  </span>
                </div>
                {r.pending_interrupt?.node_id && (
                  <div className="mt-0.5 truncate text-[10px] text-faint">
                    @{r.pending_interrupt.node_id}
                    {r.pending_interrupt.question ? ` · ${r.pending_interrupt.question}` : ""}
                  </div>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}