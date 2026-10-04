"use client";

import { useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { WorkflowDef } from "@/lib/types";
import { WorkflowInspector } from "@/components/workflow/WorkflowInspector";

// DSL 骨架模板里的人读字段（name/goal）按 locale 翻译，结构保持不变
function dslTemplate(newName: string, stepHint: string): string {
  return `{
  "name": "${newName}",
  "description": "",
  "entry": "s1",
  "context": { "schema": { "type": "object", "properties": {} }, "initial": {} },
  "nodes": [
    { "id": "s1", "type": "step", "agent": "dev", "goal": "${stepHint}" }
  ],
  "edges": []
}`;
}

function DslPreview({ wf }: { wf: WorkflowDef }) {
  const t = useTranslations("settings.workflows");
  const [open, setOpen] = useState(false);
  if (!wf.dsl) return null;
  return (
    <div className="mt-1.5">
      <button
        onClick={() => setOpen((o) => !o)}
        className="text-[11px] text-faint transition-colors hover:text-muted"
      >
        {open ? t("hideDsl") : t("viewDsl")}
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
  const t = useTranslations("settings.workflows");
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  // P3 #15: DSL v1 JSON editor (replaces the legacy steps-array field) with
  // live parse/structure hints; the server runs the full validate on create.
  const [dslText, setDslText] = useState(() => dslTemplate(t("newWorkflowName"), t("dslStepHint")));
  const [msg, setMsg] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);

  const dslHint = useMemo(() => {
    let v: unknown;
    try {
      v = JSON.parse(dslText);
    } catch (e) {
      return { ok: false as const, msg: t("jsonError", { error: e instanceof Error ? e.message : t("cannotParse") }) };
    }
    if (typeof v !== "object" || v === null || Array.isArray(v)) return { ok: false as const, msg: t("mustBeObject") };
    const d = v as Record<string, unknown>;
    if (!Array.isArray(d.nodes) || !(d.nodes as unknown[]).length)
      return { ok: false as const, msg: t("missingNodes") };
    if (typeof d.entry !== "string") return { ok: false as const, msg: t("missingEntry") };
    const ids = new Set((d.nodes as Array<{ id?: string }>).map((n) => n.id));
    if (!ids.has(d.entry)) return { ok: false as const, msg: t("entryNotInNodes", { entry: String(d.entry) }) };
    return { ok: true as const, msg: t("dslOk", { count: (d.nodes as unknown[]).length }) };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dslText, t]);

  async function create() {
    if (!dslHint.ok) {
      setMsg(dslHint.msg);
      return;
    }
    const dsl = JSON.parse(dslText) as Record<string, unknown>;
    if (name.trim()) dsl.name = name.trim();
    if (desc.trim()) dsl.description = desc.trim();
    const r = await api.createWorkflow({ name: (dsl.name as string) || name || t("newWorkflowName"), description: desc, dsl: dsl as never });
    const body = r as { ok?: boolean; detail?: string };
    setMsg(body.ok ? t("created") : body.detail || t("error"));
    if (body.ok) {
      setName("");
      setDesc("");
      setDslText(dslTemplate(t("newWorkflowName"), t("dslStepHint")));
      g.reloadWorkflows();
    }
  }
  async function del(id: string) {
    await api.deleteWorkflow(id);
    g.reloadWorkflows();
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">{t("title")}</h2>
      <p className="mt-1 text-sm text-muted">
        {t.rich("description", {
          span: (chunks) => <span className="text-txt">{chunks}</span>,
        })}
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
                  {openId === w.id ? t("hide") : t("details")}
                </button>
                <button onClick={() => del(w.id)} className="text-xs text-faint hover:text-red">
                  {t("delete")}
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
        {g.workflows.length === 0 && <div className="text-xs text-faint">{t("empty")}</div>}
      </div>
      <div className="mt-5 rounded-xl border border-line bg-card p-3">
        <div className="mb-2 text-sm font-medium text-txt">{t("newTitle")}</div>
        <input className="field mb-2" placeholder={t("namePlaceholder")} value={name} onChange={(e) => setName(e.target.value)} />
        <input className="field mb-2" placeholder={t("descPlaceholder")} value={desc} onChange={(e) => setDesc(e.target.value)} />
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
            {t("createButton")}
          </button>
          {msg && <span className="text-xs text-muted">{msg}</span>}
        </div>
      </div>
    </div>
  );
}
