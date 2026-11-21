"use client";

import { useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { WorkflowDef } from "@/lib/types";
import { WorkflowInspector } from "@/components/workflow/WorkflowInspector";
import { ConfirmModal } from "@/components/ConfirmModal";
import {
  makeStepDraft,
  stepsRepresentation,
  stepsToDsl,
  validateSteps,
  type StepDraft,
  type StepIssue,
  type StepsDsl,
} from "@/lib/workflows/stepsDsl";

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
  // 新建面板双模式：默认「步骤编辑器」（有序步骤 → 真实可执行的线性 agent
  // 链 DSL）；「高级 JSON」保留原有手写 DSL 全部能力（branch/loop/human、
  // context schema、模板表达式……）。json→steps 方向只在无损可表达时切换，
  // 否则留在 JSON 模式并提示，绝不默默降级丢草稿。
  const [mode, setMode] = useState<"steps" | "json">("steps");
  const [steps, setSteps] = useState<StepDraft[]>(() => [makeStepDraft("k1")]);
  const stepKeyRef = useRef(2);
  // json→steps 被拒的原因：parse=语法错误；complex=含不可表达结构（index 定位到具体节点）
  const [switchNote, setSwitchNote] = useState<{ kind: "parse" } | { kind: "complex"; index?: number } | null>(null);
  // P3 #15: DSL v1 JSON editor with live parse/structure hints; the server
  // runs the full validate on create.
  const [dslText, setDslText] = useState(() => dslTemplate(t("newWorkflowName"), t("dslStepHint")));
  const [msg, setMsg] = useState("");
  const [creating, setCreating] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  // 删除确认（仅自建工作流；内置由后端 is_system_def 保护，UI 直接不渲染入口）
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [delError, setDelError] = useState<{ id: string; msg: string } | null>(null);

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
    const ids = new Set<string>();
    for (const [i, node] of d.nodes.entries()) {
      if (!node || typeof node !== "object" || Array.isArray(node) || typeof node.id !== "string" || !node.id.trim()) {
        return { ok: false as const, msg: t("invalidNode", { index: i + 1 }) };
      }
      ids.add(node.id);
    }
    if (!ids.has(d.entry)) return { ok: false as const, msg: t("entryNotInNodes", { entry: String(d.entry) }) };
    return { ok: true as const, msg: t("dslOk", { count: (d.nodes as unknown[]).length }) };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dslText, t]);

  const stepIssues = useMemo(() => validateSteps(steps), [steps]);
  const issueFor = (i: number): StepIssue | undefined => stepIssues.find((x) => x.index === i);

  function addStep() {
    setSteps((s) => [...s, makeStepDraft(`k${stepKeyRef.current++}`)]);
  }
  function removeStep(key: string) {
    setSteps((s) => s.filter((x) => x.key !== key));
  }
  function patchStep(key: string, patch: Partial<Pick<StepDraft, "title" | "goal">>) {
    setSteps((s) => s.map((x) => (x.key === key ? { ...x, ...patch } : x)));
  }

  /** steps → json：把当前草稿序列化进 JSON 编辑器（草稿本就是可表达形态）。 */
  function switchToJson() {
    const dsl = stepsToDsl(steps);
    if (dsl && name) dsl.name = name;
    if (dsl && desc) dsl.description = desc;
    setDslText(JSON.stringify(dsl ?? { nodes: [], edges: [] }, null, 2));
    setSwitchNote(null);
    setMode("json");
  }

  /** json → steps：仅在 JSON 能被步骤编辑器无损表达时切换。 */
  function switchToSteps() {
    let v: unknown;
    try {
      v = JSON.parse(dslText);
    } catch {
      setSwitchNote({ kind: "parse" });
      return;
    }
    const rep = stepsRepresentation(v);
    if (!rep.ok) {
      setSwitchNote(rep.reason === "bad_node" ? { kind: "complex", index: rep.index } : { kind: "complex" });
      return;
    }
    const meta = v as Record<string, unknown>;
    if (typeof meta.name === "string") setName(meta.name);
    if (typeof meta.description === "string") setDesc(meta.description);
    setSteps(rep.steps);
    setSwitchNote(null);
    setMode("steps");
  }

  async function create() {
    if (creating) return;
    let dsl: StepsDsl | Record<string, unknown>;
    if (mode === "steps") {
      if (stepIssues.length > 0) return;
      const built = stepsToDsl(steps);
      if (!built) return;
      dsl = built;
    } else {
      if (!dslHint.ok) {
        setMsg(dslHint.msg);
        return;
      }
      dsl = JSON.parse(dslText) as Record<string, unknown>;
    }
    if (name.trim()) dsl.name = name.trim();
    if (desc.trim()) dsl.description = desc.trim();
    setCreating(true);
    try {
      const r = await api.createWorkflow({ name: (dsl.name as string) || name || t("newWorkflowName"), description: desc, dsl: dsl as never });
      const body = r as { ok?: boolean; detail?: string };
      setMsg(body.ok ? t("created") : body.detail || t("error"));
      if (body.ok) {
        setName("");
        setDesc("");
        if (mode === "steps") setSteps([makeStepDraft(`k${stepKeyRef.current++}`)]);
        else setDslText(dslTemplate(t("newWorkflowName"), t("dslStepHint")));
        g.reloadWorkflows();
      }
    } catch {
      setMsg(t("error"));
    } finally {
      setCreating(false);
    }
  }

  function askDelete(w: WorkflowDef) {
    setDelError(null);
    setConfirmId(w.id);
  }

  async function doDelete(w: WorkflowDef) {
    // 防重复：同一时刻只允许一个在途删除请求（确认按钮连点直接忽略）
    if (deletingId) return;
    setDeletingId(w.id);
    setDelError(null);
    try {
      const r = await api.deleteWorkflow(w.id);
      const body = r as { ok?: boolean; error?: string };
      if (body.ok) {
        setConfirmId(null);
        g.reloadWorkflows();
      } else {
        // 后端拒绝（409 语义内置保护等）或 not found：关弹窗露出行内错误，可重试
        setConfirmId(null);
        setDelError({ id: w.id, msg: body.error || t("error") });
      }
    } catch {
      setConfirmId(null);
      setDelError({ id: w.id, msg: t("error") });
    } finally {
      setDeletingId(null);
    }
  }

  const confirmTarget = confirmId ? g.workflows.find((x) => x.id === confirmId) : null;

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
              <span className="selectable text-xs text-faint">[{w.id}]</span>
              {w.version != null && (
                <span className="rounded border border-line2 px-1.5 py-0.5 text-[10px] text-faint">
                  v{w.version}
                </span>
              )}
              {/* 内置工作流（后端 meta.system 为真源）：明确标识 + 无删除入口 */}
              {w.system && (
                <span
                  title={t("builtinHint")}
                  className="rounded border border-line2 px-1.5 py-0.5 text-[10px] text-faint"
                >
                  {t("builtinBadge")}
                </span>
              )}
              <div className="ml-auto flex items-center gap-2">
                <button
                  onClick={() => setOpenId((o) => (o === w.id ? null : w.id))}
                  className="text-xs text-faint transition-colors hover:text-txt"
                >
                  {openId === w.id ? t("hide") : t("details")}
                </button>
                {!w.system && (
                  <button
                    onClick={() => askDelete(w)}
                    disabled={deletingId === w.id}
                    className="text-xs text-faint hover:text-red disabled:opacity-50"
                  >
                    {deletingId === w.id ? t("deleting") : t("delete")}
                  </button>
                )}
              </div>
            </div>
            {w.description && <div className="selectable mt-1 text-xs text-muted">{w.description}</div>}
            {/* 删除失败行内展示（模态已关），删按钮仍在，可直接重试 */}
            {delError?.id === w.id && (
              <div className="selectable mt-1 text-[11px] text-red">
                {t("deleteFailed", { error: delError.msg })}
              </div>
            )}
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
        <div className="mb-2 flex items-center justify-between">
          <div className="text-sm font-medium text-txt">{t("newTitle")}</div>
          <div className="inline-flex overflow-hidden rounded-lg border border-line2 text-xs">
            <button
              onClick={() => mode !== "steps" && switchToSteps()}
              className={`px-2.5 py-1 transition-colors ${mode === "steps" ? "bg-violet/15 font-medium text-txt" : "text-faint hover:text-muted"}`}
            >
              {t("modeSteps")}
            </button>
            <button
              onClick={() => mode !== "json" && switchToJson()}
              className={`px-2.5 py-1 transition-colors ${mode === "json" ? "bg-violet/15 font-medium text-txt" : "text-faint hover:text-muted"}`}
            >
              {t("modeJson")}
            </button>
          </div>
        </div>
        {switchNote && (
          <div className={`mb-2 text-[11px] ${switchNote.kind === "parse" ? "text-red" : "text-muted"}`}>
            {switchNote.kind === "parse"
              ? t("jsonInvalidSwitch")
              : switchNote.index != null
                ? t("jsonBadNode", { index: switchNote.index })
                : t("jsonTooComplex")}
          </div>
        )}
        <input className="field mb-2" placeholder={t("namePlaceholder")} value={name} onChange={(e) => setName(e.target.value)} />
        <input className="field mb-2" placeholder={t("descPlaceholder")} value={desc} onChange={(e) => setDesc(e.target.value)} />
        {mode === "steps" ? (
          <div className="mb-2 space-y-2">
            {steps.map((s, i) => {
              const issue = issueFor(i + 1);
              return (
                <div key={s.key} className={`rounded-lg border p-2 ${issue ? "border-red/50" : "border-line"}`}>
                  <div className="mb-1.5 flex items-center gap-2">
                    <span className="shrink-0 text-[11px] font-medium text-faint">#{i + 1}</span>
                    <input
                      className="field flex-1"
                      placeholder={t("stepNamePlaceholder")}
                      value={s.title}
                      onChange={(e) => patchStep(s.key, { title: e.target.value })}
                    />
                    <button
                      onClick={() => removeStep(s.key)}
                      className="shrink-0 text-[11px] text-faint transition-colors hover:text-red"
                    >
                      {t("removeStep")}
                    </button>
                  </div>
                  <textarea
                    className="field"
                    rows={2}
                    placeholder={t("stepGoalPlaceholder")}
                    value={s.goal}
                    onChange={(e) => patchStep(s.key, { goal: e.target.value })}
                  />
                  {issue && (
                    <div className="mt-1 text-[11px] text-red">
                      {issue.code === "blank"
                        ? t("stepBlank", { index: i + 1 })
                        : t("stepNoGoal", { index: i + 1 })}
                    </div>
                  )}
                </div>
              );
            })}
            <button onClick={addStep} className="text-xs text-faint transition-colors hover:text-txt">
              + {t("addStep")}
            </button>
          </div>
        ) : (
          <>
            <textarea
              className={`field mb-1 font-mono text-xs ${dslHint.ok ? "" : "border-red/50"}`}
              rows={10}
              spellCheck={false}
              value={dslText}
              onChange={(e) => setDslText(e.target.value)}
            />
            <div className={`selectable mb-2 text-[11px] ${dslHint.ok ? "text-faint" : "text-red"}`}>{dslHint.msg}</div>
          </>
        )}
        <div className="flex items-center gap-3">
          <button
            onClick={create}
            disabled={creating || (mode === "steps" ? stepIssues.length > 0 : !dslHint.ok)}
            className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
          >
            {t("createButton")}
          </button>
          {msg && <span className="selectable text-xs text-muted">{msg}</span>}
        </div>
      </div>
      {confirmTarget && (
        <ConfirmModal
          title={t("deleteConfirmTitle", { name: confirmTarget.name })}
          message={t("deleteConfirmBody")}
          onConfirm={() => void doDelete(confirmTarget)}
          onCancel={() => {
            setConfirmId(null);
          }}
        />
      )}
    </div>
  );
}
