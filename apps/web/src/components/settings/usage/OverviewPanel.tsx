"use client";

/** 概览：KPI → 两小时点格图 → 来源/Provider/模型分布。
 * 页签内的「统计窗口」只影响本页（评审决议：时间控制不跨页签）。
 * 时间序列视图已由 GridPanel 接管（设计见 usage-cadence-design.md）。 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { sourceFg } from "@/lib/sourceColor";
import { RefreshCw } from "lucide-react";
import * as api from "@/lib/runtime";
import type { UsageOverview, UsageSessionRow } from "@/lib/types";
import { fmt, pct, providerColor, SERIES, TipRow, useTip } from "./charts";
import { GridPanel } from "./GridPanel";

/* ---- V2 P1-1: money views ------------------------------------------------
 * The runtime adds `est_cost` / `cache_saving` to every aggregate row and
 * `pricing_configured` at the top level, but lib/types.ts is frozen for this
 * change, so read those fields through these narrow helpers instead of
 * widening the shared contract here. Absent/NaN reads collapse to 0, which is
 * the same value the backend returns when no pricing table is configured. */
type CostFields = { est_cost?: number; cache_saving?: number };

function costOf(row: unknown): number {
  const v = (row as CostFields | null | undefined)?.est_cost;
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

function savingOf(row: unknown): number {
  const v = (row as CostFields | null | undefined)?.cache_saving;
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

function pricingConfigured(ov: UsageOverview): boolean {
  return Boolean((ov as UsageOverview & { pricing_configured?: boolean }).pricing_configured);
}

/** CNY with enough precision to keep sub-fen estimates from reading as zero
 *  (a single short turn can cost less than ¥0.01). */
function fmtCny(n: number): string {
  const v = Math.max(0, n || 0);
  const digits = v === 0 ? 2 : v < 0.01 ? 4 : v < 1 ? 3 : 2;
  return `¥${v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

/** Shown inline when `pricing_configured` is false. Deliberately not i18n —
 *  it is the literal JSON shape the runtime parses. */
const PRICING_EXAMPLE =
  '{"pricing": {"<provider>/<model>": {"input": 2, "output": 8, "cache_read": 0.2}}}';

function Delta({ v, suffix = "", goodUp = true }: { v: number; suffix?: string; goodUp?: boolean }) {
  const up = v >= 0;
  const good = up === goodUp;
  return (
    <span style={{ color: good ? "#4ade80" : "#f87171" }}>
      {up ? "▲" : "▼"} {Math.abs(v) < 10 ? Math.abs(v).toFixed(1) : Math.round(Math.abs(v))}
      {suffix}
    </span>
  );
}

function Kpi({ label, value, sub }: { label: string; value: React.ReactNode; sub: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-line bg-card px-4 py-3.5">
      <div className="text-xs text-muted">{label}</div>
      <div className="mt-1.5 text-2xl font-semibold tracking-tight tabular-nums">{value}</div>
      <div className="mt-1 text-[11.5px] text-faint">{sub}</div>
    </div>
  );
}

export function OverviewPanel() {
  const t = useTranslations("settings.usage.overview");
  const [range, setRange] = useState(30);
  const [ov, setOv] = useState<UsageOverview | null>(null);
  const [topSessions, setTopSessions] = useState<UsageSessionRow[]>([]);
  const [err, setErr] = useState("");
  const alive = useRef(true);

  const load = useCallback(async (days: number) => {
    try {
      const o = await api.getUsageOverview(days);
      if (!alive.current) return;
      setOv(o);
      setErr("");
      // Top-session card shares the overview window so the two cards agree.
      try {
        const s = await api.getUsageSessions({
          from: o.window.from,
          to: o.window.to,
          sort: "total",
          limit: 10,
        });
        if (alive.current) setTopSessions(s.sessions || []);
      } catch {
        if (alive.current) setTopSessions([]);
      }
    } catch {
      if (alive.current) setErr(t("loadFailed"));
    }
  }, [t]);

  useEffect(() => {
    alive.current = true;
    load(range);
    const timer = setInterval(() => load(range), 60_000); // 页面可见时的轻轮询
    return () => {
      alive.current = false;
      clearInterval(timer);
    };
  }, [range, load]);

  if (err) return <div className="py-10 text-center text-sm text-faint">{err}</div>;
  if (!ov) return <div className="py-10 text-center text-sm text-faint">{t("loading")}</div>;

  const today = ov.today;
  const daily = ov.daily;
  const y = daily.length > 1 ? daily[daily.length - 2] : null;
  const dTokens = y && y.input_tokens + y.output_tokens > 0
    ? ((today.input_tokens + today.output_tokens) - (y.input_tokens + y.output_tokens)) / (y.input_tokens + y.output_tokens) * 100
    : 0;
  const dCalls = y ? today.calls - y.calls : 0;
  const dHit = y && y.input_tokens > 0 && today.input_tokens > 0
    ? (today.cache_hit_ratio - y.cache_hit_ratio) * 100
    : 0;
  const empty = ov.totals.calls === 0 && today.calls === 0;

  return (
    <div>
      <div className="mb-3 flex items-center gap-2.5">
        <span className="text-[11.5px] text-faint">{t("windowNote")}</span>
        <div className="flex-1" />
        <div className="flex rounded-lg border border-line bg-card p-0.5">
          {[7, 30, 90].map((d) => (
            <button
              key={d}
              onClick={() => setRange(d)}
              className={`rounded-md px-3 py-1 text-xs transition-colors ${range === d ? "bg-card2 text-txt" : "text-muted hover:text-txt"}`}
            >
              {t("lastN", { days: d })}
            </button>
          ))}
        </div>
        <button
          className="flex h-7 w-7 items-center justify-center rounded-lg border border-line text-muted hover:bg-card"
          title={t("refresh")}
          onClick={() => load(range)}
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* V2 P1-1: money card sits above the token KPIs — it is the answer the
          user opens this page for once prices are configured. */}
      <CostCard ov={ov} days={ov.window.days} />

      {empty ? (
        <div className="mt-3 rounded-xl border border-line bg-card px-6 py-14 text-center text-sm text-faint">
          {t("empty")}
        </div>
      ) : (
        <>
          {/* KPI */}
          <div className="mt-3 grid grid-cols-2 gap-3 xl:grid-cols-4">
            <Kpi
              label={t("kpiTokensToday")}
              value={fmt(today.input_tokens + today.output_tokens)}
              sub={<>↑ {fmt(today.input_tokens)} · ↓ {fmt(today.output_tokens)} · <Delta v={dTokens} suffix="%" /></>}
            />
            <Kpi label={t("kpiRequestsToday")} value={`${today.calls}`} sub={<>{t("vsYesterday")} <Delta v={dCalls} /></>} />
            <Kpi
              label={t("kpiHitToday")}
              value={<><span style={{ color: "#4ade80" }}>⚡</span> {pct(today.cache_hit_ratio)}</>}
              sub={<>{t("vsYesterday")} {dHit >= 0 ? "+" : ""}{dHit.toFixed(1)} {t("hitPtNote")}</>}
            />
            <Kpi
              label={t("kpiTokensRange", { days: range })}
              value={fmt(ov.totals.input_tokens + ov.totals.output_tokens)}
              sub={t("sessionsProviders", { sessions: ov.sessions_active, providers: ov.providers.length })}
            />
          </div>

          {/* 两小时点格图（替掉原每日趋势 + 24h 分布两张图） */}
          <div className="mt-3">
            {/* GridPanel 自带窗口/口径控件与取数，无需传参 */}
            <GridPanel />
          </div>

          {/* 来源 / Provider / 模型 */}
          <div className="mt-3 grid gap-3 lg:grid-cols-3">
            <SourceDist ov={ov} />
            <ProviderDist ov={ov} />
            <ModelRank ov={ov} />
          </div>

          {/* V2 P1-1：钱花在哪个模型 / 哪个会话上 */}
          <div className="mt-3 grid gap-3 lg:grid-cols-2">
            <ModelCost ov={ov} />
            <TopSessions rows={topSessions} />
          </div>
        </>
      )}
    </div>
  );
}

/** 来源（usage-stats-design §3.6）：对话 vs 工作流 vs 后台任务。
 * labelKey 存 catalog key（settings.usage.overview.source.*，字面量联合）；
 * 颜色统一走 lib/sourceColor（正典表 + 未知来源按名称 hash 从 Ginno 色板
 * 取色——runtime 侧来源不再白名单校验，前端配色随来源自然扩展）。
 * labelKey 存 catalog key（settings.usage.overview.source.*，字面量联合）。 */
type SourceKey =
  | "chat" | "goal" | "compaction" | "workflow" | "memory" | "kb" | "probe" | "external"
  | "subagent" | "subagent_plan" | "subagent_gate" | "title" | "team"
  | "schedule" | "mods" | "classifier" | "channel_resume" | "other";
const SOURCE_META: Record<string, { labelKey: SourceKey }> = {
  chat: { labelKey: "chat" },
  goal: { labelKey: "goal" },
  compaction: { labelKey: "compaction" },
  workflow: { labelKey: "workflow" },
  memory: { labelKey: "memory" },
  kb: { labelKey: "kb" },
  probe: { labelKey: "probe" },
  external: { labelKey: "external" },
  // 子代理 turn（subagent_scheduler.runner 的 usage_source="subagent"）：
  // 2026-10-13 之前不在 usage_store 来源白名单，写入时被强转 "other"
  // ——「来源分布」里凭空多出的『其他』即此。
  subagent: { labelKey: "subagent" },
  // 2026-10-25: 记账补全后端新发射的来源（此前长期显示英文原名 + hash 配色）。
  subagent_plan: { labelKey: "subagent_plan" },
  subagent_gate: { labelKey: "subagent_gate" },
  title: { labelKey: "title" },
  team: { labelKey: "team" },
  schedule: { labelKey: "schedule" },
  mods: { labelKey: "mods" },
  classifier: { labelKey: "classifier" },
  channel_resume: { labelKey: "channel_resume" },
  other: { labelKey: "other" },
};

function SourceDist({ ov }: { ov: UsageOverview }) {
  const t = useTranslations("settings.usage.overview");
  const { show, hide, move, tipEl } = useTip();
  const sources = ov.sources || [];
  const total = sources.reduce((a, s) => a + s.input_tokens + s.output_tokens, 0) || 1;
  const vmax = Math.max(...sources.map((s) => s.input_tokens + s.output_tokens), 1);
  return (
    <div className="min-w-0 overflow-hidden rounded-xl border border-line bg-card px-4 pb-3 pt-3.5">
      <div className="mb-1.5 flex items-baseline gap-3">
        <h3 className="text-[13px] font-semibold">{t("sourceDist")}</h3>
        <span className="text-[11px] text-faint">{t("sourceDistHint")}</span>
      </div>
      {sources.length === 0 && <div className="py-6 text-center text-xs text-faint">{t("noData")}</div>}
      {sources.map((s) => {
        const meta = SOURCE_META[s.source] || { labelKey: null as SourceKey | null };
        const color = sourceFg(s.source);
        const label = meta.labelKey ? t(`source.${meta.labelKey}`) : s.source;
        const v = s.input_tokens + s.output_tokens;
        return (
          <div
            key={s.source}
            className="border-b border-white/5 py-2 last:border-b-0"
            onMouseEnter={(e) =>
              show(
                <>
                  <b className="text-txt">{label}</b> ({s.source}) · {t("tipInWindow")}
                  <TipRow label={t("labelTokens")} value={fmt(v)} />
                  <TipRow label={t("labelInput")} value={fmt(s.input_tokens)} />
                  <TipRow label={t("labelOutput")} value={fmt(s.output_tokens)} />
                  <TipRow label={t("labelCacheRead")} value={fmt(s.cache_read_tokens)} swatch={SERIES.cache} />
                  <TipRow label={t("labelRequests")} value={String(s.calls)} />
                </>,
                e,
              )
            }
            onMouseMove={move}
            onMouseLeave={hide}
          >
            <div className="flex items-center gap-2">
              <span className="flex min-w-0 items-center gap-2 text-[12.5px] text-muted" title={label}>
                <i className="h-2 w-2 flex-none rounded-[2.5px]" style={{ background: color }} />
                <span className="truncate">{label}</span>
              </span>
              <span className="ml-auto flex-none whitespace-nowrap text-xs tabular-nums text-muted">
                <b className="text-txt">{pct(v / total)}</b> · {fmt(v)}
              </span>
            </div>
            <div className="mt-1.5 h-2.5 overflow-hidden rounded-full bg-card2">
              <div className="h-full rounded-full" style={{ width: `${(v / vmax) * 100}%`, background: color }} />
            </div>
          </div>
        );
      })}
      {tipEl}
    </div>
  );
}

function ProviderDist({ ov }: { ov: UsageOverview }) {
  const t = useTranslations("settings.usage.overview");
  const { show, hide, move, tipEl } = useTip();
  const total = ov.providers.reduce((a, p) => a + p.input_tokens + p.output_tokens, 0) || 1;
  const vmax = Math.max(...ov.providers.map((p) => p.input_tokens + p.output_tokens), 1);
  return (
    <div className="min-w-0 overflow-hidden rounded-xl border border-line bg-card px-4 pb-3 pt-3.5">
      <div className="mb-1.5 flex items-baseline gap-3">
        <h3 className="text-[13px] font-semibold">{t("providerDist")}</h3>
        <span className="text-[11px] text-faint">{t("providerDistHint")}</span>
      </div>
      {ov.providers.length === 0 && <div className="py-6 text-center text-xs text-faint">{t("noData")}</div>}
      {ov.providers.map((p) => {
        const v = p.input_tokens + p.output_tokens;
        return (
          <div
            key={p.provider}
            className="border-b border-white/5 py-2 last:border-b-0"
            onMouseEnter={(e) =>
              show(
                <>
                  <b className="text-txt">{p.provider}</b> · {t("tipInWindow")}
                  <TipRow label={t("labelTokens")} value={fmt(v)} />
                  <TipRow label={t("labelInput")} value={fmt(p.input_tokens)} />
                  <TipRow label={t("labelOutput")} value={fmt(p.output_tokens)} />
                  <TipRow label={t("labelCacheRead")} value={fmt(p.cache_read_tokens)} swatch={SERIES.cache} />
                  <TipRow label={t("labelHitRate")} value={p.input_tokens > 0 ? pct(p.cache_hit_ratio) : "—"} />
                  <TipRow label={t("labelRequests")} value={String(p.calls)} />
                </>,
                e,
              )
            }
            onMouseMove={move}
            onMouseLeave={hide}
          >
            <div className="flex items-center gap-2">
              <span className="flex min-w-0 items-center gap-2 text-[12.5px] text-muted" title={p.provider}>
                <i className="h-2 w-2 flex-none rounded-[2.5px]" style={{ background: providerColor(p.provider) }} />
                <span className="truncate">{p.provider}</span>
              </span>
              <span className="ml-auto flex-none whitespace-nowrap text-xs tabular-nums text-muted">
                <b className="text-txt">{pct(v / total)}</b> · {fmt(v)}
              </span>
            </div>
            <div className="mt-1.5 h-2.5 overflow-hidden rounded-full bg-card2">
              <div className="h-full rounded-full" style={{ width: `${(v / vmax) * 100}%`, background: providerColor(p.provider) }} />
            </div>
          </div>
        );
      })}
      {tipEl}
    </div>
  );
}

/** Window-level cost card. With a pricing table configured it leads with the
 *  estimated spend; without one it shows the token total and the exact JSON
 *  shape needed to enable money (never a fabricated ¥0 headline). */
function CostCard({ ov, days }: { ov: UsageOverview; days: number }) {
  const t = useTranslations("settings.usage.overview");
  const priced = pricingConfigured(ov);
  const tokens = ov.totals.input_tokens + ov.totals.output_tokens;
  return (
    <div className="rounded-xl border border-line bg-card px-4 py-3.5">
      <div className="flex flex-wrap items-end gap-x-8 gap-y-3">
        <div>
          <div className="text-xs text-muted">
            {priced ? t("costWindow", { days }) : t("tokensWindow", { days })}
          </div>
          <div className="mt-1.5 text-2xl font-semibold tracking-tight tabular-nums">
            {priced ? fmtCny(costOf(ov.totals)) : fmt(tokens)}
          </div>
          <div className="mt-1 text-[11.5px] text-faint">{t("costCalls", { calls: ov.totals.calls })}</div>
        </div>
        {priced && (
          <div>
            <div className="text-xs text-muted">{t("costSaving")}</div>
            <div className="mt-1.5 text-lg font-semibold tracking-tight tabular-nums" style={{ color: "#4ade80" }}>
              {fmtCny(savingOf(ov.totals))}
            </div>
          </div>
        )}
      </div>
      {!priced && (
        <div className="mt-3 border-t border-line pt-2.5">
          <div className="text-[11.5px] text-muted">{t("pricingHint")}</div>
          <code className="mt-1 block overflow-x-auto whitespace-pre rounded-lg bg-card2 px-2.5 py-1.5 font-mono text-[11px] text-txt">
            {PRICING_EXAMPLE}
          </code>
        </div>
      )}
    </div>
  );
}

/** Spend by model, `est_cost` desc. When every row prices to 0 (no table yet)
 *  the ordering falls back to tokens so the card still ranks usefully. */
function ModelCost({ ov }: { ov: UsageOverview }) {
  const t = useTranslations("settings.usage.overview");
  const priced = pricingConfigured(ov) && ov.models.some((m) => costOf(m) > 0);
  const rows = [...ov.models].sort((a, b) =>
    priced
      ? costOf(b) - costOf(a)
      : b.input_tokens + b.output_tokens - (a.input_tokens + a.output_tokens),
  );
  const heads = ["labelModel", "labelProvider", "labelCalls", "labelTokens", "labelCost"] as const;
  return (
    <div className="min-w-0 overflow-hidden rounded-xl border border-line bg-card px-4 pb-1 pt-3.5">
      <div className="mb-1.5 flex items-baseline gap-3">
        <h3 className="text-[13px] font-semibold">{t("costByModel")}</h3>
        <span className="text-[11px] text-faint">{priced ? t("costByModelHint") : t("costByModelHintTokens")}</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[420px] border-collapse">
          <thead>
            <tr>
              {heads.map((h, i) => (
                <th
                  key={h}
                  className={`whitespace-nowrap border-b border-line px-2 py-1.5 text-[11px] font-semibold tracking-wide text-faint ${i >= 2 ? "text-right" : "text-left"}`}
                >
                  {t(h)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={heads.length} className="py-6 text-center text-xs text-faint">{t("noData")}</td>
              </tr>
            )}
            {rows.map((m) => (
              <tr key={`${m.provider}/${m.model}`} className="border-b border-white/5 last:border-b-0">
                <td className="max-w-[14rem] py-1.5 pl-2 pr-2 text-[12.5px] text-txt">
                  <span className="flex items-start gap-1.5">
                    <i className="mt-1.5 h-2 w-2 flex-none rounded-[2.5px]" style={{ background: providerColor(m.provider) }} />
                    <span className="break-words">{m.model}</span>
                  </span>
                </td>
                <td className="px-2 py-1.5 text-[12px] text-muted">{m.provider}</td>
                <td className="px-2 py-1.5 text-right text-[12px] tabular-nums text-muted">{m.calls}</td>
                <td className="px-2 py-1.5 text-right text-[12px] tabular-nums text-muted">
                  {fmt(m.input_tokens + m.output_tokens)}
                </td>
                <td className="px-2 py-1.5 pr-0 text-right text-[12.5px] tabular-nums text-txt">{fmtCny(costOf(m))}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** Top 10 sessions in the overview window, tokens desc. The runtime's session
 *  aggregate does not carry a context-window ratio today, so the >70% warning
 *  stays dormant until a payload supplies `context_ratio` — never guessed. */
function TopSessions({ rows }: { rows: UsageSessionRow[] }) {
  const t = useTranslations("settings.usage.overview");
  const top = [...rows]
    .sort((a, b) => b.input_tokens + b.output_tokens - (a.input_tokens + a.output_tokens))
    .slice(0, 10);
  return (
    <div className="min-w-0 overflow-hidden rounded-xl border border-line bg-card px-4 pb-2 pt-3.5">
      <div className="mb-1.5 flex items-baseline gap-3">
        <h3 className="text-[13px] font-semibold">{t("topSessions")}</h3>
        <span className="text-[11px] text-faint">{t("topSessionsHint")}</span>
      </div>
      {top.length === 0 && <div className="py-6 text-center text-xs text-faint">{t("noData")}</div>}
      {top.map((r) => {
        const ratio = (r as UsageSessionRow & { context_ratio?: number }).context_ratio;
        const near = typeof ratio === "number" && ratio > 0.7;
        return (
          <div
            key={r.session_id || "(bg)"}
            className="flex items-center gap-2 border-b border-white/5 py-2 text-[12.5px] last:border-b-0"
          >
            <span className="min-w-0 flex-1 truncate text-txt" title={r.title || t("untitledSession")}>
              {r.title || t("untitledSession")}
            </span>
            <span className="shrink-0 font-mono text-[10px] text-faint">
              {r.session_id ? `#${r.session_id.slice(0, 8)}` : t("backgroundSession")}
            </span>
            <span
              className="w-16 flex-none text-right tabular-nums text-muted"
              style={near ? { color: "#fbbf24" } : undefined}
              title={near ? t("nearContextLimit") : undefined}
            >
              {fmt(r.input_tokens + r.output_tokens)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function ModelRank({ ov }: { ov: UsageOverview }) {
  const t = useTranslations("settings.usage.overview");
  const total = ov.models.reduce((a, m) => a + m.input_tokens + m.output_tokens, 0) || 1;
  return (
    <div className="min-w-0 overflow-hidden rounded-xl border border-line bg-card px-4 pb-3 pt-3.5">
      <div className="mb-1.5 flex items-baseline gap-3">
        <h3 className="text-[13px] font-semibold">{t("modelRank")}</h3>
        <span className="text-[11px] text-faint">{t("modelRankHint")}</span>
      </div>
      <div className="flex items-baseline gap-2 pb-1.5 text-[11px] text-faint">
        <span>{t("labelModel")}</span>
        <span className="ml-auto whitespace-nowrap">{t("tokensShareHit")}</span>
      </div>
      {ov.models.length === 0 && <div className="py-6 text-center text-xs text-faint">{t("noData")}</div>}
      {ov.models.map((m) => {
        const v = m.input_tokens + m.output_tokens;
        const hit = m.cache_read_tokens > 0 || m.cache_hit_ratio > 0 ? pct(m.cache_hit_ratio) : "—";
        return (
          <div key={`${m.provider}/${m.model}`} className="flex items-start gap-2 border-b border-white/5 py-2 text-[12.5px] last:border-b-0">
            <i className="mt-1.5 h-2 w-2 flex-none rounded-[2.5px]" style={{ background: providerColor(m.provider) }} />
            <span className="min-w-0 flex-1 break-words text-txt">
              {m.model} <span className="text-[11.5px] text-faint">· {m.provider}</span>
            </span>
            <span className="flex-none whitespace-nowrap text-right tabular-nums text-muted">
              <b className="text-txt">{fmt(v)}</b> · {pct(v / total)} ·{" "}
              <span style={{ color: hit === "—" ? undefined : "#4ade80" }}>{hit === "—" ? "—" : `⚡${hit}`}</span>
            </span>
          </div>
        );
      })}
    </div>
  );
}
