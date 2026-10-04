"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Check, Loader2, Save, Undo2, X } from "lucide-react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import type { WorkflowDef } from "@/lib/types";
import { useRunStatusLabel } from "@/components/chat/RunBlocks";
import { DiffView } from "../DiffView";
import { lineDiff, pretty } from "./diffLines";
import type { NodeStat } from "./useRunInspector";

type FieldKind = "text" | "area" | "json" | "number" | "select";
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
  | "hintQuestion";
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
 *  agent, not on the node, so it is deliberately absent here. */
function fieldsFor(node: DagNode): FieldSpec[] {
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
        { key: "parallel", label: "parallel", kind: "json", hint: "hintParallel" },
      ];
    case "branch":
      return [
        ...common,
        { key: "cases", label: "cases", kind: "json", hint: "hintCases" },
        { key: "default", label: "default", kind: "text", hint: "hintDefault" },
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
  }, [baseline]);

  const dirty = specs.some((f) => (draft[f.key] ?? "") !== baseline[f.key]);

  // The confirm card renders at the foot of a long form — bring it into view.
  const confirmRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (editing) confirmRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [editing]);

  // Parse one field into its DSL value; null = invalid, undefined = delete key.
  const parseField = (f: FieldSpec): { ok: true; value: unknown } | { ok: false; msg: string } => {
    const raw = draft[f.key] ?? "";
    if (!raw.trim()) return { ok: true, value: undefined };
    if (f.kind === "json") {
      try {
        return { ok: true, value: JSON.parse(raw) };
      } catch (e) {
        return { ok: false, msg: t("invalidJson", { field: f.label, msg: (e as Error).message }) };
      }
    }
    if (f.kind === "number") {
      const n = Number(raw);
      if (!Number.isFinite(n)) return { ok: false, msg: t("notNumber", { field: f.label }) };
      return { ok: true, value: Number.isInteger(n) ? n : n };
    }
    return { ok: true, value: raw };
  };

  const buildDraft = (): { ok: true; dsl: Record<string, unknown>; node: DagNode } | { ok: false; msg: string } => {
    const next: DagNode = { ...node };
    for (const f of specs) {
      const p = parseField(f);
      if (!p.ok) return { ok: false, msg: p.msg };
      if (p.value === undefined) delete next[f.key];
      else next[f.key] = p.value;
    }
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
    } catch {
      setErr(tCommon("runtimeUnreachable"));
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
          const bad =
            (f.kind === "json" && val.trim() !== "" && !isJson(val)) ||
            (f.kind === "number" && val.trim() !== "" && !Number.isFinite(Number(val)));
          return (
            <div key={f.key}>
              <label className="mb-1 block font-mono text-[10px] uppercase tracking-wide text-faint">
                {f.label}
              </label>
              {f.kind === "select" ? (
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
              ) : f.kind === "area" || f.kind === "json" ? (
                <textarea
                  value={val}
                  onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
                  rows={f.kind === "json" ? 4 : 2}
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
            </div>
          );
        })}
      </div>

      <div className="rounded-lg border border-dashed border-line2 px-2.5 py-2 text-[10.5px] text-faint">
        {t("topologyNote")}
      </div>

      {err && (
        <div className="flex items-start gap-1.5 rounded-md border border-red/30 bg-red/[0.06] px-2 py-1.5 text-[11px] text-red">
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