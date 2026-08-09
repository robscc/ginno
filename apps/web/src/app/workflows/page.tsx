"use client";

import { useState } from "react";
import { Search } from "lucide-react";
import { useGinno } from "@/lib/store";
import { WorkflowInspector } from "@/components/workflow/WorkflowInspector";

/** Workflow detail page (design §10 / P4): list on the left, live inspector
 *  (DAG + context + logs + steps) on the right. Mirrors the KB two-pane layout.
 *  Static-export friendly (single route + client selection state). */
export default function WorkflowsPage() {
  const g = useGinno();
  const [selId, setSelId] = useState<string | null>(null);
  // P3: client-side search + 全部/系统/用户 filter tabs.
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState<"all" | "system" | "user">("all");
  const visible = g.workflows.filter((w) => {
    if (scope === "system" && !w.system) return false;
    if (scope === "user" && w.system) return false;
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return (
      w.name.toLowerCase().includes(q) || (w.description || "").toLowerCase().includes(q)
    );
  });
  const sel = visible.find((w) => w.id === selId) || visible[0] || null;

  // 「总结成流程」moved to the chat composer (workflow-ux-redesign S1): the
  // entry lives where the conversation happens, with session picker + draft
  // modal. This page stays focused on inspecting definitions/runs.

  return (
    <div className="grid h-full grid-cols-1 gap-4 overflow-auto p-5 lg:grid-cols-[320px_minmax(0,1fr)]">
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold text-txt">工作流</h2>
        </div>
        <p className="text-xs text-muted">版本化 DSL，由 LangGraph 图执行。选一个查看执行图 / 上下文 / 日志。在聊天页可用「总结成流程」从会话创建工作流。</p>

        {/* P3: search + scope tabs */}
        <div className="relative">
          <Search className="pointer-events-none absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-faint" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索 workflow 名称…"
            className="w-full rounded border border-line bg-base py-1 pl-7 pr-2 text-xs text-txt placeholder:text-faint focus:border-violet/60 focus:outline-none"
          />
        </div>
        <div className="flex gap-3 border-b border-line2 text-[11px]">
          {([
            ["all", "全部"],
            ["system", "系统"],
            ["user", "用户"],
          ] as const).map(([k, label]) => (
            <button
              key={k}
              onClick={() => setScope(k)}
              className={`-mb-px border-b pb-1 transition-colors ${
                scope === k ? "border-violet text-violet" : "border-transparent text-faint hover:text-muted"
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        <div className="space-y-1.5">
          {visible.map((w) => (
            <button
              key={w.id}
              onClick={() => setSelId(w.id)}
              className={`block w-full rounded-lg border px-3 py-2 text-left transition-colors ${
                sel?.id === w.id
                  ? "border-violet/50 bg-violet/10"
                  : "border-line bg-card hover:border-line2"
              }`}
            >
              <div className="flex items-center gap-2">
                <span className="truncate text-sm font-medium text-txt">{w.name}</span>
                {w.system && (
                  <span
                    className="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium"
                    style={{ color: "#8b5cf6", background: "#8b5cf61a" }}
                    title="内置 workflow：随应用提供，不可删除"
                  >
                    内置
                  </span>
                )}
                {w.version != null && (
                  <span className="rounded border border-line2 px-1 text-[10px] text-faint">
                    v{w.version}
                  </span>
                )}
              </div>
              {w.description && <div className="mt-0.5 truncate text-[11px] text-muted">{w.description}</div>}
              <div className="mt-0.5 text-[10px] text-faint">{w.steps.length} 步</div>
            </button>
          ))}
          {visible.length === 0 && (
            <div className="text-xs text-faint">{g.workflows.length === 0 ? "暂无工作流。" : "没有匹配的工作流"}</div>
          )}
        </div>
      </div>

      <div className="min-w-0">
        {sel ? (
          <div className="rounded-xl border border-line bg-card p-4">
            <div className="mb-3 flex items-center gap-2">
              <span className="text-base font-semibold text-txt">{sel.name}</span>
              <span className="text-xs text-faint">[{sel.id}]</span>
            </div>
            <WorkflowInspector wf={sel} runs={g.workflowRuns} />
          </div>
        ) : (
          <div className="rounded-xl border border-dashed border-line p-10 text-center text-sm text-faint">
            选择一个工作流查看详情。新建/编辑配方在 设置 → Workflows。
          </div>
        )}
      </div>
    </div>
  );
}
