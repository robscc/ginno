"use client";

import { useState } from "react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { WorkflowDef } from "@/lib/types";
import { WorkflowInspector } from "@/components/workflow/WorkflowInspector";

function DslPreview({ wf }: { wf: WorkflowDef }) {
  const [open, setOpen] = useState(false);
  if (!wf.dsl) return null;
  return (
    <div className="mt-1.5">
      <button
        onClick={() => setOpen((o) => !o)}
        className="text-[11px] text-faint transition-colors hover:text-muted"
      >
        {open ? "收起 DSL" : "查看 DSL"}
      </button>
      {open && (
        <pre className="mt-1 max-h-56 overflow-auto rounded-md border border-line bg-base/50 p-2 font-mono text-[11px] text-muted">
          {JSON.stringify(wf.dsl, null, 2)}
        </pre>
      )}
    </div>
  );
}

export function WorkflowsSettings() {
  const g = useGinno();
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [steps, setSteps] = useState('[{"title":"Step 1"}]');
  const [msg, setMsg] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);

  async function create() {
    let s: unknown;
    try {
      s = JSON.parse(steps);
    } catch {
      setMsg("steps JSON invalid");
      return;
    }
    const r = await api.createWorkflow({ name, description: desc, steps: s as never });
    setMsg(r.ok ? "created" : "error");
    if (r.ok) {
      setName("");
      setDesc("");
      setSteps('[{"title":"Step 1"}]');
      g.reloadWorkflows();
    }
  }
  async function del(id: string) {
    await api.deleteWorkflow(id);
    g.reloadWorkflows();
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">Workflows</h2>
      <p className="mt-1 text-sm text-muted">
        多步流程配方（版本化 DSL，由 LangGraph 图执行）。点「详情」查看执行图 / 上下文 / 日志并触发运行；完整的两栏视图见左导航 <span className="text-txt">Workflows</span> 页。
      </p>
      <div className="mt-4 space-y-2">
        {g.workflows.map((w) => (
          <div key={w.id} className="rounded-xl border border-line bg-card p-3">
            <div className="flex items-center gap-2">
              <span className="font-medium text-txt">{w.name}</span>
              <span className="text-xs text-faint">[{w.id}]</span>
              {w.version != null && (
                <span className="rounded border border-line2 px-1.5 py-0.5 text-[10px] text-faint">
                  v{w.version}
                </span>
              )}
              <div className="ml-auto flex items-center gap-2">
                <button
                  onClick={() => setOpenId((o) => (o === w.id ? null : w.id))}
                  className="text-xs text-faint transition-colors hover:text-txt"
                >
                  {openId === w.id ? "收起" : "详情"}
                </button>
                <button onClick={() => del(w.id)} className="text-xs text-faint hover:text-red">
                  delete
                </button>
              </div>
            </div>
            {w.description && <div className="mt-1 text-xs text-muted">{w.description}</div>}
            <ol className="mt-1 list-decimal pl-5 text-xs text-faint">
              {w.steps.map((s) => (
                <li key={s.id}>{s.title}</li>
              ))}
            </ol>
            <DslPreview wf={w} />
            {openId === w.id && <WorkflowInspector wf={w} runs={g.workflowRuns} />}
          </div>
        ))}
        {g.workflows.length === 0 && <div className="text-xs text-faint">No workflows.</div>}
      </div>
      <div className="mt-5 rounded-xl border border-line bg-card p-3">
        <div className="mb-2 text-sm font-medium text-txt">New workflow</div>
        <input className="field mb-2" placeholder="name" value={name} onChange={(e) => setName(e.target.value)} />
        <input className="field mb-2" placeholder="description" value={desc} onChange={(e) => setDesc(e.target.value)} />
        <textarea
          className="field mb-2 font-mono text-xs"
          rows={4}
          value={steps}
          onChange={(e) => setSteps(e.target.value)}
        />
        <div className="flex items-center gap-3">
          <button onClick={create} className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white">
            Create
          </button>
          {msg && <span className="text-xs text-muted">{msg}</span>}
        </div>
      </div>
    </div>
  );
}
