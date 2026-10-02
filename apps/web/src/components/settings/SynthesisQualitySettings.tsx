"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Check, Loader2, RotateCcw, TrendingUp, X } from "lucide-react";
import * as api from "@/lib/runtime";
import type { SynthesisCaseSummary } from "@/lib/runtime";
import { SynthesisCaseDrawer } from "@/components/right/SynthesisCaseDrawer";

/** Settings → 总结质量 (quality-plan §3.2/§3.4): funnel metrics over recorded
 *  synthesis cases, a filterable case list, and a detail drawer with one-click
 *  replay (offline re-synthesis on the stored trace — the eval-harness MVP). */
export function SynthesisQualitySettings() {
  const [days, setDays] = useState(30);
  const [stats, setStats] = useState<Awaited<ReturnType<typeof api.getSynthesisStats>> | null>(null);
  const [cases, setCases] = useState<SynthesisCaseSummary[]>([]);
  const [onlyFailed, setOnlyFailed] = useState(false);
  const [openCase, setOpenCase] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [s, c] = await Promise.all([api.getSynthesisStats(days), api.listSynthesisCases(300)]);
      if (s.ok) setStats(s);
      setCases(c.cases || []);
    } catch {
      /* sidecar down */
    } finally {
      setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    void load();
  }, [load]);

  const visible = cases.filter((c) => {
    if (!onlyFailed) return true;
    return c.status !== "ok" || (c.outcome?.first_run && c.outcome.first_run.status === "failed");
  });

  const pct = (n: number, d: number) => (d > 0 ? Math.round((n / d) * 100) : 0);

  return (
    <div className="px-8 py-7">
      <div className="flex items-center gap-2">
        <TrendingUp className="h-5 w-5 text-violet" />
        <h2 className="text-lg font-semibold text-txt">Synthesis Quality</h2>
        <div className="ml-auto flex items-center gap-2">
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="rounded-md border border-line2 bg-card px-2 py-1 text-xs text-muted outline-none"
          >
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </select>
          <button
            onClick={() => void load()}
            className="btn-press flex items-center gap-1 rounded-md border border-line2 px-2 py-1 text-xs text-muted hover:text-txt"
          >
            {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RotateCcw className="h-3 w-3" />}
            Refresh
          </button>
        </div>
      </div>
      <p className="mt-1 text-sm text-muted">
        Success-rate and accuracy funnel for &quot;synthesize into a workflow&quot;. Success rate =
        first runs completed / total triggered; accuracy relies on case replay and feedback.
      </p>

      {/* funnel metric cards */}
      {stats && (
        <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
          <MetricCard label="L1 generated" value={`${pct(stats.l1_generated, stats.total)}%`} sub={`${stats.l1_generated}/${stats.total} runs`} color="#a78bfa" />
          <MetricCard label="L2 adopted" value={`${pct(stats.l2_adopted, stats.l1_generated)}%`} sub={`${stats.l2_adopted}/${stats.l1_generated} created`} color="#60a5fa" />
          <MetricCard label="L3 first run done" value={`${pct(stats.l3_first_run_done, stats.l2_adopted)}%`} sub={`${stats.l3_first_run_done}/${stats.l2_adopted} completed`} color="#4ade80" />
          <MetricCard
            label="Avg draft edits"
            value={stats.avg_edit_distance === null ? "—" : String(stats.avg_edit_distance)}
            sub="edit_distance mean"
            color="#f59e0b"
          />
        </div>
      )}

      {/* top failure labels */}
      {stats && stats.top_fail_labels.length > 0 && (
        <div className="mt-4">
          <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-faint">Top failure labels</div>
          <div className="flex flex-wrap gap-1.5">
            {stats.top_fail_labels.map((f) => (
              <span key={f.label} className="rounded-full border border-line px-2 py-0.5 font-mono text-[10px] text-faint">
                {f.label} <span className="text-muted">×{f.count}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {/* case list */}
      <div className="mt-5">
        <div className="mb-2 flex items-center gap-3">
          <span className="text-xs font-medium text-txt">Cases ({visible.length})</span>
          <label className="flex items-center gap-1.5 text-[11px] text-muted">
            <input type="checkbox" checked={onlyFailed} onChange={(e) => setOnlyFailed(e.target.checked)} className="accent-violet" />
            Failed / incomplete only
          </label>
        </div>
        <div className="space-y-1">
          {visible.map((c) => (
            <CaseRow key={c.synthesis_id} c={c} onOpen={() => setOpenCase(c.synthesis_id)} />
          ))}
          {visible.length === 0 && !loading && (
            <div className="rounded-lg border border-dashed border-line p-6 text-center text-xs text-faint">
              No records yet. Cases accumulate automatically after you click &quot;synthesize into
              a workflow&quot; in a chat page.
            </div>
          )}
        </div>
      </div>

      {openCase && <SynthesisCaseDrawer synthesisId={openCase} onClose={() => setOpenCase(null)} onReplayed={load} />}
    </div>
  );
}

function MetricCard({ label, value, sub, color }: { label: string; value: string; sub: string; color: string }) {
  return (
    <div className="rounded-lg border border-line bg-card p-3 text-center">
      <div className="text-2xl font-bold" style={{ color }}>
        {value}
      </div>
      <div className="mt-0.5 text-[11px] font-medium text-txt">{label}</div>
      <div className="text-[10px] text-faint">{sub}</div>
    </div>
  );
}

function CaseRow({ c, onOpen }: { c: SynthesisCaseSummary; onOpen: () => void }) {
  const runFailed = c.outcome?.first_run && c.outcome.first_run.status === "failed";
  const adopted = c.outcome?.created;
  let icon: ReactNode;
  let label: string;
  if (!c.status) {
    // No output.json yet: either the in-process task is alive (进行中) or the
    // runtime exited before it finished (未完成 — trace preserved on disk).
    if (c.running) {
      icon = <Loader2 className="h-3.5 w-3.5 animate-spin text-blue" />;
      label = "In progress";
    } else {
      icon = <X className="h-3.5 w-3.5 text-yellow" />;
      label = "Incomplete";
    }
  } else if (c.status !== "ok") {
    icon = <X className="h-3.5 w-3.5 text-red" />;
    label = c.fail_stage || "Generation failed";
  } else if (runFailed) {
    icon = <X className="h-3.5 w-3.5 text-red" />;
    label = `First run failed @ ${c.outcome?.first_run?.failed_node || "?"}`;
  } else {
    icon = <Check className={`h-3.5 w-3.5 ${adopted ? "text-green" : "text-faint"}`} />;
    label = adopted ? "Adopted · first run succeeded" : "Generated";
  }
  const when = c.ts ? new Date(c.ts * 1000).toLocaleString(undefined, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "";
  return (
    <button
      onClick={onOpen}
      className="flex w-full items-center gap-2.5 rounded-md border border-line bg-card px-3 py-2 text-left transition-colors hover:border-line2 hover:bg-card2/40"
    >
      {icon}
      <span className="min-w-0 flex-1 truncate text-xs text-txt">{label}</span>
      {c.session_stats?.messages !== undefined && (
        <span className="shrink-0 text-[10px] text-faint">{c.session_stats.messages} messages</span>
      )}
      {c.prompt_version && <span className="shrink-0 rounded border border-line px-1.5 text-[10px] font-mono text-faint">{c.prompt_version}</span>}
      <span className="shrink-0 text-[10px] text-faint">{when}</span>
    </button>
  );
}
