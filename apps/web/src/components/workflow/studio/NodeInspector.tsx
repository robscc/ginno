"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Braces, Check, Loader2, Plus, Save, Undo2, X } from "lucide-react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import type { WorkflowDef } from "@/lib/types";
import { useRunStatusLabel } from "@/components/chat/RunBlocks";
import { DiffView } from "../DiffView";
import { lineDiff, pretty } from "./diffLines";
import {
  applyDraftFields,
  condRowsToDraft,
  contextKeys,
  nodeIds,
  parseCondRows,
  parseRows,
  rowsToDraft,
  templateIssues,
  type CondRow,
  type KvRow,
} from "./fieldCodecs";
import type { NodeStat } from "./useRunInspector";

type FieldKind = "text" | "area" | "json" | "number" | "select" | "node" | "kv-map" | "cond-list";
/** 提示文案 key 的字面量联合（wf.node.hint*），保住 useTranslations 的 key 检查。 */
type NodeHintKey =
  | "hintTitle"
  | "hintGoal"
  | "hintAgent"
  | "hintWrites"
  | "hintExtractModel"
  | "hintTimeout"
  | "hintOnError"
  | "hintRetry"
  | "hintOver"
  | "hintMaxIters"
  | "hintParallel"
  | "hintCases"
  | "hintDefault"
  | "hintQuestion"
  | "hintEntry"
  | "hintArgs"
  | "hintBody";
type FieldSpec = {
  key: string;
  label: string;
  kind: FieldKind;
  /** 提示文案的 i18n key（wf.node.hint* 域），渲染处随 hook 取译。 */
  hint?: NodeHintKey;
  options?: string[];
  placeholder?: string;
};

type DagNode = Record<string, unknown> & { id: string; type: string };

/** Editable fields per node type — the REAL DSL surface (dsl.py validation),
 *  not the aspirational one in the design doc. `tools_allow` lives on the
 *  agent, not on the node, so it is deliberately absent here.
 *  Exported for tests (fieldsFor is pure: node → specs, no React state). */
export function fieldsFor(node: DagNode): FieldSpec[] {
  const common: FieldSpec[] = [
    { key: "title", label: "title", kind: "text", hint: "hintTitle" },
  ];
  switch (node.type) {
    case "step":
    case "agent":
    case "llm":
      return [
        ...common,
        { key: "goal", label: "goal", kind: "area", hint: "hintGoal" },
        { key: "agent", label: "agent", kind: "text", hint: "hintAgent" },
        { key: "writes", label: "writes", kind: "json", hint: "hintWrites" },
        { key: "extract_model", label: "extract_model", kind: "text", hint: "hintExtractModel" },
        { key: "timeout_s", label: "timeout_s", kind: "number", hint: "hintTimeout" },
        { key: "on_error", label: "on_error", kind: "select", options: ["", "stop", "continue"], hint: "hintOnError" },
        { key: "retry", label: "retry", kind: "json", hint: "hintRetry" },
      ];
    case "loop":
      return [
        ...common,
        { key: "over", label: "over", kind: "text", hint: "hintOver" },
        { key: "max_iters", label: "max_iters", kind: "number", hint: "hintMaxIters" },
        // body 从画布 DAG 节点下拉选（node = input + datalist；出口是显式
        // edge，Inspector 拓扑只读，不在字段面）。
        { key: "body", label: "body", kind: "node", hint: "hintBody" },
        { key: "parallel", label: "parallel", kind: "json", hint: "hintParallel" },
      ];
    case "branch":
      return [
        ...common,
        { key: "cases", label: "cases", kind: "cond-list", hint: "hintCases" },
        { key: "default", label: "default", kind: "node", hint: "hintDefault" },
      ];
    case "python":
      // entry 白名单来自后端注册表（scripts/ENTRY_REGISTRY），经只读端点
      // 下发；离线/未连 sidecar 时渲染层降级手输（不阻塞编辑）。
      return [
        ...common,
        { key: "entry", label: "entry", kind: "select", hint: "hintEntry" },
        { key: "args", label: "args", kind: "kv-map", hint: "hintArgs" },
        { key: "writes", label: "writes", kind: "json", hint: "hintWrites" },
        { key: "timeout", label: "timeout", kind: "number", hint: "hintTimeout" },
      ];
    case "human":
      return [
        ...common,
        { key: "question", label: "question", kind: "area", hint: "hintQuestion" },
      ];
    default:
      return common;
  }
}

