"use client";

import { useMemo, useState } from "react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { WorkflowDef } from "@/lib/types";
import { WorkflowInspector } from "@/components/workflow/WorkflowInspector";

const DSL_TEMPLATE = `{
  "name": "新流程",
  "description": "",
  "entry": "s1",
  "context": { "schema": { "type": "object", "properties": {} }, "initial": {} },
  "nodes": [
    { "id": "s1", "type": "step", "agent": "dev", "goal": "在这里写这一步要做什么" }
  ],
  "edges": []
}`;

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
  // P3 #15: DSL v1 JSON editor (replaces the legacy steps-array field) with
  // live parse/structure hints; the server runs the full validate on create.
  const [dslText, setDslText] = useState(DSL_TEMPLATE);
  const [msg, setMsg] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);

  const dslHint = useMemo(() => {
    let v: unknown;
    try {
      v = JSON.parse(dslText);
    } catch (e) {
      return { ok: false as const, msg: `JSON 语法错误：${e instanceof Error ? e.message : "无法解析"}` };
    }
    if (typeof v !== "object" || v === null || Array.isArray(v)) return { ok: false as const, msg: "DSL 必须是 JSON 对象" };
    const d = v as Record<string, unknown>;
    if (!Array.isArray(d.nodes) || !(d.nodes as unknown[]).length)
      return { ok: false as const, msg: "缺少 nodes 数组（至少一个节点）" };
    if (typeof d.entry !== "string") return { ok: false as const, msg: "缺少 entry（入口节点 id）" };
    const ids = new Set((d.nodes as Array<{ id?: string }>).map((n) => n.id));
    if (!ids.has(d.entry)) return { ok: false as const, msg: `entry "${d.entry}" 不在 nodes 里` };
    return { ok: true as const, msg: `DSL 结构正常 · ${(d.nodes as unknown[]).length} 个节点` };
  }, [dslText]);

  async function create() {
    if (!dslHint.ok) {
      setMsg(dslHint.msg);
      return;
    }
    const dsl = JSON.parse(dslText) as Record<string, unknown>;
    if (name.trim()) dsl.name = name.trim();
    if (desc.trim()) dsl.description = desc.trim();
    const r = await api.createWorkflow({ name: (dsl.name as string) || name || "新流程", description: desc, dsl: dsl as never });
    const body = r as { ok?: boolean; detail?: string };
    setMsg(body.ok ? "created" : body.detail || "error");
    if (body.ok) {
      setName("");
      setDesc("");
      setDslText(DSL_TEMPLATE);
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
        <input className="field mb-2" placeholder="name（覆盖 DSL 内的 name）" value={name} onChange={(e) => setName(e.target.value)} />
        <input className="field mb-2" placeholder="description" value={desc} onChange={(e) => setDesc(e.target.value)} />
        <textarea
          className={`field mb-1 font-mono text-xs ${dslHint.ok ? "" : "border-red/50"}`}
          rows={10}
          spellCheck={false}
          value={dslText}
          onChange={(e) => setDslText(e.target.value)}
        />
        <div className={`mb-2 text-[11px] ${dslHint.ok ? "text-faint" : "text-red"}`}>{dslHint.msg}</div>
        <div className="flex items-center gap-3">
          <button
            onClick={create}
            disabled={!dslHint.ok}
            className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
          >
            Create
          </button>
          {msg && <span className="text-xs text-muted">{msg}</span>}
        </div>
      </div>
    </div>
  );
}
