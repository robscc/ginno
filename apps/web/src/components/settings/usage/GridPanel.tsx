"use client";

/** 2 小时节奏点格（usage-grid）：一天一列 × 12 个 2h 桶。
 * 几何 / 色阶 / 阈值 / hover-pin 行为全部照搬已验证的原型
 * docs/design/prototypes/usage-grid-prototype.html；颜色一律走
 * charts.tsx 导出的 --ug-* 变量，组件里不出现任何字面量色值。 */

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import type { UsageGridCell } from "@/lib/types";
import {
  HEAT_CLASSES,
  TipRow,
  UsageGridTokens,
  exact,
  fmt,
  heatStyle,
  heatVar,
} from "./charts";

const BINS = 12; // 每天固定 12 个 2h 桶
const GAP = 3; // 格子间距（原型验证值）
const WINDOWS = [14, 30, 60, 0] as const; // 0 = All（保留期内的全部天）
type Window = (typeof WINDOWS)[number];
/** 拉取上限 —— 与 usage_store.RETENTION_DAYS 一致；All 的「全部」就是这个上限。 */
const MAX_WINDOW_DAYS = 90;

/** 本地时区的 YYYY-MM-DD（不能用 toISOString，那是 UTC，会在本地跨日时错一天） */
function isoDay(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** 默认指标。设计文档倾向 net（去缓存读，量级更贴近真实成本），但这一点
 * 当时没有最终确认 —— 改默认值只需要动这一行。 */
const DEFAULT_METRIC: Metric = "net";
type Metric = "gross" | "net";

/** 指标 → UsageGridCell 的下标：gross=0, net=1 */
function metricValue(c: UsageGridCell, m: Metric): number {
  return m === "net" ? c[1] : c[0];
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

function localDay(d: Date): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

/* ---- 阈值：只对「有请求且已经发生」的桶取 P20/P40/P60/P80，随窗口重算 ---- */
function thresholds(cells: UsageGridCell[], m: Metric): number[] {
  const nz = cells.filter((c) => c[4] > 0).map((c) => metricValue(c, m)).sort((a, b) => a - b);
  if (!nz.length) return [0, 0, 0, 0];
  const q = (p: number) => nz[Math.min(nz.length - 1, Math.floor(p * nz.length))];
  return [q(0.2), q(0.4), q(0.6), q(0.8)];
}

function level(c: UsageGridCell, th: number[], m: Metric): number {
  if (!c || !c[4]) return 0; // 空桶（含当天没有遥测文件的日子 —— 数据契约里不区分）
  const v = metricValue(c, m);
  if (v <= th[0]) return 1;
  if (v <= th[1]) return 2;
  if (v <= th[2]) return 3;
  if (v <= th[3]) return 4;
  return 5;
}

interface Pick {
  d: string;
  h: number;
}

function binLabel(h: number): string {
  return `${pad2(h * 2)}:00`;
}

function dayLabel(d: string): string {
  return new Date(`${d}T00:00:00`).toLocaleDateString("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

export function GridPanel() {
  const t = useTranslations("settings.usage.overview");
  const [data, setData] = useState<{ days: string[]; grid: UsageGridCell[][] } | null>(null);
  const [err, setErr] = useState("");
  const [win, setWin] = useState<Window>(30);
  const [metric, setMetric] = useState<Metric>(DEFAULT_METRIC);
  const [sel, setSel] = useState<Pick | null>(null);
  const [hover, setHover] = useState<Pick | null>(null);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [cell, setCell] = useState(15);
  /* 单元格锚定的 tooltip：原型默认浮在格子上方，靠近卡片顶边时翻到下方。
     不用 charts 的 useTip —— 那个跟光标走，只按视口边缘翻转。 */
  const tipRef = useRef<HTMLDivElement | null>(null);
  const anchorRef = useRef<DOMRect | null>(null);
  const [tip, setTip] = useState<{ x: number; y: number; above: boolean } | null>(null);
  const [tipBody, setTipBody] = useState<ReactNode | null>(null);

  const showTip = useCallback((el: HTMLElement, body: ReactNode) => {
    anchorRef.current = el.getBoundingClientRect();
    setTipBody(body);
  }, []);
  const hideTip = useCallback(() => {
    anchorRef.current = null;
    setTip(null);
    setTipBody(null);
  }, []);
  /* 内容提交后再量高决定是否翻到格子下方 —— 提前量会拿到上一次的尺寸。
     用 layout effect 而不是 rAF：rAF 早于 React 提交，量到的还是旧高度。 */
  useLayoutEffect(() => {
    if (!tipBody || !anchorRef.current) return;
    const r = anchorRef.current;
    const h = tipRef.current?.offsetHeight ?? 0;
    const above = r.top - 8 - h > 12;
    setTip({ x: Math.min(r.left, window.innerWidth - 268), y: above ? r.top - 8 : r.bottom + 8, above });
  }, [tipBody]);

  useEffect(() => {
    let alive = true;
    /* 必须显式给区间：服务端无参时只返回默认的 30 天，
     * 那样 60d / All 会被前端切片成同样的 30 列。一次性拉满保留期，
     * 剩下的按窗口在前端切（数据量最大 90×12 个格子，可忽略）。 */
    const to = isoDay(new Date());
    const from = isoDay(new Date(Date.now() - (MAX_WINDOW_DAYS - 1) * 86_400_000));
    api
      .getUsageGrid({ from, to })
      .then((r) => {
        if (!alive) return;
        setData({ days: r.days || [], grid: r.grid || [] });
        setErr("");
      })
      .catch(() => {
        if (alive) setErr(t("loadFailed"));
      });
    return () => {
      alive = false;
    };
  }, [t]);

  /* 窗口切片：始终取最后 N 天（0 = 全部） */
  const view = useMemo(() => {
    const days = data?.days ?? [];
    const grid = data?.grid ?? [];
    const n = win === 0 ? days.length : Math.min(win, days.length);
    return { days: days.slice(-n), grid: grid.slice(-n) };
  }, [data, win]);

  /* 阈值只采样「已发生 + 有请求」的桶：今天还没到的桶和从没用过的空闲小时
     不能把分位数拉低。 */
  const now = new Date();
  const today = localDay(now);
  const nowBin = Math.min(BINS - 1, Math.floor(now.getHours() / 2));
  const th = useMemo(() => {
    const sampled: UsageGridCell[] = [];
    view.days.forEach((d, i) => {
      (view.grid[i] || []).forEach((c, h) => {
        const future = d === today && h > nowBin;
        if (!future && c[4] > 0) sampled.push(c);
      });
    });
    return thresholds(sampled, metric);
  }, [view, metric, today, nowBin]);

  /* 格子尺寸：随容器宽度/窗口变化重算，保持正方形并夹在 10–22px */
  const fit = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const cs = getComputedStyle(el);
    const avail = el.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight) - 30 - 8;
    const n = Math.max(1, view.days.length);
    setCell(Math.max(10, Math.min(22, Math.floor((avail - (n - 1) * GAP) / n))));
  }, [view.days.length]);

  useLayoutEffect(fit, [fit]);
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", fit);
      return () => window.removeEventListener("resize", fit);
    }
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    window.addEventListener("resize", fit);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", fit);
    };
  }, [fit]);

  /* Esc 释放 pin */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setSel(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // 切换窗口/指标后原 pin 可能已经不在窗口里
  useEffect(() => {
    if (sel && !view.days.includes(sel.d)) setSel(null);
  }, [view.days, sel]);

  const pick = sel || hover;

  if (err) return <div className="py-10 text-center text-sm text-faint">{err}</div>;
  if (!data) return <div className="py-10 text-center text-sm text-faint">{t("loading")}</div>;
  if (!data.days.length) {
    return (
      <div className="rounded-xl border border-line bg-card px-6 py-14 text-center text-sm text-faint">
        {t("gridEmpty")}
      </div>
    );
  }

  const gridVars = { "--ug-cell": `${cell}px`, "--ug-gap": `${GAP}px` } as CSSProperties;

  /* 分段控件：完全沿用 OverviewPanel 里 7/30/90 那个控件的 class 组合 */
  const seg = <T extends string | number>(
    opts: Array<{ v: T; label: string }>,
    current: T,
    onPick: (v: T) => void,
  ) => (
    <div className="flex rounded-lg border border-line bg-card p-0.5">
      {opts.map((o) => (
        <button
          key={String(o.v)}
          onClick={() => onPick(o.v)}
          aria-pressed={current === o.v}
          className={`rounded-md px-3 py-1 text-xs transition-colors ${current === o.v ? "bg-card2 text-txt" : "text-muted hover:text-txt"}`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );

  return (
    <div>
      <UsageGridTokens />

      {/* 控制条 */}
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <span className="text-[11.5px] text-faint">{t("gridWindow")}</span>
          {seg(
            WINDOWS.map((w) => ({ v: w, label: w === 0 ? "All" : `${w}d` })),
            win,
            setWin,
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[11.5px] text-faint">{t("gridMetric")}</span>
          {seg(
            [
              { v: "gross", label: t("gridMetricGross") },
              { v: "net", label: t("gridMetricNet") },
            ],
            metric,
            setMetric,
          )}
        </div>
        <div className="flex-1" />
        <span className="text-[11px] text-faint">{t("gridPinHint")}</span>
      </div>

      {/* 点格卡：顶部 padding 预留日期标签带，绝对定位的标签才不会被裁掉 */}
      <div className="overflow-hidden rounded-xl border border-line bg-card">
        <div ref={scrollRef} className="ug-scroll" style={gridVars} aria-label={t("gridAriaLabel")} role="group">
          <div className="ug-inner">
            {/* 左侧小时槽 —— 不加 padding-top，否则整列会错开一行 */}
            <div className="ug-gutter" aria-hidden="true">
              {Array.from({ length: BINS }, (_, h) => (
                <span key={h}>{pad2(h * 2)}</span>
              ))}
            </div>

            <div className="ug-cols">
              {view.days.map((d, i) => {
                const isToday = d === today;
                const isMonthStart = d.slice(8) === "01";
                const first = i === 0;
                // 只在首列和每月 1 号打标签；首列本身可能同时是月初，避免重叠
                const showDate = first || (isMonthStart && !first);
                const row = view.grid[i] || [];
                return (
                  <div className="ug-day" key={d}>
                    {showDate && <span className="ug-date">{d.slice(5)}</span>}
                    {isMonthStart && i > 0 && <span className="ug-monthsep" style={{ left: -4 }} />}
                    {Array.from({ length: BINS }, (_, h) => {
                      const c = row[h];
                      const future = isToday && h > nowBin;
                      if (future) {
                        // 还没到的桶：极淡、不可点、不参与分位数
                        return <div key={h} className="ug-cell ug-future" aria-hidden="true" />;
                      }
                      const lv = level(c, th, metric);
                      const isSel = !!sel && sel.d === d && sel.h === h;
                      const isNow = isToday && h === nowBin;
                      const req = c?.[4] || 0;
                      return (
                        <button
                          key={h}
                          type="button"
                          className={`ug-cell ${HEAT_CLASSES[lv]}${isNow ? " ug-now" : ""}`}
                          data-sel={isSel ? "1" : "0"}
                          aria-label={`${dayLabel(d)} ${binLabel(h)}, ${req ? `${t("gridRequests")}: ${req}` : t("gridNoRequests")}`}
                          onMouseEnter={(e) => {
                            setHover({ d, h });
                            showTip(
                              e.currentTarget,
                              <>
                                <b className="text-txt">
                                  {dayLabel(d)} · {binLabel(h)}
                                </b>
                                {c && c[4] > 0 ? (
                                  <>
                                    <TipRow label={t("gridCacheReads")} value={exact(c[3])} swatch={heatVar(2)} />
                                    <TipRow
                                      label={`${t("gridInputExCache")} / ${t("gridOutput")}`}
                                      value={`${exact(Math.max(0, c[1] - c[2]))} / ${exact(c[2])}`}
                                      swatch={heatVar(4)}
                                    />
                                    <TipRow label={t("gridRequests")} value={exact(c[4])} />
                                  </>
                                ) : (
                                  <div className="text-muted">{t("gridNoRequests")}</div>
                                )}
                              </>,
                            );
                          }}
                          onMouseLeave={() => {
                            setHover(null);
                            hideTip();
                          }}
                          onClick={() => setSel(isSel ? null : { d, h })}
                        />
                      );
                    })}
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        {/* 图例：放在滚动容器外，横向滚动时不跟着走 */}
        <div className="flex flex-wrap items-center gap-2.5 border-t border-line px-4 py-3 text-[11px] text-muted">
          <span>{t("gridLegendLess")}</span>
          <span className="ug-swatch" />
          {HEAT_CLASSES.slice(1).map((c) => (
            <span key={c} className={`ug-swatch ${c}`} />
          ))}
          <span>{t("gridLegendMore")}</span>
          {view.days.includes(today) && (
            <span className="ml-auto text-faint">{t("gridTodayInProgress")}</span>
          )}
        </div>
      </div>

      {/* 详情：左=选中/悬停的单桶，右=当天展开；都没有则窗口概览 */}
      <div className="mt-3 grid gap-3 lg:grid-cols-2">
        <div className="min-w-0 rounded-xl border border-line bg-card px-4 pb-3.5 pt-3.5">
          {pick ? <CellDetail pick={pick} grid={view.grid} days={view.days} pinned={!!sel} /> : <WindowSummary days={view.days} grid={view.grid} />}
        </div>
        <div className="min-w-0 rounded-xl border border-line bg-card px-4 pb-3.5 pt-3.5">
          {pick ? (
            <DayDetail pick={pick} grid={view.grid} days={view.days} metric={metric} />
          ) : (
            <EmptyRight days={view.days} grid={view.grid} />
          )}
        </div>
      </div>

      {/* 浮层 tooltip：fixed 定位，不受滚动容器裁剪 */}
      <div
        ref={tipRef}
        role="tooltip"
        /* 用 opacity 而不是 display 隐藏：元素始终参与布局，量高才准。
           pointer-events-none 保证不挡格子。 */
        className="pointer-events-none fixed z-50 max-w-[260px] rounded-lg border border-line2 bg-panel px-2.5 py-2 text-[11.5px] leading-relaxed text-txt shadow-xl transition-opacity duration-100 motion-reduce:transition-none"
        style={{
          opacity: tip && tipBody ? 1 : 0,
          left: tip?.x ?? 0,
          top: tip?.y ?? 0,
          transform: tip?.above ? "translateY(-100%)" : undefined,
        }}
        aria-hidden={!tipBody}
      >
        {tipBody}
      </div>
    </div>
  );
}

/** 左面板：单桶的三段占比（缓存读 / 新鲜输入 / 输出）+ 四个总数 */
function CellDetail({ pick, grid, days, pinned }: { pick: Pick; grid: UsageGridCell[][]; days: string[]; pinned: boolean }) {
  const t = useTranslations("settings.usage.overview");
  const ci = days.indexOf(pick.d);
  const c = ci >= 0 ? grid[ci]?.[pick.h] : null;
  if (!c || !c[4]) {
    return (
      <>
        <h3 className="text-[13px] font-semibold">
          {dayLabel(pick.d)} · {binLabel(pick.h)}
        </h3>
        <p className="mt-0.5 mb-2 text-[11px] text-faint">{pinned ? t("gridPinned") : t("gridHovering")}</p>
        <p className="text-xs text-muted">{t("gridNoRequests")}</p>
      </>
    );
  }
  const [g, n, o, ca] = c;
  const fresh = Math.max(0, n - o);
  const part = (v: number, level: number) => ({ width: `${g ? (v / g) * 100 : 0}%`, ...heatStyle(level) });
  return (
    <>
      <h3 className="text-[13px] font-semibold">
        {dayLabel(pick.d)} · {binLabel(pick.h)}–{pad2(pick.h * 2 + 2)}:00
      </h3>
      <p className="mt-0.5 mb-2 text-[11px] text-faint">{pinned ? t("gridPinned") : t("gridHovering")}</p>

      <div className="mb-2.5 flex h-2 overflow-hidden rounded-full bg-card2">
        <i className="block h-full" style={part(o, 5)} />
        <i className="block h-full" style={part(fresh, 4)} />
        <i className="block h-full" style={part(ca, 2)} />
      </div>

      <div className="flex justify-between border-t border-line py-1.5 text-xs">
        <span className="text-muted">{t("gridCacheReads")}</span>
        <span className="tabular-nums text-txt">
          {fmt(ca)} · <span className="text-muted">{g ? Math.round((ca / g) * 100) : 0}%</span>
        </span>
      </div>
      <div className="flex justify-between border-t border-line py-1.5 text-xs">
        <span className="text-muted">{t("gridInputExCache")}</span>
        <span className="tabular-nums text-txt">{fmt(fresh)}</span>
      </div>
      <div className="flex justify-between border-t border-line py-1.5 text-xs">
        <span className="text-muted">{t("gridOutput")}</span>
        <span className="tabular-nums text-txt">{fmt(o)}</span>
      </div>
      <div className="flex justify-between border-t border-line py-1.5 text-xs">
        <span className="text-muted">{t("gridGrossTotal")}</span>
        <span className="tabular-nums text-txt">{fmt(g)}</span>
      </div>
      <div className="flex justify-between border-t border-line py-1.5 text-xs">
        <span className="text-muted">{t("gridRequests")}</span>
        <span className="tabular-nums text-txt">{exact(c[4])}</span>
      </div>
    </>
  );
}

/** 右面板：把当天 12 个桶逐格摊开 */
function DayDetail({ pick, grid, days, metric }: { pick: Pick; grid: UsageGridCell[][]; days: string[]; metric: Metric }) {
  const t = useTranslations("settings.usage.overview");
  const ci = days.indexOf(pick.d);
  const day = ci >= 0 ? grid[ci] : undefined;
  if (!day) {
    return (
      <>
        <h3 className="text-[13px] font-semibold">{t("gridWindowSummary")}</h3>
        <p className="mt-0.5 text-[11px] text-faint">{t("gridNoRequestsDay")}</p>
      </>
    );
  }
  const total = day.reduce((a, c) => a + metricValue(c, metric), 0);
  const active = day.filter((c) => c[4] > 0).length;
  return (
    <>
      <h3 className="text-[13px] font-semibold">{t("gridDayInFull", { date: dayLabel(pick.d) })}</h3>
      <p className="mt-0.5 mb-2 text-[11px] text-faint">
        {t("gridActiveBins", { n: active, total: BINS })} · {fmt(total)}
      </p>
      {active === 0 ? (
        <p className="text-xs text-muted">{t("gridNoRequestsDay")}</p>
      ) : (
        day.map((c, h) =>
          c[4] ? (
            <div key={h} className="flex justify-between border-t border-line py-1.5 text-xs">
              <span className="tabular-nums text-muted">{binLabel(h)}</span>
              <span className="tabular-nums text-txt">{fmt(metricValue(c, metric))}</span>
            </div>
          ) : null,
        )
      )}
    </>
  );
}

/** 没有选中也没有悬停：左面板给交互提示，右面板给窗口概览 */
function WindowSummary({ days, grid }: { days: string[]; grid: UsageGridCell[][] }) {
  const t = useTranslations("settings.usage.overview");
  const cells = grid.flat();
  const req = cells.reduce((a, c) => a + c[4], 0);
  const active = cells.filter((c) => c[4] > 0).length;
  return (
    <>
      <h3 className="text-[13px] font-semibold">{t("gridHoverHint")}</h3>
      <p className="mt-0.5 mb-2 text-[11px] text-faint">{t("gridPinHint")}</p>
      <p className="text-xs leading-relaxed text-muted">{t("gridSplitHelp")}</p>
      <div className="mt-2 flex justify-between border-t border-line py-1.5 text-xs">
        <span className="text-muted">{t("gridRequests")}</span>
        <span className="tabular-nums text-txt">{exact(req)}</span>
      </div>
      <p className="border-t border-line py-1.5 text-[11px] text-faint">
        {t("gridActiveBinsShort", { n: active })}
      </p>
      <p className="tabular-nums text-[11px] text-faint">
        {days[0]} → {days[days.length - 1]}
      </p>
    </>
  );
}

/** 右面板的窗口概览：总数 / 最忙的一天 / 峰值桶 / 全天空闲的小时数 */
function EmptyRight({ days, grid }: { days: string[]; grid: UsageGridCell[][] }) {
  const t = useTranslations("settings.usage.overview");
  const cells = grid.flat();
  const gross = cells.reduce((a, c) => a + c[0], 0);
  const best = days
    .map((d, i) => [d, (grid[i] || []).reduce((a, c) => a + c[0], 0)] as const)
    .sort((a, b) => b[1] - a[1])[0];
  const peak = cells.reduce((a, c) => (c[0] > a[0] ? c : a), [0, 0, 0, 0, 0] as UsageGridCell)[0];
  const emptyRows = Array.from({ length: BINS }, (_, h) => h).filter((h) => !grid.some((r) => r[h]?.[4])).length;
  return (
    <>
      <h3 className="text-[13px] font-semibold">{t("gridWindowSummary")}</h3>
      <p className="mt-0.5 mb-2 text-[11px] text-faint">
        {days[0]} → {days[days.length - 1]}
      </p>
      <div className="flex justify-between border-t border-line py-1.5 text-xs">
        <span className="text-muted">{t("gridGrossTotal")}</span>
        <span className="tabular-nums text-txt">{fmt(gross)}</span>
      </div>
      <div className="flex justify-between border-t border-line py-1.5 text-xs">
        <span className="text-muted">{t("gridBusiestDay")}</span>
        <span className="tabular-nums text-txt">{best ? best[0] : "—"}</span>
      </div>
      <div className="flex justify-between border-t border-line py-1.5 text-xs">
        <span className="text-muted">{t("gridPeakBin")}</span>
        <span className="tabular-nums text-txt">{fmt(peak)}</span>
      </div>
      <div className="flex justify-between border-t border-line py-1.5 text-xs">
        <span className="text-muted">{t("gridEmptyRows")}</span>
        <span className="tabular-nums text-txt">
          {emptyRows} / {BINS}
        </span>
      </div>
    </>
  );
}