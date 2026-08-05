"use client";

import { useState } from "react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import { WorkflowInspector } from "@/components/workflow/WorkflowInspector";

/** Workflow detail page (design §10 / P4): list on the left, live inspector
 *  (DAG + context + logs + steps) on the right. Mirrors the KB two-pane layout.
 *  Static-export friendly (single route + client selection state). */
export default function WorkflowsPage() {
  const g = useGinno();
  const [selId, setSelId] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const sel = g.workflows.find((w) => w.id === selId) || g.workflows[0] || null;

  // P6: distill the most recent session into a DSL draft, then create a workflow.
  const summarizeLastSession = async () => {
    const last = g.sessions[0];
    if (!last) {
      setMsg("没有可总结的会话");
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.summarizeSessionToDsl(last.id);
      if (!r.ok) {
        setMsg(`总结失败：${r.error}`);
        return;
      }
      const created = await api.createWorkflow({
        name: (r.dsl.name as string) || "从会话总结",
        description: (r.dsl.description as string) || "",
        dsl: r.dsl as never,
      });
      if (created.ok && created.workflow) {
        setSelId(created.workflow.id);
        g.reloadWorkflows();
        setMsg(`已从会话生成工作流 v1：${created.workflow.name}`);
      } else {
        setMsg("创建工作流失败");
      }
    } catch {
      setMsg("无法连接运行时");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid h-full grid-cols-1 gap-4 overflow-auto p-5 lg:grid-cols-[320px_minmax(0,1fr)]">
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold text-txt">工作流</h2>
          <button
            onClick={summarizeLastSession}
            disabled={busy || g.sessions.length === 0}
            title="把最近一次会话提炼成工作流 DSL 草稿并创建为 v1"
            className="ml-auto rounded-md border border-line2 px-2 py-1 text-[11px] text-muted transition-colors hover:text-txt disabled:opacity-50"
          >
            {busy ? "总结中…" : "从会话总结"}
          </button>
        </div>
        <p className="text-xs text-muted">版本化 DSL，由 LangGraph 图执行。选一个查看执行图 / 上下文 / 日志。</p>
        {msg && <div className="text-[11px] text-violet">{msg}</div>}
        <div className="space-y-1.5">
          {g.workflows.map((w) => (
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
          {g.workflows.length === 0 && <div className="text-xs text-faint">暂无工作流。</div>}
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
