"use client";

import { useEffect, useRef, useState } from "react";
import { Bot, Check, FileInput, FlaskConical, Loader2, Wrench, X } from "lucide-react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import { relTime } from "@/lib/utils";
import { useGinno } from "@/lib/store";
import type { WorkflowDef } from "@/lib/types";

type DslNode = { id: string; type?: string; title?: string; goal?: string; agent?: string; [k: string]: unknown };

/** Node-list delta between the draft and the current recipe's DSL (阶段4):
 * added/removed by id, changed = same id but the node body differs. Purely
 * client-side (the draft is not a version, so diffWorkflowVersions can't be
 * used); layout-only keys are stripped so a pure reposition isn't "changed". */
function nodeDelta(draft: Record<string, unknown>, cur: Record<string, unknown> | undefined) {
  const strip = (n: DslNode) => {
    const { position: _p, pos: _q, ...rest } = n;
    return JSON.stringify(rest);
  };
  const dn = (draft?.nodes as DslNode[]) || [];
  const cn = ((cur?.nodes as DslNode[]) || []) as DslNode[];
  const cMap = new Map(cn.map((n) => [n.id, n]));
  const dMap = new Map(dn.map((n) => [n.id, n]));
  return {
    added: dn.filter((n) => !cMap.has(n.id)).map((n) => n.id),
    removed: cn.filter((n) => !dMap.has(n.id)).map((n) => n.id),
    changed: dn
      .filter((n) => cMap.has(n.id) && strip(n) !== strip(cMap.get(n.id) as DslNode))
      .map((n) => n.id),
  };
}

/**
 * Studio「从会话导入」(方案B 阶段4, design B §4 屏3): turn any session's
 * conversation (or a selected message RANGE of it) into a workflow draft.
 *
 *   step 1  pick source session + start/end trace rows (contiguous range —
 *           rows are CHECKPOINT-message indices, exactly what the backend slices)
 *   step 2  background synthesis with live attempt progress (2s case polling —
 *           the Studio has no session-scoped WS to carry synthesis.event frames)
 *   step 3  confirm: 创建新配方 (v1) or 并入当前配方 (v(N+1), with a node-list
 *           delta preview)
 *
 * On failure the modal STAYS open with the reason inline (the draft must not be
 * lost silently) — same rule as the chat-side SummarizeModal.
 */
