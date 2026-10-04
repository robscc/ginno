"use client";

/** 概览：KPI → 每日趋势 → 小时分布 → Provider/模型分布。
 * 页签内的「统计窗口」只影响本页（评审决议：时间控制不跨页签）。 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { RefreshCw } from "lucide-react";
import * as api from "@/lib/runtime";
import type { UsageHourly, UsageOverview } from "@/lib/types";
import {
  CACHE_WRITE_COLOR,
  exact,
  fmt,
  pct,
  providerColor,
  SERIES,
  SeriesLegend,
  SkuBreakdown,
  StackedBars,
  TipRow,
  useTip,
} from "./charts";

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
  const [hourly, setHourly] = useState<UsageHourly | null>(null);
  const [hourDate, setHourDate] = useState<string>("");
  const [err, setErr] = useState("");
  const alive = useRef(true);

  const load = useCallback(async (days: number) => {
    try {
      const o = await api.getUsageOverview(days);
      if (!alive.current) return;
      setOv(o);
      setErr("");
      if (!hourDate && o.window?.to) {
        setHourDate(o.window.to);
        api.getUsageHourly(o.window.to).then((h) => alive.current && setHourly(h)).catch(() => {});
      }
    } catch {
      if (alive.current) setErr(t("loadFailed"));
    }
  }, [hourDate, t]);

  const loadHour = useCallback(async (date: string) => {
    try {
      const h = await api.getUsageHourly(date);
      if (alive.current) setHourly(h);
    } catch {
      /* keep previous */
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    load(range);
    const timer = setInterval(() => load(range), 60_000); // 页面可见时的轻轮询
    return () => {
      alive.current = false;
      clearInterval(timer);
    };
  }, [range, load]);

  function pickDay(date: string) {
    setHourDate(date);
    loadHour(date);
  }

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
          onClick={() => { load(range); if (hourDate) loadHour(hourDate); }}
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </button>
      </div>

      {empty ? (
        <div className="rounded-xl border border-line bg-card px-6 py-14 text-center text-sm text-faint">
          {t("empty")}
        </div>
      ) : (
        <>
          {/* KPI */}
          <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
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

          {/* 每日趋势 */}
          <div className="mt-3 rounded-xl border border-line bg-card px-4 pb-2 pt-3.5">
            <div className="mb-2 flex flex-wrap items-baseline gap-3">
              <h3 className="text-[13px] font-semibold">{t("dailyTrend")}</h3>
              <span className="text-[11px] text-faint">{t("dailyTrendHint")}</span>
              <SeriesLegend />
            </div>
            <StackedBars
              ariaLabel={t("ariaDaily")}
              data={daily.map((d) => ({
                key: d.date,
                label: d.date.slice(5),
                cache: d.cache_read_tokens,
                inputNet: Math.max(0, d.input_tokens - d.cache_read_tokens - d.cache_creation_tokens),
                output: d.output_tokens,
              }))}
              selected={hourDate}
              onPick={pickDay}
              tipFor={(d) => {
                const p = daily.find((x) => x.date === d.key);
                if (!p) return null;
                return (
                  <>
                    <b className="text-txt">{p.date}</b> · {t("tipRequests", { count: p.calls })} · {t("tipHit", { value: pct(p.cache_hit_ratio) })}
                    {p.models?.length ? (
                      <SkuBreakdown models={p.models} />
                    ) : (
                      /* 旧 runtime 无 per-model 明细 — 退回按天聚合（精确数字） */
                      <>
                        <TipRow label={t("labelInputNonCache")} value={exact(Math.max(0, p.input_tokens - p.cache_read_tokens - p.cache_creation_tokens))} swatch={SERIES.input} />
                        <TipRow label={t("labelCacheWrite")} value={exact(p.cache_creation_tokens)} swatch={CACHE_WRITE_COLOR} />
                        <TipRow label={t("labelCacheRead")} value={exact(p.cache_read_tokens)} swatch={SERIES.cache} />
                        <TipRow label={t("labelOutput")} value={exact(p.output_tokens)} swatch={SERIES.output} />
                      </>
                    )}
                    <div className="mt-1.5 border-t border-white/10 pt-1">
                      <TipRow label={t("labelTotal")} value={exact(p.input_tokens + p.output_tokens)} />
                    </div>
                    <div className="mt-0.5 text-faint">{t("tipClickDay")}</div>
                  </>
                );
              }}
            />
          </div>

          {/* 小时分布 */}
          <div className="mt-3 rounded-xl border border-line bg-card px-4 pb-2 pt-3.5">
            <div className="mb-2 flex flex-wrap items-baseline gap-3">
              <h3 className="text-[13px] font-semibold">{t("hourlyDist")}</h3>
              <div className="flex items-center gap-1.5">
                {daily.slice(-5).map((d) => (
                  <button
                    key={d.date}
                    onClick={() => pickDay(d.date)}
                    className={`rounded-md border px-2 py-0.5 text-[11.5px] ${d.date === hourDate ? "border-line2 bg-card2 text-txt" : "border-line text-muted hover:text-txt"}`}
                  >
                    {d.date === hourDate ? d.date : d.date.slice(5)}
                  </button>
                ))}
              </div>
              <span className="text-[11px] text-faint">{t("hourlyHint")}</span>
              <SeriesLegend />
            </div>
            {hourly && hourly.date === hourDate ? (
              <StackedBars
                ariaLabel={t("ariaHourly", { date: hourDate })}
                height={210}
                xEvery={3}
                data={hourly.hours.map((h) => ({
                  key: String(h.hour),
                  label: `${h.hour}h`,
                  cache: h.cache_read_tokens,
                  inputNet: Math.max(0, h.input_tokens - h.cache_read_tokens - (h.cache_creation_tokens ?? 0)),
                  output: h.output_tokens,
                }))}
                tipFor={(d) => {
                  const h = hourly.hours[Number(d.key)];
                  const cw = h.cache_creation_tokens ?? 0;
                  return (
                    <>
                      <b className="text-txt">{hourDate} {String(h.hour).padStart(2, "0")}:00–{String(h.hour).padStart(2, "0")}:59</b> · {t("tipRequests", { count: h.calls })}
                      {h.models?.length ? (
                        <SkuBreakdown models={h.models} />
                      ) : (
                        /* 旧 runtime 无 per-model 明细 — 退回按小时聚合（精确数字） */
                        <>
                          <TipRow label={t("labelInputNonCache")} value={exact(Math.max(0, h.input_tokens - h.cache_read_tokens - cw))} swatch={SERIES.input} />
                          <TipRow label={t("labelCacheWrite")} value={exact(cw)} swatch={CACHE_WRITE_COLOR} />
                          <TipRow label={t("labelCacheRead")} value={exact(h.cache_read_tokens)} swatch={SERIES.cache} />
                          <TipRow label={t("labelOutput")} value={exact(h.output_tokens)} swatch={SERIES.output} />
                        </>
                      )}
                      <div className="mt-1.5 border-t border-white/10 pt-1">
                        <TipRow label={t("labelTotal")} value={exact(h.input_tokens + h.output_tokens)} />
                      </div>
                    </>
                  );
                }}
              />
            ) : (
              <div className="py-12 text-center text-xs text-faint">{t("loading")}</div>
            )}
          </div>

          {/* 来源 / Provider / 模型 */}
          <div className="mt-3 grid gap-3 lg:grid-cols-3">
            <SourceDist ov={ov} />
            <ProviderDist ov={ov} />
            <ModelRank ov={ov} />
          </div>
        </>
      )}
    </div>
  );
}

