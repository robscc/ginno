"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Check, ChevronDown, FlaskConical, Loader2, Play, Shield, ShieldAlert, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { WorkflowDef, WorkflowRun, WorkflowRunEvent } from "@/lib/types";
import { useRunStatusLabel } from "@/components/chat/RunBlocks";
import { useTriggerFeedback } from "@/lib/useTriggerFeedback";
import { WorkflowDag } from "./WorkflowDag";
import { WorkflowLogTimeline } from "./WorkflowLogTimeline";
import { ContextEditor } from "./ContextEditor";
import { RunErrorBox } from "./RunErrorBox";
import { VersionHistoryDrawer } from "./VersionHistoryDrawer";

const STEP_COLOR: Record<string, string> = {
  done: "#22c55e",
  running: "#3b82f6",
  failed: "#ef4444",
  paused: "#f59e0b",
  cancelled: "#71717a",
  interrupted: "#f97316",
  skipped: "#a1a1aa",
};

/** Full workflow inspector: editable context, run trigger, live DAG (click a node
 *  to filter the log), step list, and event timeline. Shared by the /workflows
 *  page (P4) and the Settings detail panel. Polls while a run is active.
 *
 *  A run selector exposes EVERY historical run of the workflow (not just the
 *  latest), so older failed runs remain inspectable — the core of the
 *  error-localization push. */
