"use client";

import { useCallback, useEffect, useState } from "react";
import { Check, Loader2, RotateCcw, TrendingUp, X } from "lucide-react";
import * as api from "@/lib/runtime";
import type { SynthesisCaseSummary } from "@/lib/runtime";

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
        <h2 className="text-lg font-semibold text-txt">总结质量</h2>
        <div className="ml-auto flex items-center gap-2">
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="rounded-md border border-line2 bg-card px-2 py-1 text-xs text-muted outline-none"
          >
            <option value={7}>最近 7 天</option>
            <option value={30}>最近 30 天</option>
            <option value={90}>最近 90 天</option>
          </select>
          <button
            onClick={() => void load()}
            className="btn-press flex items-center gap-1 rounded-md border border-line2 px-2 py-1 text-xs text-muted hover:text-txt"
          >
            {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RotateCcw className="h-3 w-3" />}
            刷新
          </button>
        </div>
      </div>
      <p className="mt-1 text-sm text-muted">
        「总结成流程」的成功率与准确率漏斗。成功率 = 首跑完成 / 触发总数；准确率依赖案例回放与反馈。
      </p>

      {/* funnel metric cards */}
      {stats && (
        <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
          <MetricCard label="L1 生成成功" value={`${pct(stats.l1_generated, stats.total)}%`} sub={`${stats.l1_generated}/${stats.total} 次`} color="#a78bfa" />
          <MetricCard label="L2 被采用" value={`${pct(stats.l2_adopted, stats.l1_generated)}%`} sub={`${stats.l2_adopted}/${stats.l1_generated} 创建`} color="#60a5fa" />
          <MetricCard label="L3 首跑完成" value={`${pct(stats.l3_first_run_done, stats.l2_adopted)}%`} sub={`${stats.l3_first_run_done}/${stats.l2_adopted} 跑通`} color="#4ade80" />
          <MetricCard
            label="平均草稿改动"
            value={stats.avg_edit_distance === null ? "—" : String(stats.avg_edit_distance)}
            sub="edit_distance 均值"
            color="#f59e0b"
          />
        </div>
      )}

      {/* top failure labels */}
      {stats && stats.top_fail_labels.length > 0 && (
        <div className="mt-4">
          <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-faint">Top 失败标签</div>
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
          <span className="text-xs font-medium text-txt">案例（{visible.length}）</span>
          <label className="flex items-center gap-1.5 text-[11px] text-muted">
            <input type="checkbox" checked={onlyFailed} onChange={(e) => setOnlyFailed(e.target.checked)} className="accent-violet" />
            仅看失败 / 未跑通
          </label>
        </div>
        <div className="space-y-1">
          {visible.map((c) => (
            <CaseRow key={c.synthesis_id} c={c} onOpen={() => setOpenCase(c.synthesis_id)} />
          ))}
          {visible.length === 0 && !loading && (
            <div className="rounded-lg border border-dashed border-line p-6 text-center text-xs text-faint">
              暂无记录。在聊天页点「总结成流程」后会自动积累案例。
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
  const failed = c.status !== "ok";
  const runFailed = c.outcome?.first_run && c.outcome.first_run.status === "failed";
  const adopted = c.outcome?.created;
  const icon = failed ? <X className="h-3.5 w-3.5 text-red" /> : runFailed ? <X className="h-3.5 w-3.5 text-red" /> : adopted ? <Check className="h-3.5 w-3.5 text-green" /> : <Check className="h-3.5 w-3.5 text-faint" />;
  const when = c.ts ? new Date(c.ts * 1000).toLocaleString(undefined, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "";
  const label = failed
    ? c.fail_stage || "生成失败"
    : runFailed
      ? `首跑失败 @ ${c.outcome?.first_run?.failed_node || "?"}`
      : adopted
        ? "已采用 · 首跑成功"
        : "已生成";
  return (
    <button
      onClick={onOpen}
      className="flex w-full items-center gap-2.5 rounded-md border border-line bg-card px-3 py-2 text-left transition-colors hover:border-line2 hover:bg-card2/40"
    >
      {icon}
      <span className="min-w-0 flex-1 truncate text-xs text-txt">{label}</span>
      {c.session_stats?.messages !== undefined && (
        <span className="shrink-0 text-[10px] text-faint">{c.session_stats.messages} 消息</span>
      )}
      {c.prompt_version && <span className="shrink-0 rounded border border-line px-1.5 text-[10px] font-mono text-faint">{c.prompt_version}</span>}
      <span className="shrink-0 text-[10px] text-faint">{when}</span>
    </button>
  );
}

function SynthesisCaseDrawer({
  synthesisId,
  onClose,
  onReplayed,
}: {
  synthesisId: string;
  onClose: () => void;
  onReplayed: () => void;
}) {
  const [detail, setDetail] = useState<Awaited<ReturnType<typeof api.getSynthesisCase>>["case"] | null>(null);
  const [replaying, setReplaying] = useState(false);
  const [replayMsg, setReplayMsg] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .getSynthesisCase(synthesisId)
      .then((r) => alive && r.ok && setDetail(r.case))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [synthesisId]);

  const replay = async () => {
    setReplaying(true);
    setReplayMsg(null);
    try {
      const r = await api.replaySynthesis(synthesisId);
      if (r.ok) {
        setReplayMsg(`重放成功（${r.attempts_used} 次尝试，${r.prompt_version}）`);
        onReplayed();
      } else {
        setReplayMsg(`重放失败：${r.fail_stage || (r.errors || []).join("; ") || "未知"}`);
      }
    } catch {
      setReplayMsg("重放失败：无法连接运行时");
    } finally {
      setReplaying(false);
    }
  };

  return (
    <div className="fixed inset-0 z-40" onClick={onClose}>
      <div
        className="absolute right-0 top-0 flex h-full w-96 flex-col border-l border-line bg-panel shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-line px-4 py-3">
          <span className="text-sm font-semibold text-txt">案例详情</span>
          <span className="truncate font-mono text-[10px] text-faint">{synthesisId}</span>
          <button onClick={onClose} className="ml-auto rounded p-1 text-faint hover:bg-card2 hover:text-txt" aria-label="关闭">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
          {!detail && <div className="py-6 text-center text-xs text-faint">加载中…</div>}
          {detail && (
            <>
              <div className="rounded-lg border border-line bg-base/30 p-2.5 text-[11px]">
                <div className="text-faint">
                  状态：<span className={detail.output?.status === "ok" ? "text-green" : "text-red"}>{detail.output?.status}</span>
                  {detail.output?.fail_stage && <> · {detail.output.fail_stage}</>}
                </div>
                <div className="mt-0.5 text-faint">
                  提示词版本：{detail.input?.prompt_version} · 尝试 {detail.output?.attempts_used} 次
                </div>
              </div>

              {(detail.attempts || []).length > 0 && (
                <div>
                  <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-faint">各轮尝试</div>
                  <div className="space-y-1">
                    {(detail.attempts || []).map((a, i) => (
                      <div key={i} className="rounded-md border border-line bg-card px-2 py-1.5 text-[11px]">
                        <span className={a.validate_errors.length ? "text-red" : "text-green"}>
                          第 {a.attempt} 轮 · {a.parse}
                        </span>
                        <span className="ml-2 text-faint">{a.latency_ms}ms</span>
                        {a.validate_errors.length > 0 && (
                          <div className="mt-0.5 text-[10px] text-red">{a.validate_errors.join("；")}</div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {detail.input?.trace && (
                <div>
                  <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-faint">Trace（会话摘要）</div>
                  <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-all rounded-md border border-line bg-base/50 p-2 font-mono text-[10px] leading-relaxed text-muted">
                    {detail.input.trace}
                  </pre>
                </div>
              )}
            </>
          )}
        </div>
        <div className="border-t border-line p-3">
          {replayMsg && <div className="mb-2 text-[11px] text-muted">{replayMsg}</div>}
          <button
            onClick={() => void replay()}
            disabled={replaying || !detail}
            className="btn-press flex w-full items-center justify-center gap-1.5 rounded-md bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            {replaying ? <Loader2 className="h-3 w-3 animate-spin" /> : <RotateCcw className="h-3 w-3" />}
            {replaying ? "重放中…" : "用当前提示词重新总结（离线重放）"}
          </button>
        </div>
      </div>
    </div>
  );
}