function initialText(v: unknown): string {
  if (v === undefined || v === null) return "";
  if (typeof v === "object") return pretty(v);
  return String(v);
}

/** python entry 白名单的模块级缓存（一次会话只拉一次；失败缓存 null → 手输降级）。 */
let _pyEntryCache: { names: string[] } | null | undefined;
async function loadPythonEntries(): Promise<string[] | null> {
  if (_pyEntryCache !== undefined) return _pyEntryCache?.names ?? null;
  try {
    const r = await api.listPythonEntries();
    _pyEntryCache = { names: r.entries.map((e) => e.name).sort() };
  } catch {
    _pyEntryCache = null;
  }
  return _pyEntryCache?.names ?? null;
}

/**
 * Node inspector (design B §4 屏 1): edit a node's parameters in place. An edit
 * produces a NEW immutable DSL version — the draft is dry-run checked
 * (zero-LLM), shown as a diff, and only then committed through the existing
 * `PUT /api/workflows/{id}`.
 *
 * The canvas never edits topology: ids, types and edges are read-only here.
 */
export function NodeInspector({
  wf,
  node,
  status,
  stat,
  onSaved,
}: {
  wf: WorkflowDef;
  node: DagNode;
  status?: string;
  stat?: NodeStat;
  onSaved: () => void;
}) {
  const specs = useMemo(() => fieldsFor(node), [node]);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState(false);
  const [diff, setDiff] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState("");
  /** 结构化字段（kv-map/cond-list）的 JSON 高级模式开关——per-field，随节点切换重置。 */
  const [jsonMode, setJsonMode] = useState<Record<string, boolean>>({});
  /** python entry 白名单（null = 未加载/失败 → 降级手输）。 */
  const [pyEntries, setPyEntries] = useState<string[] | null>(null);
  // wf 域文案；节点/步骤状态经 chat.status.* key 渲染。
  const t = useTranslations("wf.node");
  const tCommon = useTranslations("wf.common");
  const statusLabel = useRunStatusLabel();

  const baseline = useMemo(() => {
    const out: Record<string, string> = {};
    for (const f of specs) out[f.key] = initialText(node[f.key]);
    return out;
  }, [specs, node]);

  // Re-seed the draft whenever a different node (or a new version) is selected.
  useEffect(() => {
    setDraft(baseline);
    setEditing(false);
    setDiff(null);
    setErr(null);
    setNote("");
    setJsonMode({});
  }, [baseline]);

  // entry 下拉白名单：只有 python 节点才拉（复用模块缓存，切节点零开销）。
  useEffect(() => {
    if (node.type !== "python") return;
    void loadPythonEntries().then(setPyEntries);
  }, [node.type]);

  const dagNodeIds = useMemo(() => nodeIds(wf.dsl), [wf.dsl]);
  const ctxKeys = useMemo(() => contextKeys(wf.dsl), [wf.dsl]);

  /**
   * 锁定的结构化字段：当前 draft 无法用行编辑器无损表达（嵌套 args /
   * 带 transform 的 case / JSON 模式下手改坏）→ 只读 + 提示切 JSON，
   * buildDraft 对这些字段原样保留原值（fieldCodecs.applyDraftFields 钉死）。
   */
  const lockedKeys = useMemo(() => {
    const locked = new Set<string>();
    for (const f of specs) {
      if (jsonMode[f.key]) continue; // JSON 高级模式：全量编辑，无锁
      const raw = draft[f.key] ?? "";
      if (f.kind === "kv-map" && parseRows(raw) === null) locked.add(f.key);
      if (f.kind === "cond-list" && parseCondRows(raw) === null) locked.add(f.key);
    }
    return locked;
  }, [specs, draft, jsonMode]);

  const dirty = specs.some((f) => (draft[f.key] ?? "") !== baseline[f.key]);

  // The confirm card renders at the foot of a long form — bring it into view.
  const confirmRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (editing) confirmRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [editing]);

  // Parse one field into its DSL value; null = invalid, undefined = delete key.
  const parseField = (f: FieldSpec): { ok: true; value: unknown } | { ok: false; msg: string } => {
    if (lockedKeys.has(f.key)) return { ok: true, value: node[f.key] }; // 锁定：原样保留
    const raw = draft[f.key] ?? "";
    if (!raw.trim()) return { ok: true, value: undefined };
    if (f.kind === "json" || f.kind === "kv-map" || f.kind === "cond-list") {
      try {
        return { ok: true, value: JSON.parse(raw) };
      } catch (e) {
        return { ok: false, msg: t("invalidJson", { field: f.label, msg: (e as Error).message }) };
      }
    }
    if (f.kind === "number") {
      const n = Number(raw);
      if (!Number.isFinite(n)) return { ok: false, msg: t("notNumber", { field: f.label }) };
      return { ok: true, value: n };
    }
    return { ok: true, value: raw };
  };

  const buildDraft = (): { ok: true; dsl: Record<string, unknown>; node: DagNode } | { ok: false; msg: string } => {
    for (const f of specs) {
      const p = parseField(f);
      if (!p.ok) return { ok: false, msg: p.msg };
    }
    // 纯逻辑核心在 fieldCodecs.applyDraftFields（vitest 钉住「locked 字段
    // 绝不静默丢」）；locked 字段跳过 = 保留原值。
    const next = applyDraftFields(
      node,
      specs.map((f) => ({ key: f.key, kind: f.kind })),
      draft,
      lockedKeys,
    ) as DagNode;
    const dsl = wf.dsl as { nodes?: DagNode[] } | undefined;
    if (!dsl?.nodes) return { ok: false, msg: t("noDsl") };
    return {
      ok: true,
      node: next,
      dsl: { ...(wf.dsl as Record<string, unknown>), nodes: dsl.nodes.map((n) => (n.id === node.id ? next : n)) },
    };
  };

  const preview = async () => {
    setErr(null);
    const built = buildDraft();
    if (!built.ok) {
      setErr(built.msg);
      return;
    }
    setBusy(true);
    try {
      // Zero-LLM preflight: a draft that doesn't validate/compile never reaches
      // the store (the PUT would 400 anyway — this explains why first).
      const dry = await api.dryRunWorkflow(built.dsl);
      if (!dry.ok) {
        setBusy(false);
        setErr(
          t("draftFailed", {
            detail: [...dry.errors, ...dry.doctor_errors.map((d) => d.message)]
              .slice(0, 3)
              .join("; "),
          }),
        );
        return;
      }
      setDiff(lineDiff(pretty(node), pretty(built.node), `${node.id}.json`));
      setEditing(true);
    } catch {
      setErr(tCommon("runtimeUnreachable"));
    } finally {
      setBusy(false);
    }
  };

  const commit = async () => {
    const built = buildDraft();
    if (!built.ok) {
      setErr(built.msg);
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const r = await api.updateWorkflow(wf.id, {
        dsl: built.dsl,
        commit: note.trim() || `studio: edited node ${node.id}`,
      });
      if (!r.ok) {
        setErr(t("saveRejected"));
        setBusy(false);
        return;
      }
      setEditing(false);
      setDiff(null);
      onSaved();
    } catch (e) {
      // Q6：updateWorkflow 走 jsonStrict，非 2xx 抛的 Error 带 ginnoStatus
      // （HTTP 状态码）且 message=服务端 body.detail。对照设置页/TasksPanel
      // 的分型范式：服务端明确拒绝（4xx，含将来的 409 并发冲突；5xx 同为
      // 已送达的明确响应）→ 「保存失败：{服务端原因}」行内透传，不再丢
      // detail 只显泛型文案；断连/超时（TypeError）→ 中性「无法连接
      // runtime」，不漏浏览器技术串。
      const st = (e as { ginnoStatus?: number } | null)?.ginnoStatus;
      if (typeof st === "number") {
        const reason = e instanceof Error && e.message ? e.message : `HTTP ${st}`;
        setErr(t("saveRejectedDetail", { reason }));
      } else if (e instanceof TypeError) {
        setErr(tCommon("runtimeUnreachable"));
      } else {
        setErr(t("saveRejected"));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-1.5">
        <span className="font-mono text-[11px] text-muted">⬡</span>
        <span className="text-[12.5px] font-semibold text-txt">{t("title", { id: node.id })}</span>
        <span className="rounded bg-card2 px-1.5 py-px font-mono text-[10px] text-faint">{node.type}</span>
        {status && <span className="ml-auto font-mono text-[10px] text-faint">{statusLabel(status)}</span>}
      </div>

      {(stat?.latencyMs !== undefined || stat?.tokens) && (
        <div className="flex gap-3 rounded-lg border border-line bg-base/30 px-2.5 py-1.5 font-mono text-[10.5px] text-faint">
          {stat?.latencyMs !== undefined && (
            <span>
              {t("latency")}{" "}
              <span className="tabular-nums text-muted">
                {stat.latencyMs >= 1000 ? `${(stat.latencyMs / 1000).toFixed(1)}s` : `${Math.round(stat.latencyMs)}ms`}
              </span>
            </span>
          )}
          {!!stat?.tokens && (
            <span>
              {t("tokens")} <span className="tabular-nums text-muted">{stat.tokens}</span>
            </span>
          )}
        </div>
      )}

      <div className="space-y-2.5">
        {specs.map((f) => {
          const val = draft[f.key] ?? "";
          const structured = f.kind === "kv-map" || f.kind === "cond-list";
          const asJson = structured && jsonMode[f.key];
          const showJsonUi = f.kind === "json" || f.kind === "area" || asJson;
          const bad =
            (showJsonUi && f.kind !== "area" && val.trim() !== "" && !isJson(val)) ||
            (f.kind === "number" && val.trim() !== "" && !Number.isFinite(Number(val)));
          // entry 的动态白名单：拉到 → 下拉；未拉到/失败 → 手输降级（不阻塞）。
          const entryOpts = f.key === "entry" && f.kind === "select" ? pyEntries : null;
          return (
            <div key={f.key}>
              {structured ? (
                <div className="mb-1 flex items-center gap-2">
                  <label className="block font-mono text-[10px] uppercase tracking-wide text-faint">
                    {f.label}
                  </label>
                  <button
                    type="button"
                    onClick={() => setJsonMode((m) => ({ ...m, [f.key]: !m[f.key] }))}
                    className="ml-auto flex items-center gap-1 rounded border border-line2 px-1.5 py-px font-mono text-[9.5px] text-faint hover:text-txt"
                    title={asJson ? t("useForm") : t("useJson")}
                  >
                    <Braces className="h-2.5 w-2.5" />
                    {asJson ? t("useForm") : t("useJson")}
                  </button>
                </div>
              ) : (
                // 非 structured 字段保持 label 与输入控件直接相邻——packaged UI
                // e2e 用 `label:text-is("goal") + textarea` 兄弟选择器（勿破坏）。
                <label className="mb-1 block font-mono text-[10px] uppercase tracking-wide text-faint">
                  {f.label}
                </label>
              )}
              {f.kind === "select" && entryOpts ? (
                <select
                  value={val}
                  onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
                  className="w-full rounded border border-line2 bg-card px-2 py-1 text-[11.5px] text-txt outline-none focus:border-violet/60"
                >
                  <option value="">{t("entryNone")}</option>
                  {entryOpts.map((o) => (
                    <option key={o} value={o}>
                      {o}
                    </option>
                  ))}
                </select>
              ) : f.kind === "select" && !f.options ? (
                <input
                  value={val}
                  onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
                  className="w-full rounded border border-line2 bg-card px-2 py-1 font-mono text-[11.5px] text-txt outline-none placeholder:text-faint focus:border-violet/60"
                />
              ) : f.kind === "select" ? (
                <select
                  value={val}
                  onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
                  className="w-full rounded border border-line2 bg-card px-2 py-1 text-[11.5px] text-txt outline-none focus:border-violet/60"
                >
                  {(f.options || []).map((o) => (
                    <option key={o} value={o}>
                      {o === "" ? t("defaultStop") : o}
                    </option>
                  ))}
                </select>
              ) : f.kind === "node" ? (
                <input
                  value={val}
                  list={WF_NODE_DATALIST}
                  onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
                  placeholder={t("phNode")}
                  className="w-full rounded border border-line2 bg-card px-2 py-1 font-mono text-[11.5px] text-txt outline-none placeholder:text-faint focus:border-violet/60"
                />
              ) : structured && !asJson ? (
                lockedKeys.has(f.key) ? (
                  <div className="space-y-1.5 rounded-md border border-yellow/40 bg-yellow/[0.06] px-2 py-1.5">
                    <div className="flex items-start gap-1.5 text-[10.5px] text-yellow">
                      <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                      <span>{t("lockedHint", { field: f.label })}</span>
                    </div>
                    <pre className="max-h-40 overflow-auto rounded bg-base/60 p-1.5 font-mono text-[10px] leading-relaxed text-muted">
                      {val}
                    </pre>
                    <button
                      type="button"
                      onClick={() => setJsonMode((m) => ({ ...m, [f.key]: true }))}
                      className="flex items-center gap-1 rounded border border-yellow/50 px-1.5 py-0.5 text-[10px] text-yellow hover:bg-yellow/10"
                    >
                      <Braces className="h-2.5 w-2.5" />
                      {t("useJson")}
                    </button>
                  </div>
                ) : f.kind === "kv-map" ? (
                  <KvRowsEditor
                    rows={parseRows(val) ?? []}
                    ctxKeys={ctxKeys}
                    onRows={(rows) => setDraft((d) => ({ ...d, [f.key]: rowsToDraft(rows) }))}
                  />
                ) : (
                  <CondRowsEditor
                    rows={parseCondRows(val) ?? []}
                    onRows={(rows) => setDraft((d) => ({ ...d, [f.key]: condRowsToDraft(rows) }))}
                  />
                )
              ) : showJsonUi ? (
                <textarea
                  value={val}
                  onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
                  rows={f.kind === "area" ? 2 : 4}
                  placeholder={f.placeholder}
                  className={`w-full resize-y rounded border bg-card px-2 py-1 font-mono text-[11px] text-txt outline-none placeholder:text-faint focus:border-violet/60 ${
                    bad ? "border-red/60" : "border-line2"
                  }`}
                />
              ) : (
                <input
                  value={val}
                  onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
                  className={`w-full rounded border bg-card px-2 py-1 text-[11.5px] text-txt outline-none placeholder:text-faint focus:border-violet/60 ${
                    bad ? "border-red/60" : "border-line2"
                  }`}
                />
              )}
              {f.hint && <div className="mt-0.5 text-[10px] text-faint">{t(f.hint)}</div>}
              {f.key === "entry" && f.kind === "select" && !entryOpts && (
                <div className="mt-0.5 text-[10px] text-faint">{t("entriesFallback")}</div>
              )}
            </div>
          );
        })}
      </div>

      <datalist id={WF_NODE_DATALIST}>
        {dagNodeIds.map((id) => (
          <option key={id} value={id} />
        ))}
      </datalist>

      <div className="rounded-lg border border-dashed border-line2 px-2.5 py-2 text-[10.5px] text-faint">
        {t("topologyNote")}
      </div>

      {err && (
        <div className="selectable flex items-start gap-1.5 rounded-md border border-red/30 bg-red/[0.06] px-2 py-1.5 text-[11px] text-red">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          <span>{err}</span>
        </div>
      )}

      {!editing ? (
        <div className="flex items-center gap-2">
          <button
            onClick={() => void preview()}
            disabled={!dirty || busy}
            className="btn-press flex items-center gap-1.5 rounded-md bg-violet px-3 py-1 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40"
          >
            {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />}
            {t("save")}
          </button>
          <button
            onClick={() => setDraft(baseline)}
            disabled={!dirty || busy}
            className="btn-press flex items-center gap-1.5 rounded-md border border-line2 px-2.5 py-1 text-xs text-muted hover:text-txt disabled:opacity-40"
          >
            <Undo2 className="h-3 w-3" />
            {t("revert")}
          </button>
          {dirty && <span className="text-[10.5px] text-yellow">{t("unsaved")}</span>}
        </div>
      ) : (
        <div ref={confirmRef} className="space-y-2 rounded-lg border border-violet/40 bg-violet/[0.04] p-2.5">
          <div className="flex items-center gap-1.5">
            <Check className="h-3.5 w-3.5 text-violet" />
            <span className="text-[11.5px] font-medium text-txt">
              {t("dryPassed", { version: (wf.version ?? 1) + 1 })}
            </span>
          </div>
          <DiffView diff={diff ?? ""} />
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder={t("notePlaceholder")}
            className="w-full rounded border border-line2 bg-card px-2 py-1 text-[11.5px] text-txt placeholder:text-faint focus:border-violet/60 focus:outline-none"
          />
          <div className="flex items-center gap-2">
            <button
              onClick={() => void commit()}
              disabled={busy}
              className="btn-press flex items-center gap-1.5 rounded-md bg-violet px-3 py-1 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
            >
              {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
              {t("apply", { version: (wf.version ?? 1) + 1 })}
            </button>
            <button
              onClick={() => {
                setEditing(false);
                setDiff(null);
              }}
              disabled={busy}
              className="btn-press flex items-center gap-1.5 rounded-md border border-line2 px-2.5 py-1 text-xs text-muted hover:text-txt disabled:opacity-50"
            >
              <X className="h-3 w-3" />
              {t("cancel")}
            </button>
            <span className="ml-auto text-[10px] text-faint">{t("untouchedHint")}</span>
          </div>
        </div>
      )}
    </div>
  );
}

function isJson(s: string): boolean {
  try {
    JSON.parse(s);
    return true;
  } catch {
    return false;
  }
}

/** 画布节点 id 下拉的 datalist id（Inspector 单实例，全局唯一即可）。 */
const WF_NODE_DATALIST = "wf-inspector-node-ids";

/** python args 的键值行编辑器（kv-map）。value 输入带 `{{}}` 模板粗检红边 +
 *  可用 context keys 插入下拉（DSL context.schema/initial 派生，离线恒可用）。 */
function KvRowsEditor({
  rows,
  ctxKeys,
  onRows,
}: {
  rows: KvRow[];
  ctxKeys: string[];
  onRows: (rows: KvRow[]) => void;
}) {
  const t = useTranslations("wf.node");
  const setRow = (i: number, patch: Partial<KvRow>) =>
    onRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <div className="space-y-1">
      {rows.map((r, i) => {
        const issues = templateIssues(String(r.value));
        return (
          <div key={i} className="flex items-center gap-1">
            <input
              value={r.key}
              onChange={(e) => setRow(i, { key: e.target.value })}
              placeholder={t("phArgKey")}
              aria-label={t("phArgKey")}
              className="w-[38%] rounded border border-line2 bg-card px-1.5 py-1 font-mono text-[11px] text-txt outline-none placeholder:text-faint focus:border-violet/60"
            />
            <input
              value={String(r.value)}
              onChange={(e) => setRow(i, { value: e.target.value })}
              placeholder={t("phArgValue")}
              aria-label={`${t("phArgValue")} ${r.key}`}
              className={`min-w-0 flex-1 rounded border bg-card px-1.5 py-1 font-mono text-[11px] text-txt outline-none placeholder:text-faint focus:border-violet/60 ${
                issues.length ? "border-red/60" : "border-line2"
              }`}
            />
            {ctxKeys.length > 0 && (
              <select
                value=""
                onChange={(e) => {
                  if (e.target.value) setRow(i, { value: `${String(r.value)}{{${e.target.value}}}` });
                }}
                title={t("insertCtx")}
                aria-label={t("insertCtx")}
                className="w-7 shrink-0 rounded border border-line2 bg-card px-0.5 py-1 font-mono text-[10px] text-faint outline-none hover:text-txt"
              >
                <option value="">{"{{}}"}</option>
                {ctxKeys.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            )}
            <button
              type="button"
              onClick={() => onRows(rows.filter((_, j) => j !== i))}
              aria-label={t("removeRow")}
              className="shrink-0 rounded p-0.5 text-faint hover:text-red"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        );
      })}
      {rows.some((r) => templateIssues(String(r.value)).length > 0) && (
        <div className="text-[10px] text-red">{t("tplIssueTitle")}</div>
      )}
      <button
        type="button"
        onClick={() => onRows([...rows, { key: "", value: "" }])}
        className="flex items-center gap-1 rounded border border-line2 px-1.5 py-0.5 text-[10px] text-muted hover:text-txt"
      >
        <Plus className="h-2.5 w-2.5" />
        {t("addRow")}
      </button>
    </div>
  );
}

/** branch cases 的条件行编辑器（cond-list）：`when` 表达式 → `then` 节点 id
 *  （then 走 datalist 从画布 DAG 节点选，也可手输）。 */
function CondRowsEditor({ rows, onRows }: { rows: CondRow[]; onRows: (rows: CondRow[]) => void }) {
  const t = useTranslations("wf.node");
  const setRow = (i: number, patch: Partial<CondRow>) =>
    onRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <div className="space-y-1">
      {rows.map((r, i) => {
        const issues = templateIssues(r.when);
        return (
          <div key={i} className="flex items-center gap-1">
            <input
              value={r.when}
              onChange={(e) => setRow(i, { when: e.target.value })}
              placeholder={t("phCondWhen")}
              aria-label={t("phCondWhen")}
              className={`min-w-0 flex-1 rounded border bg-card px-1.5 py-1 font-mono text-[11px] text-txt outline-none placeholder:text-faint focus:border-violet/60 ${
                issues.length ? "border-red/60" : "border-line2"
              }`}
            />
            <span className="shrink-0 font-mono text-[10px] text-faint">→</span>
            <input
              value={r.then}
              list={WF_NODE_DATALIST}
              onChange={(e) => setRow(i, { then: e.target.value })}
              placeholder={t("phCondThen")}
              aria-label={`${t("phCondThen")} ${i + 1}`}
              className="w-[42%] shrink-0 rounded border border-line2 bg-card px-1.5 py-1 font-mono text-[11px] text-txt outline-none placeholder:text-faint focus:border-violet/60"
            />
            <button
              type="button"
              onClick={() => onRows(rows.filter((_, j) => j !== i))}
              aria-label={t("removeRow")}
              className="shrink-0 rounded p-0.5 text-faint hover:text-red"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        );
      })}
      {rows.some((r) => templateIssues(r.when).length > 0) && (
        <div className="text-[10px] text-red">{t("tplIssueTitle")}</div>
      )}
      <button
        type="button"
        onClick={() => onRows([...rows, { when: "", then: "" }])}
        className="flex items-center gap-1 rounded border border-line2 px-1.5 py-0.5 text-[10px] text-muted hover:text-txt"
      >
        <Plus className="h-2.5 w-2.5" />
        {t("addRow")}
      </button>
    </div>
  );
}