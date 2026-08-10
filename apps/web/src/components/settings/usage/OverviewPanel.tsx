"use client";

/** 概览：KPI → 每日趋势 → 小时分布 → Provider/模型分布。
 * 页签内的「统计窗口」只影响本页（评审决议：时间控制不跨页签）。 */

import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import * as api from "@/lib/runtime";
import type { UsageHourly, UsageOverview } from "@/lib/types";
import { fmt, pct, providerColor, SERIES, SeriesLegend, StackedBars, TipRow, useTip } from "./charts";

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
      if (alive.current) setErr("用量数据加载失败（runtime 未连接？）");
    }
  }, [hourDate]);

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
    const t = setInterval(() => load(range), 60_000); // 页面可见时的轻轮询
    return () => {
      alive.current = false;
      clearInterval(t);
    };
  }, [range, load]);

  function pickDay(date: string) {
    setHourDate(date);
    loadHour(date);
  }

  if (err) return <div className="py-10 text-center text-sm text-faint">{err}</div>;
  if (!ov) return <div className="py-10 text-center text-sm text-faint">加载中…</div>;

  const t = ov.today;
  const daily = ov.daily;
  const y = daily.length > 1 ? daily[daily.length - 2] : null;
  const dTokens = y && y.input_tokens + y.output_tokens > 0
    ? ((t.input_tokens + t.output_tokens) - (y.input_tokens + y.output_tokens)) / (y.input_tokens + y.output_tokens) * 100
    : 0;
  const dCalls = y ? t.calls - y.calls : 0;
  const dHit = y && y.input_tokens > 0 && t.input_tokens > 0
    ? (t.cache_hit_ratio - y.cache_hit_ratio) * 100
    : 0;
  const empty = ov.totals.calls === 0 && t.calls === 0;

  return (
    <div>
      <div className="mb-3 flex items-center gap-2.5">
        <span className="text-[11.5px] text-faint">统计窗口只影响本页趋势与分布；会话 / 请求日志各有自己的时间过滤</span>
        <div className="flex-1" />
        <div className="flex rounded-lg border border-line bg-card p-0.5">
          {[7, 30, 90].map((d) => (
            <button
              key={d}
              onClick={() => setRange(d)}
              className={`rounded-md px-3 py-1 text-xs transition-colors ${range === d ? "bg-card2 text-txt" : "text-muted hover:text-txt"}`}
            >
              近 {d} 天
            </button>
          ))}
        </div>
        <button
          className="flex h-7 w-7 items-center justify-center rounded-lg border border-line text-muted hover:bg-card"
          title="刷新"
          onClick={() => { load(range); if (hourDate) loadHour(hourDate); }}
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </button>
      </div>

      {empty ? (
        <div className="rounded-xl border border-line bg-card px-6 py-14 text-center text-sm text-faint">
          开始对话后，这里会出现 Token 用量数据。
        </div>
      ) : (
        <>
          {/* KPI */}
          <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
            <Kpi
              label="今日 Tokens"
              value={fmt(t.input_tokens + t.output_tokens)}
              sub={<>↑ {fmt(t.input_tokens)} · ↓ {fmt(t.output_tokens)} · <Delta v={dTokens} suffix="%" /></>}
            />
            <Kpi label="今日请求" value={`${t.calls} 次`} sub={<>较昨日 <Delta v={dCalls} /> 次</>} />
            <Kpi
              label="今日缓存命中率"
              value={<><span style={{ color: "#4ade80" }}>⚡</span> {pct(t.cache_hit_ratio)}</>}
              sub={<>较昨日 {dHit >= 0 ? "+" : ""}{dHit.toFixed(1)} pt（缓存读按折扣计费）</>}
            />
            <Kpi
              label={`近 ${range} 天 Tokens`}
              value={fmt(ov.totals.input_tokens + ov.totals.output_tokens)}
              sub={<>{ov.sessions_active} 个会话 · {ov.providers.length} 个 Provider</>}
            />
          </div>

          {/* 每日趋势 */}
          <div className="mt-3 rounded-xl border border-line bg-card px-4 pb-2 pt-3.5">
            <div className="mb-2 flex flex-wrap items-baseline gap-3">
              <h3 className="text-[13px] font-semibold">每日趋势</h3>
              <span className="text-[11px] text-faint">点击某天 → 下方小时分布切换；悬停看明细</span>
              <SeriesLegend />
            </div>
            <StackedBars
              ariaLabel="每日 Token 趋势堆叠柱状图"
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
                    <b className="text-txt">{p.date}</b> · {p.calls} 次请求 · 命中 {pct(p.cache_hit_ratio)}
                    <TipRow label="总 Tokens" value={fmt(p.input_tokens + p.output_tokens)} />
                    <TipRow label="缓存读" value={fmt(p.cache_read_tokens)} swatch={SERIES.cache} />
                    <TipRow label="输入（非缓存）" value={fmt(Math.max(0, p.input_tokens - p.cache_read_tokens - p.cache_creation_tokens))} swatch={SERIES.input} />
                    <TipRow label="输出" value={fmt(p.output_tokens)} swatch={SERIES.output} />
                    <div className="mt-0.5 text-faint">点击查看该日小时分布</div>
                  </>
                );
              }}
            />
          </div>

          {/* 小时分布 */}
          <div className="mt-3 rounded-xl border border-line bg-card px-4 pb-2 pt-3.5">
            <div className="mb-2 flex flex-wrap items-baseline gap-3">
              <h3 className="text-[13px] font-semibold">小时分布</h3>
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
              <span className="text-[11px] text-faint">悬停看该小时 in / out / cache / 请求数</span>
              <SeriesLegend />
            </div>
            {hourly && hourly.date === hourDate ? (
              <StackedBars
                ariaLabel={`${hourDate} 小时分布`}
                height={210}
                xEvery={3}
                data={hourly.hours.map((h) => ({
                  key: String(h.hour),
                  label: `${h.hour}时`,
                  cache: h.cache_read_tokens,
                  inputNet: Math.max(0, h.input_tokens - h.cache_read_tokens),
                  output: h.output_tokens,
                }))}
                tipFor={(d) => {
                  const h = hourly.hours[Number(d.key)];
                  const tot = h.input_tokens + h.output_tokens;
                  return (
                    <>
                      <b className="text-txt">{hourDate} {String(h.hour).padStart(2, "0")}:00–{String(h.hour).padStart(2, "0")}:59</b>
                      <TipRow label="总 Tokens" value={fmt(tot)} />
                      <TipRow label="缓存读" value={fmt(h.cache_read_tokens)} swatch={SERIES.cache} />
                      <TipRow label="输入（非缓存）" value={fmt(Math.max(0, h.input_tokens - h.cache_read_tokens))} swatch={SERIES.input} />
                      <TipRow label="输出" value={fmt(h.output_tokens)} swatch={SERIES.output} />
                      <TipRow label="请求数" value={String(h.calls)} />
                    </>
                  );
                }}
              />
            ) : (
              <div className="py-12 text-center text-xs text-faint">加载中…</div>
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
 * 颜色与请求日志的 SrcChip 保持一致（RequestsPanel.SRC_STYLE 的 fg 列）。 */
const SOURCE_META: Record<string, { label: string; color: string }> = {
  chat: { label: "对话", color: "#8d90f8" },
  goal: { label: "Goal 续轮", color: "#b39df9" },
  compaction: { label: "历史压缩", color: "#d9a93e" },
  workflow: { label: "工作流", color: "#4ade80" },
  memory: { label: "记忆", color: "#38bdf8" },
  kb: { label: "知识库", color: "#60a5fa" },
  probe: { label: "探测", color: "#9a9aa6" },
  other: { label: "其他", color: "#9a9aa6" },
};

function SourceDist({ ov }: { ov: UsageOverview }) {
  const { show, hide, move, tipEl } = useTip();
  const sources = ov.sources || [];
  const total = sources.reduce((a, s) => a + s.input_tokens + s.output_tokens, 0) || 1;
  const vmax = Math.max(...sources.map((s) => s.input_tokens + s.output_tokens), 1);
  return (
    <div className="rounded-xl border border-line bg-card px-4 pb-3 pt-3.5">
      <div className="mb-1.5 flex items-baseline gap-3">
        <h3 className="text-[13px] font-semibold">来源分布</h3>
        <span className="text-[11px] text-faint">对话 · 工作流 · 后台</span>
      </div>
      {sources.length === 0 && <div className="py-6 text-center text-xs text-faint">暂无数据</div>}
      {sources.map((s) => {
        const meta = SOURCE_META[s.source] || { label: s.source, color: "#9a9aa6" };
        const v = s.input_tokens + s.output_tokens;
        return (
          <div
            key={s.source}
            className="grid grid-cols-[96px_1fr_118px] items-center gap-2.5 border-b border-white/5 py-2 last:border-b-0"
            onMouseEnter={(e) =>
              show(
                <>
                  <b className="text-txt">{meta.label}</b>（{s.source}）· 窗口内
                  <TipRow label="Tokens" value={fmt(v)} />
                  <TipRow label="输入" value={fmt(s.input_tokens)} />
                  <TipRow label="输出" value={fmt(s.output_tokens)} />
                  <TipRow label="缓存读" value={fmt(s.cache_read_tokens)} swatch={SERIES.cache} />
                  <TipRow label="请求数" value={String(s.calls)} />
                </>,
                e,
              )
            }
            onMouseMove={move}
            onMouseLeave={hide}
          >
            <span className="flex items-center gap-2 text-[12.5px] text-muted">
              <i className="h-2 w-2 flex-none rounded-[2.5px]" style={{ background: meta.color }} />
              {meta.label}
            </span>
            <span className="h-2.5 overflow-hidden rounded-full bg-card2">
              <span className="block h-full rounded-full" style={{ width: `${(v / vmax) * 100}%`, background: meta.color }} />
            </span>
            <span className="text-right text-xs tabular-nums text-muted">
              <b className="text-txt">{pct(v / total)}</b> · {fmt(v)}
            </span>
          </div>
        );
      })}
      {tipEl}
    </div>
  );
}

function ProviderDist({ ov }: { ov: UsageOverview }) {
  const { show, hide, move, tipEl } = useTip();
  const total = ov.providers.reduce((a, p) => a + p.input_tokens + p.output_tokens, 0) || 1;
  const vmax = Math.max(...ov.providers.map((p) => p.input_tokens + p.output_tokens), 1);
  return (
    <div className="rounded-xl border border-line bg-card px-4 pb-3 pt-3.5">
      <div className="mb-1.5 flex items-baseline gap-3">
        <h3 className="text-[13px] font-semibold">Provider 分布</h3>
        <span className="text-[11px] text-faint">窗口内占比</span>
      </div>
      {ov.providers.length === 0 && <div className="py-6 text-center text-xs text-faint">暂无数据</div>}
      {ov.providers.map((p) => {
        const v = p.input_tokens + p.output_tokens;
        return (
          <div
            key={p.provider}
            className="grid grid-cols-[96px_1fr_118px] items-center gap-2.5 border-b border-white/5 py-2 last:border-b-0"
            onMouseEnter={(e) =>
              show(
                <>
                  <b className="text-txt">{p.provider}</b> · 窗口内
                  <TipRow label="Tokens" value={fmt(v)} />
                  <TipRow label="输入" value={fmt(p.input_tokens)} />
                  <TipRow label="输出" value={fmt(p.output_tokens)} />
                  <TipRow label="缓存读" value={fmt(p.cache_read_tokens)} swatch={SERIES.cache} />
                  <TipRow label="命中率" value={p.input_tokens > 0 ? pct(p.cache_hit_ratio) : "—"} />
                  <TipRow label="请求数" value={String(p.calls)} />
                </>,
                e,
              )
            }
            onMouseMove={move}
            onMouseLeave={hide}
          >
            <span className="flex items-center gap-2 text-[12.5px] text-muted">
              <i className="h-2 w-2 flex-none rounded-[2.5px]" style={{ background: providerColor(p.provider) }} />
              {p.provider}
            </span>
            <span className="h-2.5 overflow-hidden rounded-full bg-card2">
              <span className="block h-full rounded-full" style={{ width: `${(v / vmax) * 100}%`, background: providerColor(p.provider) }} />
            </span>
            <span className="text-right text-xs tabular-nums text-muted">
              <b className="text-txt">{pct(v / total)}</b> · {fmt(v)}
            </span>
          </div>
        );
      })}
      {tipEl}
    </div>
  );
}

function ModelRank({ ov }: { ov: UsageOverview }) {
  const total = ov.models.reduce((a, m) => a + m.input_tokens + m.output_tokens, 0) || 1;
  return (
    <div className="rounded-xl border border-line bg-card px-4 pb-3 pt-3.5">
      <div className="mb-1.5 flex items-baseline gap-3">
        <h3 className="text-[13px] font-semibold">模型排行</h3>
        <span className="text-[11px] text-faint">按总 Tokens</span>
      </div>
      <div className="grid grid-cols-[14px_1.35fr_0.8fr_0.55fr_0.6fr] gap-2 pb-1.5 text-[11px] text-faint">
        <span />
        <span>模型</span>
        <span className="text-right">Tokens</span>
        <span className="text-right">占比</span>
        <span className="text-right">命中</span>
      </div>
      {ov.models.length === 0 && <div className="py-6 text-center text-xs text-faint">暂无数据</div>}
      {ov.models.map((m) => {
        const v = m.input_tokens + m.output_tokens;
        const hit = m.cache_read_tokens > 0 || m.cache_hit_ratio > 0 ? pct(m.cache_hit_ratio) : "—";
        return (
          <div key={`${m.provider}/${m.model}`} className="grid grid-cols-[14px_1.35fr_0.8fr_0.55fr_0.6fr] items-center gap-2 border-b border-white/5 py-2 text-[12.5px] last:border-b-0">
            <i className="h-2 w-2 rounded-[2.5px]" style={{ background: providerColor(m.provider) }} />
            <span>
              <span className="text-txt">{m.model}</span> <span className="text-[11.5px] text-faint">· {m.provider}</span>
            </span>
            <span className="text-right tabular-nums"><b>{fmt(v)}</b></span>
            <span className="text-right tabular-nums text-muted">{pct(v / total)}</span>
            <span className="text-right tabular-nums" style={{ color: hit === "—" ? undefined : "#4ade80" }}>
              {hit === "—" ? "—" : `⚡${hit}`}
            </span>
          </div>
        );
      })}
    </div>
  );
}
