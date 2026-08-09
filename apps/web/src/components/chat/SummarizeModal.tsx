"use client";

import { useEffect, useMemo, useState } from "react";
import { Bot, Check, Loader2, Plus, RotateCcw, Trash2, Workflow, X } from "lucide-react";
import { WorkflowDag } from "@/components/workflow/WorkflowDag";

type DslNode = { id: string; type?: string; title?: string; goal?: string; agent?: string; [k: string]: unknown };

/**
 * 聊天页「总结成流程」确认弹层 (workflow-ux-redesign S2/S3): DAG preview on the
 * left, editable node cards + context variables on the right. User can tweak
 * titles/goals, drop nodes, mark variables required — then 创建并运行 / 仅创建
 * / 进入开发会话精炼. On failure the modal STAYS open and shows the reason
 * inline (the draft must not be lost silently).
 */
export function SummarizeModal({
  dsl,
  busy,
  error,
  createdName,
  sourceLabel,
  onClose,
  onCreate,
  onRetry,
  onOpenDevSession,
}: {
  dsl: Record<string, unknown>;
  busy?: "create" | "run" | "dev" | null;
  error?: string | null;
  /** Create-only success receipt — switches the modal to a confirmed state. */
  createdName?: string | null;
  sourceLabel?: string;
  onClose: () => void;
  onCreate: (run: boolean, editedDsl: Record<string, unknown>) => void;
  onRetry?: () => void;
  onOpenDevSession?: (editedDsl: Record<string, unknown>) => void;
}) {
  const [local, setLocal] = useState<Record<string, unknown>>(dsl);
  const [showJson, setShowJson] = useState(false);
  // Raw-text mirror for the JSON editor: intermediate invalid states keep the
  // caret; the draft updates once the text parses again (ContextEditor pattern).
  const [rawJson, setRawJson] = useState(() => JSON.stringify(dsl, null, 2));
  const [jsonErr, setJsonErr] = useState<string | null>(null);
  const [newVar, setNewVar] = useState("");

  const editJson = (text: string) => {
    setRawJson(text);
    try {
      const v = JSON.parse(text);
      if (typeof v === "object" && v !== null && !Array.isArray(v)) {
        setLocal(v as Record<string, unknown>);
        setJsonErr(null);
      } else {
        setJsonErr("DSL 必须是 JSON 对象");
      }
    } catch (e) {
      setJsonErr(e instanceof Error ? e.message : "JSON 语法错误");
    }
  };

  // If a NEW dsl object arrives while the modal is open (e.g. ↺ re-summarize),
  // resync the editable state — useState(dsl) alone would keep the stale draft.
  useEffect(() => {
    setLocal(dsl);
    setRawJson(JSON.stringify(dsl, null, 2));
    setJsonErr(null);
  }, [dsl]);

  const name = (local.name as string) || "新流程";
  const nodes = (local.nodes as DslNode[]) || [];

  // {{variable}} placeholders used anywhere in the DSL (S3 guidance row).
  const vars = useMemo(() => {
    const found = new Set<string>();
    for (const m of JSON.stringify(local).matchAll(/\{\{\s*([a-zA-Z0-9_]+)\s*\}\}/g)) found.add(m[1]);
    const props = (((local.context as Record<string, unknown>) || {}).schema as Record<string, unknown> | undefined)
      ?.properties as Record<string, unknown> | undefined;
    for (const k of Object.keys(props || {})) found.add(k);
    return [...found];
  }, [local]);
  const requiredVars = useMemo(() => {
    const req = (((local.context as Record<string, unknown>) || {}).schema as Record<string, unknown> | undefined)
      ?.required;
    return Array.isArray(req) ? (req as string[]) : [];
  }, [local]);

  const patchNode = (id: string, patch: Partial<DslNode>) =>
    setLocal((cur) => ({
      ...cur,
      nodes: ((cur.nodes as DslNode[]) || []).map((n) => (n.id === id ? { ...n, ...patch } : n)),
    }));

  const deleteNode = (id: string) =>
    setLocal((cur) => {
      const rest = ((cur.nodes as DslNode[]) || []).filter((n) => n.id !== id);
      const edges = ((cur.edges as Array<{ from: string; to: string }>) || []).filter(
        (e) => e.from !== id && e.to !== id,
      );
      const next: Record<string, unknown> = { ...cur, nodes: rest, edges };
      if (cur.entry === id && rest.length) next.entry = rest[0].id;
      return next;
    });

  const ensureContextShape = (cur: Record<string, unknown>): Record<string, unknown> => {
    const ctx = { ...((cur.context as Record<string, unknown>) || {}) };
    const schema = { ...((ctx.schema as Record<string, unknown>) || {}) };
    schema.properties = { ...((schema.properties as Record<string, unknown>) || {}) };
    ctx.schema = schema;
    ctx.initial = { ...((ctx.initial as Record<string, unknown>) || {}) };
    return ctx;
  };

  const toggleRequired = (v: string) =>
    setLocal((cur) => {
      const ctx = ensureContextShape(cur);
      const schema = ctx.schema as Record<string, unknown>;
      const req = Array.isArray(schema.required) ? [...(schema.required as string[])] : [];
      const i = req.indexOf(v);
      if (i >= 0) req.splice(i, 1);
      else req.push(v);
      schema.required = req;
      return { ...cur, context: ctx };
    });

  const addVariable = () => {
    const v = newVar.trim();
    if (!v || vars.includes(v)) return;
    setLocal((cur) => {
      const ctx = ensureContextShape(cur);
      const schema = ctx.schema as Record<string, unknown>;
      (schema.properties as Record<string, unknown>)[v] = { type: "string" };
      (ctx.initial as Record<string, unknown>)[v] = "";
      return { ...cur, context: ctx };
    });
    setNewVar("");
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4" onClick={onClose} role="dialog">
      <div
        className="flex max-h-[85vh] w-full max-w-3xl flex-col overflow-hidden rounded-xl border border-line2 bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* header */}
        <div className="flex items-center gap-2 border-b border-line px-4 py-3">
          <Workflow className="h-4 w-4 text-violet" />
          <span className="text-sm font-semibold text-txt">总结成流程 · {name}</span>
          <span className="ml-auto text-xs text-faint">
            {sourceLabel ? `从「${sourceLabel}」提炼 · ` : ""}草稿未保存
          </span>
          <button onClick={onClose} className="rounded p-1 text-faint hover:bg-card2 hover:text-txt" aria-label="关闭">
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* body: DAG left / editable details right */}
        <div className="flex min-h-0 flex-1">
          <div className="flex w-[300px] shrink-0 flex-col gap-3 overflow-y-auto border-r border-line p-4">
            <div className="text-[10px] font-medium uppercase tracking-wide text-faint">
              结构预览 · {nodes.length} 个节点
            </div>
            <WorkflowDag
              interactive={false}
              dsl={local as unknown as {
                entry?: string;
                nodes?: Array<{ id: string; type: string; title?: string; goal?: string; agent?: string }>;
                edges?: Array<{ from: string; to: string }>;
              }}
            />
            {onRetry && (
              <button
                onClick={onRetry}
                disabled={!!busy}
                className="btn-press mt-auto flex items-center gap-1 self-start rounded-md border border-line px-2 py-1 text-[11px] text-faint hover:bg-card2 hover:text-muted disabled:opacity-50"
              >
                <RotateCcw className="h-3 w-3" /> 重新总结
              </button>
            )}
          </div>

          <div className="flex-1 space-y-3 overflow-y-auto p-4">
            <div className="flex items-center">
              <span className="text-[10px] font-medium uppercase tracking-wide text-faint">节点</span>
              <button
                onClick={() => {
                  if (!showJson) {
                    // entering JSON view: sync the raw text from the card state
                    setRawJson(JSON.stringify(local, null, 2));
                    setJsonErr(null);
                  }
                  setShowJson((v) => !v);
                }}
                className="ml-auto text-[11px] text-faint hover:text-muted"
              >
                {showJson ? "返回卡片视图" : "展开 JSON"}
              </button>
            </div>

            {showJson ? (
              <div className="space-y-1">
                <textarea
                  value={rawJson}
                  onChange={(e) => editJson(e.target.value)}
                  spellCheck={false}
                  className={`max-h-[42vh] min-h-[160px] w-full overflow-auto whitespace-pre rounded-lg border bg-base p-3 font-mono text-[11px] leading-relaxed text-violet/90 focus:outline-none ${
                    jsonErr ? "border-red/50" : "border-line focus:border-violet/60"
                  }`}
                />
                {jsonErr && <div className="text-[11px] text-red">{jsonErr}</div>}
              </div>
            ) : (
              <div className="space-y-2">
                {nodes.map((n, i) => (
                  <div key={n.id} className="group rounded-md border border-line bg-card p-2.5 text-xs">
                    <div className="mb-1 flex items-center gap-2">
                      <span className="rounded bg-card2 px-1.5 py-0.5 font-mono text-[10px] text-faint">{i + 1}</span>
                      <input
                        value={n.title || n.goal || ""}
                        placeholder="节点名称"
                        onChange={(e) => patchNode(n.id, n.title !== undefined || !n.goal ? { title: e.target.value } : { goal: e.target.value })}
                        className="min-w-0 flex-1 border-b border-transparent bg-transparent font-medium text-txt outline-none placeholder:text-faint focus:border-violet"
                      />
                      <span className="shrink-0 text-[10px] uppercase text-faint">{n.type || "step"}</span>
                      <button
                        onClick={() => deleteNode(n.id)}
                        title="删除节点"
                        className="shrink-0 rounded p-0.5 text-faint opacity-0 transition-opacity hover:bg-red/10 hover:text-red group-hover:opacity-100"
                      >
                        <Trash2 className="h-3 w-3" />
                      </button>
                    </div>
                    {n.goal && n.title && (
                      <textarea
                        value={n.goal}
                        rows={2}
                        placeholder="节点目标"
                        onChange={(e) => patchNode(n.id, { goal: e.target.value })}
                        onInput={(e) => {
                          // S3: auto-grow with content instead of an inner scrollbar
                          e.currentTarget.style.height = "auto";
                          e.currentTarget.style.height = `${e.currentTarget.scrollHeight}px`;
                        }}
                        className="w-full resize-none overflow-hidden rounded border border-transparent bg-transparent text-muted outline-none placeholder:text-faint focus:border-violet/40"
                      />
                    )}
                  </div>
                ))}
                {!nodes.length && <div className="py-4 text-center text-xs text-faint">草稿中没有节点</div>}
              </div>
            )}

            {/* context variables (S3): what changes per-run */}
            <div className="pt-1">
              <div className="mb-1.5 text-[10px] font-medium uppercase tracking-wide text-faint">上下文变量</div>
              {vars.length ? (
                <div className="flex flex-wrap items-center gap-1.5">
                  {vars.map((v) => {
                    const req = requiredVars.includes(v);
                    return (
                      <button
                        key={v}
                        onClick={() => toggleRequired(v)}
                        title={req ? "必填 — 点击改为可选" : "可选 — 点击改为必填"}
                        className={`flex items-center gap-1 rounded-full border px-2 py-0.5 font-mono text-[11px] ${
                          req ? "border-yellow/50 bg-yellow/10 text-yellow" : "border-line text-muted hover:text-txt"
                        }`}
                      >
                        {`{{${v}}}`}
                        <span className="text-[9px]">{req ? "必填" : "可选"}</span>
                      </button>
                    );
                  })}
                </div>
              ) : (
                <div className="text-[11px] italic text-faint">未识别到变量 — 运行时输入写死在 DSL 里，可手动添加</div>
              )}
              <div className="mt-1.5 flex items-center gap-1.5">
                <input
                  value={newVar}
                  onChange={(e) => setNewVar(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && addVariable()}
                  placeholder="变量名"
                  className="w-32 rounded border border-line bg-base px-2 py-1 font-mono text-[11px] text-txt placeholder:text-faint focus:border-violet/60 focus:outline-none"
                />
                <button
                  onClick={addVariable}
                  className="btn-press flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[11px] text-muted hover:bg-card2 hover:text-txt"
                >
                  <Plus className="h-3 w-3" /> 添加变量
                </button>
              </div>
            </div>
          </div>
        </div>

        {createdName ? (
          <div className="mx-4 mb-2 flex items-center gap-1.5 rounded-md border border-green/30 bg-green/[0.06] px-2 py-1.5 text-xs text-green">
            <Check className="h-3.5 w-3.5" /> 已创建工作流「{createdName}」，可在 Workflows 页查看与运行
          </div>
        ) : (
          error && (
            <div className="mx-4 mb-2 rounded-md border border-red/30 bg-red/[0.06] px-2 py-1.5 text-xs text-red">
              {error}
            </div>
          )
        )}

        {/* footer actions */}
        <div className="flex items-center gap-2 border-t border-line px-4 py-3">
          {createdName ? (
            <button
              onClick={onClose}
              className="btn-press ml-auto rounded-md bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90"
            >
              完成
            </button>
          ) : (
            <>
              <button onClick={onClose} className="btn-press rounded-md border border-line px-3 py-1.5 text-xs text-muted hover:bg-card2">
                取消
              </button>
              {onOpenDevSession && (
                <button
                  disabled={!!busy}
                  onClick={() => onOpenDevSession(local)}
                  className="btn-press flex items-center gap-1 rounded-md border border-violet/40 px-3 py-1.5 text-xs text-violet hover:bg-violet/[0.06] disabled:opacity-50"
                >
                  {busy === "dev" ? <Loader2 className="h-3 w-3 animate-spin" /> : <Bot className="h-3 w-3" />}
                  {busy === "dev" ? "创建并打开…" : "进入开发会话精炼"}
                </button>
              )}
              <div className="ml-auto flex gap-2">
                <button
                  disabled={!!busy}
                  onClick={() => onCreate(false, local)}
                  className="btn-press flex items-center gap-1 rounded-md border border-line px-3 py-1.5 text-xs text-txt hover:bg-card2 disabled:opacity-50"
                >
                  {busy === "create" && <Loader2 className="h-3 w-3 animate-spin" />}
                  {busy === "create" ? "创建中…" : "仅创建"}
                </button>
                <button
                  disabled={!!busy}
                  onClick={() => onCreate(true, local)}
                  className="btn-press flex items-center gap-1 rounded-md bg-gradient-to-r from-violet to-fuchsia px-3 py-1.5 text-xs font-semibold text-white hover:opacity-90 disabled:opacity-50"
                >
                  {busy === "run" && <Loader2 className="h-3 w-3 animate-spin" />}
                  {busy === "run" ? "运行中…" : "创建并运行"}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