export function WorkflowInspector({ wf, runs }: { wf: WorkflowDef; runs: WorkflowRun[] }) {
  const router = useRouter();
  const g = useGinno();
  const [run, setRun] = useState<WorkflowRun | null>(null);
  const [events, setEvents] = useState<WorkflowRunEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [ctxOverride, setCtxOverride] = useState<Record<string, unknown>>({});
  const [selNode, setSelNode] = useState<string | null>(null);
  const [selRunId, setSelRunId] = useState<string | null>(null);
  const fb = useTriggerFeedback();
  const [errMsg, setErrMsg] = useState<string | null>(null);
  // wf 域文案；run 状态经 chat.status.* 的 key 渲染（原 fmtRunOption 为模块级
  // 函数直读英文 STATUS_LABEL，现移入组件随 hook 取译）。
  const t = useTranslations("wf.inspector");
  const tCommon = useTranslations("wf.common");
  const tDev = useTranslations("wf.dev");
  const tError = useTranslations("wf.error");
  const statusLabel = useRunStatusLabel();
  const fmtRunOption = (r: WorkflowRun): string => {
    const d = new Date(r.started * 1000);
    const p = (n: number) => String(n).padStart(2, "0");
    return `${statusLabel(r.status)} · ${r.id.slice(0, 8)} · ${p(d.getMonth() + 1)}-${p(
      d.getDate(),
    )} ${p(d.getHours())}:${p(d.getMinutes())}`;
  };
  // P2: version history drawer (diff + rollback) opened from the v-badge.
  const [versionDrawer, setVersionDrawer] = useState(false);
  // §4.2 doctor: static dataflow lint results + expandable panel.
  const [doctor, setDoctor] = useState<{
    errors: Array<{ rule: string; node_id?: string; message: string }>;
    warnings: Array<{ rule: string; node_id?: string; message: string }>;
  } | null>(null);
  const [doctorOpen, setDoctorOpen] = useState(false);
  // Stability plan P1d: zero-LLM 试运行 of the stored DSL (validate + doctor +
  // compile + reachability; never saves or executes). Complement to the doctor
  // badge: doctor lints dataflow, dry-run proves the graph actually compiles.
  const [dryRun, setDryRun] = useState<{ busy: boolean; result: api.DryRunResult | null }>({
    busy: false,
    result: null,
  });

  // All runs of THIS workflow, newest first (listWorkflowRuns ordering).
  const wfRuns = runs.filter((r) => r.workflow_id === wf.id);
  const activeId = selRunId ?? wfRuns[0]?.id ?? null;

  // Switching workflow resets the selection + any in-flight view.
  useEffect(() => {
    setSelRunId(null);
    setRun(null);
    setEvents([]);
    setSelNode(null);
    setErrMsg(null);
    setDoctorOpen(false);
    setDryRun({ busy: false, result: null });
  }, [wf.id]);

  const runDry = async () => {
    if (!wf.dsl) return;
    setDryRun({ busy: true, result: null });
    try {
      const r = await api.dryRunWorkflow(wf.dsl);
      setDryRun({ busy: false, result: r });
    } catch {
      setDryRun({ busy: false, result: null });
      setErrMsg(t("dryRunRequestFailed"));
    }
  };

  // §4.2: run the dataflow lint whenever the definition/version changes.
  useEffect(() => {
    let alive = true;
    setDoctor(null);
    api
      .doctorWorkflow(wf.id)
      .then((r) => {
        if (alive && r.ok) setDoctor({ errors: r.errors || [], warnings: r.warnings || [] });
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [wf.id, wf.version]);

  // A rollback/propose changes wf.version — the old receipt would vouch for
  // the wrong DSL, so drop it.
  useEffect(() => {
    setDryRun((d) => (d.result ? { busy: false, result: null } : d));
  }, [wf.version]);

  useEffect(() => {
    if (!activeId) {
      setEvents([]);
      return;
    }
    let alive = true;
    let t: ReturnType<typeof setInterval> | undefined;
    const load = async () => {
      try {
        const [ev, allRuns] = await Promise.all([
          api.getWorkflowRunEvents(activeId),
          api.listWorkflowRuns(),
        ]);
        if (!alive) return;
        setEvents(ev.events || []);
        const cur = allRuns.find((r) => r.id === activeId);
        if (cur) {
          setRun(cur);
          if (cur.status !== "running") {
            setBusy(false);
            if (t) clearInterval(t); // terminal → stop polling (don't tick forever)
          }
        }
      } catch {
        /* sidecar down */
      }
    };
    void load();
    t = setInterval(load, 1500);
    return () => {
      alive = false;
      if (t) clearInterval(t);
    };
  }, [activeId]);

  const trigger = async () => {
    fb.start();
    setErrMsg(null);
    setBusy(true);
    setEvents([]);
    try {
      const r = await api.triggerWorkflowRun(
        wf.id,
        Object.keys(ctxOverride).length ? ctxOverride : undefined,
      );
      // json() never throws on HTTP errors — a 4xx body is {detail}, so check
      // `ok`/`run` explicitly instead of trusting the type.
      const body = r as { ok?: boolean; run?: WorkflowRun; detail?: string };
      if (body.ok && body.run) {
        setSelRunId(body.run.id); // selector follows the freshly created run
        setRun(body.run);
        fb.succeed();
      } else {
        setBusy(false); // no run to poll → unlock the button now
        const msg = body.detail || tCommon("triggerFailed");
        setErrMsg(msg);
        fb.fail(msg);
      }
    } catch {
      setBusy(false); // sidecar down → don't leave the button dead
      const msg = tCommon("runtimeUnreachable");
      setErrMsg(msg);
      fb.fail(msg);
    }
  };

  const openDevSession = async () => {
    // Bind the session to this workflow so every turn injects the current DSL
    // (docs/workflow-dsl-design.md §8.3). The agent proposes via
    // workflow_propose_edit → diff card.
    const s = await g.newSession("workflow-dev", {
      title: tDev("sessionTitle", { name: wf.name }),
      workflow_id: wf.id,
    });
    router.push("/");
    return s;
  };

  const nodeStatus: Record<string, string> = {};
  for (const s of run?.steps || []) nodeStatus[s.id] = s.status;

  const filteredEvents = selNode ? events.filter((e) => e.node_id === selNode) : events;

  // §4.5 per-node telemetry from the event stream: latency (node_enter→node_exit)
  // and token usage (node_exit.usage), keyed by node id.
  const nodeStats = useMemo(() => {
    const enter: Record<string, number> = {};
    const stats: Record<string, { latencyMs?: number; tokens?: number }> = {};
    for (const e of events) {
      const nid = e.node_id;
      if (!nid) continue;
      if (e.kind === "node_enter" && typeof e.ts === "number") enter[nid] = e.ts;
      if (e.kind === "node_exit" && typeof e.ts === "number") {
        const s = (stats[nid] ??= {});
        if (enter[nid] !== undefined) s.latencyMs = Math.max(0, (e.ts - enter[nid]) * 1000);
        const u = e.usage;
        if (u) s.tokens = (s.tokens || 0) + (u.input_tokens || 0) + (u.output_tokens || 0);
      }
    }
    return stats;
  }, [events]);

  // P3: {{context.x}} placeholders still unfilled (no override + empty initial)
  // — the 运行 button switches to a yellow outline so the gap is visible, but
  // running stays allowed (empty strings are a legitimate input).
  const unfilledVars = (() => {
    const keys = new Set<string>();
    try {
      for (const m of JSON.stringify(wf.dsl ?? {}).matchAll(/\{\{\s*context\.([a-zA-Z0-9_]+)\s*\}\}/g)) {
        keys.add(m[1]);
      }
    } catch {
      return [] as string[];
    }
    const initial =
      ((wf.dsl as { context?: { initial?: Record<string, unknown> } } | undefined)?.context?.initial) || {};
    const empty = (v: unknown) => v === undefined || v === null || v === "";
    return [...keys].filter((k) => empty(ctxOverride[k]) && empty(initial[k]));
  })();

  return (
    <div className="space-y-3">
      <ContextEditor dsl={wf.dsl as never} onChange={setCtxOverride} />

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium text-txt">{t("executionGraph")}</span>
        {/* P2: current DSL version — click to open history (diff + rollback) */}
        <button
          onClick={() => setVersionDrawer(true)}
          title={t("versionHistoryTitle")}
          className="btn-press flex items-center gap-1 rounded border border-line2 px-1.5 py-0.5 text-[10px] text-faint hover:text-muted"
        >
          v{wf.version ?? 1} <ChevronDown className="h-3 w-3" />
        </button>
        {/* §4.2 doctor badge: red = errors, yellow = warnings only. */}
        {doctor && (doctor.errors.length > 0 || doctor.warnings.length > 0) && (
          <button
            onClick={() => setDoctorOpen((o) => !o)}
            title={t("doctorTitle")}
            className={`btn-press flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] ${
              doctor.errors.length > 0
                ? "border-red/40 text-red hover:bg-red/10"
                : "border-yellow/40 text-yellow hover:bg-yellow/10"
            }`}
          >
            <ShieldAlert className="h-3 w-3" />
            {doctor.errors.length > 0 ? doctor.errors.length : doctor.warnings.length}
          </button>
        )}
        {selNode && (
          <button
            onClick={() => setSelNode(null)}
            className="btn-press rounded border border-line2 px-1.5 py-0.5 text-[10px] text-faint hover:text-muted"
          >
            {t("clearNodeFilter", { node: selNode })}
          </button>
        )}
        <button
          onClick={openDevSession}
          title={t("devSessionTitleAttr")}
          className="btn-press rounded-md border border-line2 px-2.5 py-1 text-xs text-muted hover:text-txt"
        >
          {t("devSession")}
        </button>
        {/* P1d: zero-LLM preflight of the stored DSL (never saves/executes). */}
        {wf.dsl && (
          <button
            onClick={runDry}
            disabled={dryRun.busy}
            title={t("dryRunTitle")}
            className="btn-press flex items-center gap-1 rounded-md border border-line2 px-2.5 py-1 text-xs text-muted hover:text-txt disabled:opacity-50"
          >
            {dryRun.busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <FlaskConical className="h-3 w-3" />}
            {dryRun.busy ? t("dryRunning") : t("dryRun")}
          </button>
        )}
        {/* Historical-run selector: the latest run is the default; any older
            (e.g. failed) run can be opened for inspection. */}
        {wfRuns.length > 0 && (
          <select
            value={selRunId ?? ""}
            onChange={(e) => setSelRunId(e.target.value || null)}
            title={t("selectRunTitle")}
            className="rounded-md border border-line2 bg-card px-1.5 py-1 text-[11px] text-muted outline-none hover:text-txt"
          >
            <option value="">{t("latestRun", { count: wfRuns.length })}</option>
            {wfRuns.map((r) => (
              <option key={r.id} value={r.id}>
                {fmtRunOption(r)}
              </option>
            ))}
          </select>
        )}
        <button
          onClick={trigger}
          disabled={busy || fb.phase === "busy"}
          title={unfilledVars.length ? t("unfilledTitle", { count: unfilledVars.length, list: unfilledVars.join(", ") }) : t("runTitle")}
          className={`btn-press ml-auto flex items-center gap-1.5 rounded-md px-3 py-1 text-xs font-medium disabled:opacity-50 ${
            unfilledVars.length
              ? "border border-yellow/60 bg-yellow/10 text-yellow hover:bg-yellow/20"
              : "bg-violet text-white hover:opacity-90"
          } ${fb.animClass}`}
        >
          {busy || fb.phase === "busy" ? (
            <>
              <Loader2 className="h-3 w-3 animate-spin" /> {t("running")}
            </>
          ) : (
            <>
              <Play className="h-3 w-3" /> {t("run")}
            </>
          )}
        </button>
      </div>
      {errMsg && <div className="rounded-md border border-red/30 bg-red/[0.06] px-2 py-1.5 text-[11px] text-red">{errMsg}</div>}

      {/* §4.2 doctor panel: expanded findings with a one-click dev-session fix. */}
      {doctorOpen && doctor && (doctor.errors.length > 0 || doctor.warnings.length > 0) && (
        <div className="space-y-1.5 rounded-lg border border-line bg-base/30 p-2.5">
          <div className="flex items-center gap-1.5">
            <ShieldAlert className="h-3.5 w-3.5 text-muted" />
            <span className="text-xs font-medium text-txt">{t("doctorPanel")}</span>
            <button onClick={() => setDoctorOpen(false)} className="ml-auto text-[10px] text-faint hover:text-muted">
              {tCommon("collapseUp")}
            </button>
          </div>
          {doctor.errors.map((e, i) => (
            <div key={`e${i}`} className="flex items-start gap-1.5 text-[11px]">
              <X className="mt-0.5 h-3 w-3 shrink-0 text-red" />
              <span className="text-red">{e.message}</span>
            </div>
          ))}
          {doctor.warnings.map((w, i) => (
            <div key={`w${i}`} className="flex items-start gap-1.5 text-[11px]">
              <ShieldAlert className="mt-0.5 h-3 w-3 shrink-0 text-yellow" />
              <span className="text-yellow">{w.message}</span>
            </div>
          ))}
          {doctor.errors.length > 0 && (
            <button
              onClick={openDevSession}
              className="btn-press mt-1 flex items-center gap-1 rounded-md border border-violet/40 px-2 py-1 text-[11px] text-violet hover:bg-violet/[0.06]"
            >
              {t("oneClickUpgrade")}
            </button>
          )}
        </div>
      )}

      {/* P1d dry-run receipt for the stored DSL (vouches for wf.version). */}
      {dryRun.result && (
        dryRun.result.ok ? (
          <div className="space-y-0.5 rounded-lg border border-green/30 bg-green/[0.06] px-2.5 py-2 text-[11px] text-green">
            <div className="flex items-center gap-1.5">
              <Check className="h-3.5 w-3.5 shrink-0" />
              <span>
                {t("dryRunPassed", { version: wf.version ?? 1, count: dryRun.result.node_count ?? 0 })}
              </span>
              <button
                onClick={() => setDryRun({ busy: false, result: null })}
                className="ml-auto text-[10px] text-green/70 hover:text-green"
              >
                {tCommon("collapseUp")}
              </button>
            </div>
            {dryRun.result.warnings.length > 0 && (
              <div className="pl-5 text-yellow">
                {t("warningsLine", {
                  count: dryRun.result.warnings.length,
                  list: dryRun.result.warnings.map((w) => w.message).join("; "),
                })}
              </div>
            )}
            {dryRun.result.unreachable.length > 0 && (
              <div className="pl-5 text-yellow">
                {tCommon("unreachable", { list: dryRun.result.unreachable.join(", ") })}
              </div>
            )}
          </div>
        ) : (
          <div className="max-h-32 space-y-0.5 overflow-y-auto rounded-lg border border-red/30 bg-red/[0.06] px-2.5 py-2 text-[11px] text-red">
            <div className="flex items-center gap-1.5">
              <X className="h-3.5 w-3.5 shrink-0" />
              <span>{t("dryRunFailed", { version: wf.version ?? 1 })}</span>
              <button
                onClick={() => setDryRun({ busy: false, result: null })}
                className="ml-auto text-[10px] text-red/70 hover:text-red"
              >
                {tCommon("collapseUp")}
              </button>
            </div>
            {dryRun.result.errors.map((e, i) => (
              <div key={`e${i}`} className="pl-5">· {e}</div>
            ))}
            {dryRun.result.doctor_errors.map((e, i) => (
              <div key={`d${i}`} className="pl-5">· {e.message}</div>
            ))}
          </div>
        )
      )}

      <WorkflowDag
        dsl={wf.dsl as never}
        status={nodeStatus}
        selected={selNode}
        onSelect={setSelNode}
      />

      {run && (
        <div className="text-[11px] text-faint">
          {t("runPrefix")} <span className="font-mono text-muted">{run.id.slice(0, 8)}</span> · v
          {run.dsl_version ?? "?"} ·{" "}
          <span style={{ color: STEP_COLOR[run.status] || "rgb(var(--muted))" }}>
            {statusLabel(run.status)}
          </span>
        </div>
      )}

      {run && (
        <RunErrorBox
          run={run}
          onRetryFromCheckpoint={async () => {
            try {
              const r = await api.retryWorkflowRunFromCheckpoint(run.id);
              const body = r as { ok?: boolean; detail?: string; run?: WorkflowRun };
              if (body.ok && body.run) {
                setSelRunId(body.run.id);
                setRun(body.run);
                setBusy(true);
                setEvents([]);
                return undefined;
              }
              return { ok: false, detail: body.detail || tError("resumeFailed") };
            } catch {
              return { ok: false, detail: tCommon("runtimeUnreachable") };
            }
          }}
        />
      )}

      {run && (
        <div className="space-y-1">
          <div className="text-xs font-medium text-txt">{t("steps")}</div>
          <div className="space-y-0.5">
            {run.steps.map((s) => {
              const st = nodeStats[s.id] || {};
              return (
                <button
                  key={s.id}
                  onClick={() => setSelNode((n) => (n === s.id ? null : s.id))}
                  className={`flex w-full items-center gap-2 rounded px-1 py-0.5 text-left text-[11px] transition-colors hover:bg-card2/50 ${
                    selNode === s.id ? "bg-card2/60" : ""
                  }`}
                >
                  <span
                    className="h-1.5 w-1.5 shrink-0 rounded-full"
                    style={{ background: STEP_COLOR[s.status] || "rgb(var(--faint))" }}
                  />
                  <span style={{ color: STEP_COLOR[s.status] || "rgb(var(--muted))" }}>
                    {statusLabel(s.status)}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-txt">{s.title || s.id}</span>
                  {st.latencyMs !== undefined && (
                    <span className="shrink-0 tabular-nums text-faint">
                      {st.latencyMs >= 1000 ? `${(st.latencyMs / 1000).toFixed(1)}s` : `${Math.round(st.latencyMs)}ms`}
                    </span>
                  )}
                  {st.tokens !== undefined && st.tokens > 0 && (
                    <span className="shrink-0 tabular-nums text-faint" title={tCommon("tokensTitle")}>
                      {st.tokens >= 1000 ? `${(st.tokens / 1000).toFixed(1)}K` : st.tokens}↑
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      )}

      <div className="space-y-1">
        <div className="text-xs font-medium text-txt">
          {t("executionLog")}
          {selNode ? tCommon("nodeSuffix", { node: selNode }) : ""}
        </div>
        <WorkflowLogTimeline events={filteredEvents} filters />
      </div>

      {/* Supervisor interventions of the inspected run (workflow-ux-redesign
          P2): the deterministic decider (coerce/patch_dsl/abort) already runs
          inside the engine — this surfaces what it did instead of a stub. */}
      <div className="space-y-1.5 rounded-lg border border-line bg-base/30 p-2.5">
        <div className="flex items-center gap-1.5">
          <Shield className="h-3.5 w-3.5 text-muted" />
          <span className="text-xs font-medium text-txt">{t("supervisor")}</span>
          <span className="ml-auto text-[10px] text-faint">{t("autoMode")}</span>
        </div>
        {(() => {
          const supEvents = events.filter((e) => e.kind === "supervisor_intervene");
          if (!supEvents.length) {
            return <div className="text-[11px] text-faint">{t("noInterventions")}</div>;
          }
          return (
            <div className="space-y-1.5">
              <div className="text-[10px] text-faint">{t("interventionsCount", { count: supEvents.length })}</div>
              {supEvents.map((e, i) => {
                const action = String(e.action ?? "?");
                const ok = action === "coerce" || action === "patch_dsl";
                const errs = Array.isArray(e.errors) ? (e.errors as unknown[]) : [];
                return (
                  <div key={i} className="rounded border border-line bg-card p-2 text-[11px]">
                    <div className="flex items-center gap-1.5">
                      {ok ? (
                        <Check className="h-3 w-3 text-green" />
                      ) : action === "abort" ? (
                        <X className="h-3 w-3 text-red" />
                      ) : (
                        <ShieldAlert className="h-3 w-3 text-orange" />
                      )}
                      <span className={ok ? "text-green" : action === "abort" ? "text-red" : "text-orange"}>
                        {action}
                      </span>
                      {e.node_id && <span className="font-mono text-faint">· {e.node_id}</span>}
                    </div>
                    {errs.length > 0 && (
                      <div className="mt-0.5 text-faint">
                        {tCommon("validationErrors", { list: errs.map(String).join("; ") })}
                      </div>
                    )}
                    {typeof e.reason === "string" && e.reason && (
                      <div className="mt-0.5 text-muted">{e.reason}</div>
                    )}
                  </div>
                );
              })}
            </div>
          );
        })()}
      </div>

      {versionDrawer && (
        <VersionHistoryDrawer
          workflowId={wf.id}
          currentVersion={wf.version ?? 1}
          onClose={() => setVersionDrawer(false)}
          onRolledBack={() => g.reloadWorkflows()}
        />
      )}
    </div>
  );
}