/** 来源（usage-stats-design §3.6）：对话 vs 工作流 vs 后台任务。
 * 颜色与请求日志的 SrcChip 保持一致（RequestsPanel.SRC_STYLE 的 fg 列）。
 * labelKey 存 catalog key（settings.usage.overview.source.*，字面量联合）。 */
type SourceKey =
  | "chat" | "goal" | "compaction" | "workflow" | "memory" | "kb" | "probe" | "external" | "other";
const SOURCE_META: Record<string, { labelKey: SourceKey; color: string }> = {
  chat: { labelKey: "chat", color: "#8d90f8" },
  goal: { labelKey: "goal", color: "#b39df9" },
  compaction: { labelKey: "compaction", color: "#d9a93e" },
  workflow: { labelKey: "workflow", color: "#4ade80" },
  memory: { labelKey: "memory", color: "#38bdf8" },
  kb: { labelKey: "kb", color: "#60a5fa" },
  probe: { labelKey: "probe", color: "#9a9aa6" },
  external: { labelKey: "external", color: "#f472b6" },
  other: { labelKey: "other", color: "#9a9aa6" },
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
        const meta = SOURCE_META[s.source] || { labelKey: null as SourceKey | null, color: "#9a9aa6" };
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
                <i className="h-2 w-2 flex-none rounded-[2.5px]" style={{ background: meta.color }} />
                <span className="truncate">{label}</span>
              </span>
              <span className="ml-auto flex-none whitespace-nowrap text-xs tabular-nums text-muted">
                <b className="text-txt">{pct(v / total)}</b> · {fmt(v)}
              </span>
            </div>
            <div className="mt-1.5 h-2.5 overflow-hidden rounded-full bg-card2">
              <div className="h-full rounded-full" style={{ width: `${(v / vmax) * 100}%`, background: meta.color }} />
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
