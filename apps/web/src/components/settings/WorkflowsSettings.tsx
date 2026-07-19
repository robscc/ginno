"use client";

import { useState } from "react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";

export function WorkflowsSettings() {
  const g = useGinno();
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [steps, setSteps] = useState('[{"title":"Step 1"}]');
  const [msg, setMsg] = useState("");

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
      <p className="mt-1 text-sm text-muted">多步流程配方；运行时右栏 Workflow 标签显示进度树。</p>
      <div className="mt-4 space-y-2">
        {g.workflows.map((w) => (
          <div key={w.id} className="rounded-xl border border-line bg-card p-3">
            <div className="flex items-center gap-2">
              <span className="font-medium text-txt">{w.name}</span>
              <span className="text-xs text-faint">[{w.id}]</span>
              <button onClick={() => del(w.id)} className="ml-auto text-xs text-faint hover:text-red">
                delete
              </button>
            </div>
            {w.description && <div className="mt-1 text-xs text-muted">{w.description}</div>}
            <ol className="mt-1 list-decimal pl-5 text-xs text-faint">
              {w.steps.map((s) => (
                <li key={s.id}>{s.title}</li>
              ))}
            </ol>
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