export function ImportFromSessionModal({
  wf,
  onClose,
  onDone,
}: {
  /** Currently selected Studio recipe — the merge target (v(N+1)). */
  wf: WorkflowDef;
  onClose: () => void;
  /** Success receipt: the Studio shows it as a green notice bar after close. */
  onDone: (message: string) => void;
}) {
  const g = useGinno();
  const [step, setStep] = useState<"pick" | "draft">("pick");
  const [selId, setSelId] = useState<string | null>(null);
  const [rows, setRows] = useState<api.SynthesisTraceRow[] | null>(null);
  const [traceLoading, setTraceLoading] = useState(false);
  const [traceErr, setTraceErr] = useState<string | null>(null);
  // Range selection = pick a start row, then an end row; the span highlights.
  // null start = full session (no range param sent).
  const [startI, setStartI] = useState<number | null>(null);
  const [endI, setEndI] = useState<number | null>(null);

  // Synthesis wait state (mirrors ChatStream's polling fallback: the Studio
  // doesn't hold the source session's WS, so polling IS the primary channel).
  const [pendingId, setPendingId] = useState<string | null>(null);
  // Kept after the wait resolves — the confirm calls need the case id for the
  // adoption backfill (pendingRef is cleared on resolve).
  const [synthesisId, setSynthesisId] = useState<string | null>(null);
  const [attemptCount, setAttemptCount] = useState(0);
  const [synthErr, setSynthErr] = useState<string | null>(null);
  const pendingRef = useRef<string | null>(null);
  const [draft, setDraft] = useState<Record<string, unknown> | null>(null);

  const [confirmBusy, setConfirmBusy] = useState<"create" | "merge" | null>(null);
  const [confirmErr, setConfirmErr] = useState<string | null>(null);
  // wf.import 域文案（异步回调捕获渲染期 t，与既有 useChatT 模式一致）。
  const t = useTranslations("wf.import");
  const tCommon = useTranslations("wf.common");
  const tInspector = useTranslations("wf.inspector");
  // 试运行 of the draft (zero-LLM preflight; receipt copied from SummarizeModal).
  const [dry, setDry] = useState<{ busy: boolean; result: api.DryRunResult | null; err: string | null }>({
    busy: false,
    result: null,
    err: null,
  });

  const sessions = [...g.sessions].sort((a, b) => (b.updated || 0) - (a.updated || 0));

  const pickSession = async (id: string) => {
    setSelId(id);
    setRows(null);
    setTraceErr(null);
    setStartI(null);
    setEndI(null);
    setTraceLoading(true);
    try {
      const r = await api.getSummarizeTrace(id);
      if (!r.ok || !r.rows.length) {
        setTraceErr(t("traceErrNoHistory"));
      } else {
        setRows(r.rows);
      }
    } catch {
      setTraceErr(t("traceErrRead"));
    } finally {
      setTraceLoading(false);
    }
  };

  const clickRow = (i: number) => {
    if (startI === null || endI !== null) {
      // fresh selection
      setStartI(i);
      setEndI(null);
    } else {
      setStartI(Math.min(startI, i));
      setEndI(Math.max(startI, i));
    }
  };

  const startSynthesis = async () => {
    if (!selId) return;
    setSynthErr(null);
    setConfirmErr(null);
    try {
      const range =
        startI !== null && endI !== null ? { start: Math.min(startI, endI), end: Math.max(startI, endI) } : undefined;
      const r = await api.summarizeSessionToDsl(selId, undefined, undefined, range);
      if (r.ok && r.synthesis_id) {
        pendingRef.current = r.synthesis_id;
        setSynthesisId(r.synthesis_id);
        setPendingId(r.synthesis_id);
        setAttemptCount(0);
      } else {
        setSynthErr(t("synthFailed", { detail: r.detail ?? r.error ?? "unknown" }));
      }
    } catch {
      setSynthErr(t("synthFailedConn"));
    }
  };

  const resolveWait = async (id: string) => {
    if (pendingRef.current !== id) return; // already resolved
    pendingRef.current = null;
    setPendingId(null);
    try {
      const r = await api.getSynthesisCase(id);
      const out = r.case?.output;
      if (r.ok && out?.status === "ok" && out.dsl) {
        setDraft(out.dsl as Record<string, unknown>);
        setStep("draft");
      } else {
        setSynthErr(t("synthFailedStage", { stage: out?.fail_stage || "unknown", id }));
      }
    } catch {
      setSynthErr(t("synthFailedConn"));
    }
  };

  // Poll the synthesis case until output lands (2s interval, 180s timeout).
  useEffect(() => {
    if (!pendingId) return;
    const id = pendingId;
    const started = Date.now();
    const timer = setInterval(() => {
      if (pendingRef.current !== id) {
        clearInterval(timer);
        return;
      }
      api
        .getSynthesisCase(id)
        .then((r) => {
          if (r.case?.attempts?.length) setAttemptCount(r.case.attempts.length);
          if (r.ok && r.case?.output) void resolveWait(id);
        })
        .catch(() => {
          /* transient — retry on the next tick */
        });
      if (Date.now() - started > 180_000) {
        clearInterval(timer);
        pendingRef.current = null;
        setPendingId(null);
        setSynthErr(t("synthFailedTimeout", { id }));
      }
    }, 2000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingId]);

  useEffect(() => {
    setDry((d) => (d.result || d.err ? { busy: false, result: null, err: null } : d));
  }, [draft]);

  const runDry = async () => {
    if (!draft) return;
    setDry({ busy: true, result: null, err: null });
    try {
      const r = await api.dryRunWorkflow(draft);
      setDry({ busy: false, result: r, err: null });
    } catch {
      setDry({ busy: false, result: null, err: t("dryErr") });
    }
  };

  const confirm = async (mode: "create" | "merge") => {
    if (!draft) return;
    setConfirmBusy(mode);
    setConfirmErr(null);
    try {
      const r =
        mode === "merge"
          ? await api.createWorkflow({ dsl: draft, synthesis_id: synthesisId!, workflow_id: wf.id })
          : await api.createWorkflow({ dsl: draft, synthesis_id: synthesisId! });
      if (r.ok && r.workflow) {
        void g.reloadWorkflows();
        onDone(
          mode === "merge"
            ? t("merged", { name: wf.name, version: r.workflow.version ?? (wf.version ?? 1) + 1 })
            : t("created", { name: r.workflow.name }),
        );
        onClose();
      } else {
        setConfirmErr(t("saveFailed"));
      }
    } catch {
      setConfirmErr(t("saveFailedConn"));
    } finally {
      setConfirmBusy(null);
    }
  };

  const nodes = (draft?.nodes as DslNode[]) || [];
  const lo = startI !== null && endI !== null ? Math.min(startI, endI) : null;
  const hi = startI !== null && endI !== null ? Math.max(startI, endI) : null;
  const delta = draft ? nodeDelta(draft, wf.dsl) : null;

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4" onClick={onClose} role="dialog">
      <div
        className="flex max-h-[85vh] w-full max-w-3xl flex-col overflow-hidden rounded-xl border border-line2 bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* header */}
        <div className="flex items-center gap-2 border-b border-line px-4 py-3">
          <FileInput className="h-4 w-4 text-violet" />
          <span className="text-sm font-semibold text-txt">{t("title")}</span>
          <span className="ml-auto text-xs text-faint">
            {step === "pick" ? t("stepPick") : t("stepDraft")}
          </span>
          <button onClick={onClose} className="rounded p-1 text-faint hover:bg-card2 hover:text-txt" aria-label={t("close")}>
            <X className="h-4 w-4" />
          </button>
        </div>

        {step === "pick" ? (
          <div className="flex min-h-0 flex-1">
            {/* session list */}
            <div className="flex w-[240px] shrink-0 flex-col overflow-y-auto border-r border-line p-2">
              <div className="px-1.5 pb-1.5 text-[10px] font-medium uppercase tracking-wide text-faint">{t("sessions")}</div>
              {!sessions.length && <div className="p-2 text-xs text-faint">{t("noSessions")}</div>}
              {sessions.map((s) => (
                <button
                  key={s.id}
                  onClick={() => void pickSession(s.id)}
                  className={`mb-0.5 flex w-full flex-col items-start gap-0.5 rounded-md px-2 py-1.5 text-left transition-colors ${
                    selId === s.id ? "bg-card2" : "hover:bg-card2/60"
                  }`}
                >
                  <span className="w-full truncate text-xs text-txt">{s.title || t("untitledSession")}</span>
                  <span className="flex w-full items-center gap-1.5 text-[10px] text-faint">
                    <Bot className="h-3 w-3" />
                    {s.agent_id || "—"}
                    <span className="ml-auto">{relTime(s.updated)}</span>
                  </span>
                </button>
              ))}
            </div>

            {/* trace preview + range selection */}
            <div className="flex min-w-0 flex-1 flex-col p-3">
              <div className="mb-1.5 flex items-center text-[10px] font-medium uppercase tracking-wide text-faint">
                {t("tracePreview")}
                {rows && (
                  <span className="ml-auto normal-case">
                    {lo !== null ? t("selectedRange", { lo, hi: hi ?? 0 }) : t("pickRangeHint")}
                  </span>
                )}
                {rows && lo === null && (
                  <button
                    onClick={() => {
                      setStartI(0);
                      setEndI(rows.length - 1);
                    }}
                    className="ml-2 rounded border border-line px-1.5 py-0.5 text-[10px] normal-case text-muted hover:text-txt"
                  >
                    {t("selectAll")}
                  </button>
                )}
              </div>
              {traceLoading && <div className="flex items-center gap-1.5 p-3 text-xs text-faint"><Loader2 className="h-3.5 w-3.5 animate-spin" /> {tCommon("loading")}</div>}
              {traceErr && <div className="rounded-md border border-red/30 bg-red/[0.06] px-2 py-1.5 text-xs text-red">{traceErr}</div>}
              {!traceLoading && !traceErr && !rows && (
                <div className="flex flex-1 items-center justify-center text-xs text-faint">{t("pickSession")}</div>
              )}
              {rows && (
                <div className="min-h-0 flex-1 overflow-y-auto rounded-lg border border-line bg-base">
                  {rows.map((row) => {
                    const inSpan = lo !== null && row.i >= lo && row.i <= (hi as number);
                    const isEdge = row.i === startI || row.i === endI;
                    return (
                      <button
                        key={row.i}
                        onClick={() => clickRow(row.i)}
                        className={`flex w-full items-start gap-2 border-b border-line/60 px-2 py-1 text-left text-[11px] last:border-b-0 ${
                          isEdge ? "bg-violet/[0.14]" : inSpan ? "bg-violet/[0.06]" : "hover:bg-card2/60"
                        }`}
                      >
                        <span className="w-6 shrink-0 pt-px text-right font-mono text-[10px] text-faint">{row.i}</span>
                        {row.role === "user" && <span className="shrink-0 rounded bg-card2 px-1 text-[9px] uppercase text-muted">user</span>}
                        {row.role === "assistant" && <span className="shrink-0 rounded bg-violet/15 px-1 text-[9px] uppercase text-violet">agent</span>}
                        {row.role === "tool" && (
                          <span className="flex shrink-0 items-center gap-0.5 text-[10px] text-yellow">
                            <Wrench className="h-3 w-3" />
                            {row.name}
                          </span>
                        )}
                        {row.role === "system" && <span className="shrink-0 text-[9px] uppercase text-faint">sys</span>}
                        <span className="min-w-0 flex-1 truncate text-muted">
                          {row.role === "assistant" && row.tools?.length ? `[${row.tools.join(", ")}] ` : ""}
                          {row.text || t("noText")}
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
              {synthErr && (
                <div className="mt-2 rounded-md border border-red/30 bg-red/[0.06] px-2 py-1.5 text-xs text-red">{synthErr}</div>
              )}
              <div className="mt-2 flex items-center gap-2">
                {selId && (
                  <button
                    onClick={() => {
                      setStep("draft");
                      void startSynthesis();
                    }}
                    disabled={traceLoading || !rows}
                    title={t("summarizeTitle")}
                    className="btn-press ml-auto rounded-md bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90"
                  >
                    {t("summarize")}
                  </button>
                )}
              </div>
            </div>
          </div>
        ) : (
          <div className="flex min-h-0 flex-1 flex-col overflow-y-auto p-4">
            {pendingId ? (
              <div className="flex flex-1 flex-col items-center justify-center gap-2 text-sm text-muted">
                <Loader2 className="h-5 w-5 animate-spin text-violet" />
                {attemptCount > 1 ? t("attempt", { count: attemptCount }) : t("summarizing")}
                <span className="text-[11px] text-faint">{t("summarizingHint")}</span>
              </div>
            ) : (
              <>
                <div className="mb-2 text-[10px] font-medium uppercase tracking-wide text-faint">
                  {t("draftNodes", { count: nodes.length })}
                </div>
                <div className="space-y-2">
                  {nodes.map((n, i) => (
                    <div key={n.id} className="rounded-md border border-line bg-card p-2.5 text-xs">
                      <div className="flex items-center gap-2">
                        <span className="rounded bg-card2 px-1.5 py-0.5 font-mono text-[10px] text-faint">{i + 1}</span>
                        <span className="font-medium text-txt">{n.title || n.goal || n.id}</span>
                        <span className="ml-auto shrink-0 text-[10px] uppercase text-faint">{n.type || t("nodeStep")}</span>
                      </div>
                      {n.goal && n.title && <div className="mt-1 text-[11px] text-muted">{n.goal}</div>}
                    </div>
                  ))}
                  {!nodes.length && <div className="py-4 text-center text-xs text-faint">{t("noNodes")}</div>}
                </div>

                {/* 试运行 receipt (mirrors SummarizeModal) */}
                <div className="mt-3">
                  <button
                    onClick={runDry}
                    disabled={dry.busy || confirmBusy !== null}
                    title={tInspector("dryRunTitle")}
                    className="btn-press flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[11px] text-faint hover:bg-card2 hover:text-muted disabled:opacity-50"
                  >
                    {dry.busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <FlaskConical className="h-3 w-3" />}
                    {dry.busy ? tInspector("dryRunning") : tInspector("dryRun")}
                  </button>
                  {dry.result &&
                    (dry.result.ok ? (
                      <div className="mt-1.5 space-y-0.5 rounded-md border border-green/30 bg-green/[0.06] px-2 py-1.5 text-xs text-green">
                        <div className="flex items-center gap-1.5">
                          <Check className="h-3.5 w-3.5 shrink-0" />
                          {tCommon("dryPassed", { count: dry.result.node_count ?? 0 })}
                        </div>
                        {dry.result.warnings.length > 0 && (
                          <div className="pl-5 text-[11px] text-yellow">
                            {tInspector("warningsLine", {
                              count: dry.result.warnings.length,
                              list: dry.result.warnings.map((w) => w.message).join("; "),
                            })}
                          </div>
                        )}
                        {dry.result.unreachable.length > 0 && (
                          <div className="pl-5 text-[11px] text-yellow">
                            {tCommon("unreachable", { list: dry.result.unreachable.join(", ") })}
                          </div>
                        )}
                      </div>
                    ) : (
                      <div className="mt-1.5 max-h-28 space-y-0.5 overflow-y-auto rounded-md border border-red/30 bg-red/[0.06] px-2 py-1.5 text-xs text-red">
                        <div>{tCommon("dryFailed")}</div>
                        {dry.result.errors.map((e, i) => (
                          <div key={`e${i}`} className="pl-3 text-[11px]">· {e}</div>
                        ))}
                        {dry.result.doctor_errors.map((e, i) => (
                          <div key={`d${i}`} className="pl-3 text-[11px]">· {e.message}</div>
                        ))}
                      </div>
                    ))}
                  {dry.err && (
                    <div className="mt-1.5 rounded-md border border-red/30 bg-red/[0.06] px-2 py-1.5 text-xs text-red">{dry.err}</div>
                  )}
                </div>

                {/* node-list delta for the merge path (draft vs current recipe) */}
                {delta && (
                  <div className="mt-3 rounded-md border border-line bg-base px-2 py-1.5 text-[11px] text-muted">
                    <span className="font-medium text-txt">
                      {t("mergedInto", { name: wf.name, version: wf.version ?? 1 })}
                    </span>
                    {delta.added.length === 0 && delta.removed.length === 0 && delta.changed.length === 0 ? (
                      <span className="ml-1 text-faint">{t("deltaNone")}</span>
                    ) : (
                      <span className="ml-1">
                        {delta.added.length > 0 && <span className="text-green">{t("deltaAdded", { list: delta.added.join(", ") })}</span>}
                        {delta.removed.length > 0 && <span className="text-red">{t("deltaRemoved", { list: delta.removed.join(", ") })}</span>}
                        {delta.changed.length > 0 && <span className="text-yellow">{t("deltaChanged", { list: delta.changed.join(", ") })}</span>}
                      </span>
                    )}
                  </div>
                )}

                {synthErr && (
                  <div className="mt-2 rounded-md border border-red/30 bg-red/[0.06] px-2 py-1.5 text-xs text-red">{synthErr}</div>
                )}
                {confirmErr && (
                  <div className="mt-2 rounded-md border border-red/30 bg-red/[0.06] px-2 py-1.5 text-xs text-red">{confirmErr}</div>
                )}
              </>
            )}
          </div>
        )}

        {/* footer actions */}
        <div className="flex items-center gap-2 border-t border-line px-4 py-3">
          {step === "pick" ? (
            <button onClick={onClose} className="btn-press rounded-md border border-line px-3 py-1.5 text-xs text-muted hover:bg-card2">
              {t("cancel")}
            </button>
          ) : (
            <>
              <button
                onClick={() => {
                  setStep("pick");
                  setDraft(null);
                  setSynthErr(null);
                  setSynthesisId(null);
                  setConfirmErr(null);
                }}
                disabled={confirmBusy !== null}
                className="btn-press rounded-md border border-line px-3 py-1.5 text-xs text-muted hover:bg-card2 disabled:opacity-50"
              >
                {t("reselect")}
              </button>
              <div className="ml-auto flex gap-2">
                <button
                  onClick={() => void confirm("create")}
                  disabled={!!pendingId || confirmBusy !== null}
                  className="btn-press flex items-center gap-1 rounded-md border border-line px-3 py-1.5 text-xs text-txt hover:bg-card2 disabled:opacity-50"
                >
                  {confirmBusy === "create" && <Loader2 className="h-3 w-3 animate-spin" />}
                  {t("createNew")}
                </button>
                <button
                  onClick={() => void confirm("merge")}
                  disabled={!!pendingId || confirmBusy !== null}
                  title={t("mergeTitle", { name: wf.name, version: (wf.version ?? 1) + 1 })}
                  className="btn-press flex items-center gap-1 rounded-md bg-gradient-to-r from-violet to-fuchsia px-3 py-1.5 text-xs font-semibold text-white hover:opacity-90 disabled:opacity-50"
                >
                  {confirmBusy === "merge" && <Loader2 className="h-3 w-3 animate-spin" />}
                  {t("mergeInto", { version: (wf.version ?? 1) + 1 })}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
