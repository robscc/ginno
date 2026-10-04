"use client";

// 当天时间条(scheduled-tasks-design.md §3.4,选型 D·15 分钟热力格):
// 一天 96 格,格子分层小带——**填充色=任务身份**(调色板循环,顶部图例),
// **边框=结果**(成功=任务色实线/失败=红实线/运行中=呼吸/跳过=灰虚线),
// 错过=琥珀虚线空格、计划=天蓝虚线空格。hover 出该格全部执行,层可单独点击
// 进回放(prompt→ScheduleRunView,workflow→run 视图);现在线白色贯穿。

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { ScheduleRun, ScheduleTimeline } from "@/lib/types";
import { useTip } from "@/components/settings/usage/charts";
import { dateStr, fmtClock, fmtDuration, runClickable, statusMeta } from "./shared";

// SVG 几何:96 格 × 15 分钟,viewBox 宽 1000。TOP 留给现在线时间标签。
const W = 1000;
const PAD = 10;
const CELLS = 96;
const GAP = 2;
const CW = (W - PAD * 2 - GAP * (CELLS - 1)) / CELLS;
const TOP = 20;
const CH = 56; // 格高(多层时在格内分层;偏高更醒目)
const AXIS = 22;
const H = TOP + CH + AXIS;
const MAX_LAYERS = 4; // 同格最多画 4 层,更多并入末层(hover 仍列全)

// 任务身份色:图表调色板循环(globals.css --chart-1..5 同源),同任务全天/跨天一致。
// 任务身份色:--chart-* 在 globals.css 有明暗两套,主题切换自动换档
const TASK_PALETTE = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)"];
// 结果边框色(--st-* 同样双主题)
const STROKE = { err: "var(--st-error)", skipped: "var(--st-skipped)", missed: "var(--st-missed)", planned: "var(--st-planned)", now: "rgb(var(--txt))" };

const xOfCell = (c: number) => PAD + c * (CW + GAP);
const hourX = (h: number) => PAD + (h / 24) * (W - PAD * 2);

export function DayTimeline({
  date,
  onDateChange,
  refreshKey,
  onOpenDayRuns,
}: {
  /** YYYY-MM-DD(本地时区)。 */
  date: string;
  onDateChange: (d: string) => void;
  refreshKey: number;
  /** 「View all runs of this day」→ 执行记录页签带单日过滤跳入(§3.5)。 */
  onOpenDayRuns?: () => void;
}) {
  const g = useGinno();
  const tr = useTranslations("sched");
  // workflow 执行的回放(RunSubSessionView)只在工作区路由——从这里点开推回 "/"。
  const router = useRouter();
  const [tl, setTl] = useState<ScheduleTimeline | null>(null);
  const [loaded, setLoaded] = useState(false);
  const { show, hide, tipEl } = useTip();

  const load = useCallback(async () => {
    try {
      setTl(await api.getScheduleTimeline(date));
    } catch {
      /* sidecar 未起——保留上次数据 */
    } finally {
      setLoaded(true);
    }
  }, [date]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  // 现在线每 30s 移动。
  const [, setTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 30000);
    return () => clearInterval(t);
  }, []);

  const todayStr = dateStr(new Date());
  const isToday = date === todayStr;
  const now = new Date();
  const nowH =
    isToday ? now.getHours() + now.getMinutes() / 60 + now.getSeconds() / 3600 : 0;

  // 任务 → 身份色:runs 与 planned 的任务并集,按首次出现顺序循环取色。
  const taskColors = useMemo(() => {
    const ids: string[] = [];
    for (const r of tl?.runs ?? []) if (!ids.includes(r.task_id)) ids.push(r.task_id);
    for (const p of tl?.planned ?? []) if (!ids.includes(p.task_id)) ids.push(p.task_id);
    const m = new Map<string, string>();
    ids.forEach((id, i) => m.set(id, TASK_PALETTE[i % TASK_PALETTE.length]));
    return m;
  }, [tl]);
  const colorOf = (taskId: string) => taskColors.get(taskId) ?? "var(--st-skipped)";

  // 格子模型:每格 = 该 15 分钟内重叠的执行列表(运行中按现在截断)。
  const cells = useMemo(() => {
    const out: ScheduleRun[][] = Array.from({ length: CELLS }, () => []);
    const dayStart = new Date(date + "T00:00:00").getTime() / 1000;
    for (const r of tl?.runs ?? []) {
      if (r.status === "missed") continue; // missed 单独画(无时长)
      const start = r.started_at ?? r.scheduled_at;
      if (start == null) continue;
      const end = r.finished_at ?? (r.status === "running" && isToday ? Date.now() / 1000 : start + 60);
      const c0 = Math.max(0, Math.floor((start - dayStart) / 900));
      const c1 = Math.min(CELLS - 1, Math.floor((end - dayStart) / 900));
      for (let c = c0; c <= c1; c++) if (c >= 0 && c < CELLS) out[c].push(r);
    }
    return out;
  }, [tl, date, isToday]);

  const missedCells = useMemo(() => {
    const dayStart = new Date(date + "T00:00:00").getTime() / 1000;
    const m = new Map<number, ScheduleRun[]>();
    for (const r of tl?.runs ?? []) {
      if (r.status !== "missed" || r.scheduled_at == null) continue;
      const c = Math.floor((r.scheduled_at - dayStart) / 900);
      if (c >= 0 && c < CELLS) m.set(c, [...(m.get(c) ?? []), r]);
    }
    return m;
  }, [tl, date]);

  const plannedCells = useMemo(() => {
    const dayStart = new Date(date + "T00:00:00").getTime() / 1000;
    const m = new Map<number, { task_id: string; task_name: string; at: number }[]>();
    for (const p of tl?.planned ?? []) {
      const c = Math.floor((p.at - dayStart) / 900);
      if (c >= 0 && c < CELLS) m.set(c, [...(m.get(c) ?? []), p]);
    }
    return m;
  }, [tl, date]);

  const hoverRun = (r: ScheduleRun) => (ev: React.MouseEvent) => {
    const meta = statusMeta(r.status);
    const dur =
      r.started_at && (r.finished_at || r.status === "running")
        ? fmtDuration((r.finished_at ?? Date.now() / 1000) - r.started_at)
        : null;
    show(
      <div>
        <div className="flex items-center gap-1.5 font-medium text-txt">
          <i className="inline-block h-2.5 w-2.5 shrink-0 rounded-[3px]" style={{ background: colorOf(r.task_id) }} />
          {r.target_type === "workflow" ? "⚡" : "💬"} {r.task_name}
        </div>
        <div className="mt-0.5" style={{ color: meta.color }}>
          {meta.glyph} {meta.label}
          {r.status === "missed" ? tr("day.missedSuffix") : ""}
        </div>
        <div className="mt-0.5">
          {r.status === "missed"
            ? tr("day.tipDue", { clock: fmtClock(r.scheduled_at) })
            : `${tr("day.tipStart", { clock: fmtClock(r.started_at) })}${
                r.finished_at ? tr("day.tipEnd", { clock: fmtClock(r.finished_at) }) : ""
              }${dur ? ` (${dur})` : ""}`}
        </div>
        {r.summary && <div className="mt-1 line-clamp-2 text-txt/80">{r.summary.slice(0, 160)}</div>}
        {runClickable(r) && <div className="mt-1 text-faint">{tr("day.tipClick")}</div>}
      </div>,
      ev,
    );
  };

  const clickRun = (r: ScheduleRun) => {
    hide();
    if (!runClickable(r)) return;
    if (r.target_type === "workflow" && r.workflow_run_id) {
      g.openRunView(r.workflow_run_id);
      router.push("/");
    } else if (r.target_type === "prompt") {
      g.openScheduleRun(r);
    }
  };

  // 日期 ‹ Today › 切换(可回看历史日;未来日只有计划格)。
  const shiftDate = (days: number) => {
    const d = new Date(date + "T00:00:00");
    d.setDate(d.getDate() + days);
    onDateChange(dateStr(d));
  };

  const axisTicks = [0, 4, 8, 12, 16, 20, 24];
  const dayStart = new Date(date + "T00:00:00");
  const cellClock = (c: number) => fmtClock(dayStart.getTime() / 1000 + c * 900);

  // 单层小带的形状:填充=任务色(半透明),边框=结果。
  const stripAttrs = (r: ScheduleRun, taskId: string) => {
    const col = colorOf(taskId);
    if (r.status === "error")
      return { fill: col, fillOpacity: 0.45, stroke: STROKE.err, strokeWidth: 1.8, strokeDasharray: undefined as string | undefined };
    if (r.status === "skipped_overlap")
      return { fill: col, fillOpacity: 0.25, stroke: STROKE.skipped, strokeWidth: 1.2, strokeDasharray: "2 1.5" };
    // ok / running:任务色实线;running 由外层加呼吸动画
    return { fill: col, fillOpacity: 0.45, stroke: col, strokeWidth: 1.6, strokeDasharray: undefined as string | undefined };
  };

  return (
    <div className="rounded-xl border border-line bg-card p-4">
      <div className="mb-2 flex items-center gap-2 text-xs">
        <span className="font-medium text-txt">{isToday ? tr("dayPrefix.today") : date}</span>
        <button
          onClick={() => shiftDate(-1)}
          title={tr("day.prevDay")}
          className="rounded p-0.5 text-faint hover:bg-card2 hover:text-txt"
        >
          <ChevronLeft className="h-3.5 w-3.5" />
        </button>
        <button
          onClick={() => shiftDate(1)}
          disabled={date >= todayStr}
          title={tr("day.nextDay")}
          className="rounded p-0.5 text-faint hover:bg-card2 hover:text-txt disabled:opacity-30"
        >
          <ChevronRight className="h-3.5 w-3.5" />
        </button>
        {!isToday && (
          <button onClick={() => onDateChange(todayStr)} className="text-[11px] text-violet hover:underline">
            {tr("day.backToToday")}
          </button>
        )}
      </div>

      {/* 任务色图例(填充=任务)+ 结果图例(边框=结果) */}
      <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10.5px] text-faint">
        {[...taskColors.entries()].slice(0, 8).map(([tid, col]) => {
          const name = tl?.runs.find((r) => r.task_id === tid)?.task_name ?? tl?.planned.find((p) => p.task_id === tid)?.task_name ?? tid;
          return (
            <span key={tid} className="flex items-center gap-1.5">
              <i className="inline-block h-2.5 w-2.5 rounded-[3px]" style={{ background: col }} />
              {name}
            </span>
          );
        })}
        <span className="ml-auto flex items-center gap-3">
          <span className="flex items-center gap-1.5">
            <i className="inline-block h-3 w-3 rounded-[3px] border-[1.6px]" style={{ borderColor: "var(--chart-1)" }} /> {tr("status.ok")}
          </span>
          <span className="flex items-center gap-1.5">
            <i className="inline-block h-3 w-3 rounded-[3px] border-[1.8px]" style={{ borderColor: STROKE.err }} /> {tr("status.error")}
          </span>
          <span className="flex items-center gap-1.5">
            <i className="inline-block h-3 w-3 rounded-[3px] border-[1.4px] border-dashed" style={{ borderColor: STROKE.missed }} /> {tr("status.missed")}
          </span>
          <span className="flex items-center gap-1.5">
            <i className="inline-block h-3 w-3 rounded-[3px] border-[1.4px] border-dashed" style={{ borderColor: STROKE.planned }} /> {tr("day.legendPlanned")}
          </span>
        </span>
      </div>

      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={tr("day.heatGridLabel")}>
        {/* 网格底轨 */}
        <rect x={PAD - 4} y={TOP - 4} width={W - PAD * 2 + 8} height={CH + 8} rx={8} fill="rgb(var(--base))" opacity={0.9} />

        {/* 96 格 */}
        {Array.from({ length: CELLS }, (_, c) => {
          const rs = cells[c];
          const missed = missedCells.get(c);
          const planned = plannedCells.get(c);
          const x = xOfCell(c);
          if (rs.length === 0 && !missed && !planned) {
            return <rect key={c} x={x} y={TOP} width={CW} height={CH} rx={2.5} fill="transparent" stroke="rgb(var(--line))" strokeWidth={1} />;
          }
          const shown = rs.slice(0, MAX_LAYERS);
          const layerH = (CH - (shown.length - 1) * 1.5) / Math.max(1, shown.length);
          return (
            <g key={c}>
              {/* miss / plan 的空格底(不与执行共存时可见) */}
              {rs.length === 0 && (missed || planned) && (
                <rect
                  x={x}
                  y={TOP}
                  width={CW}
                  height={CH}
                  rx={2.5}
                  fill="transparent"
                  stroke={missed ? STROKE.missed : STROKE.planned}
                  strokeWidth={1.4}
                  strokeDasharray="3 2"
                  onMouseEnter={(ev) =>
                    missed
                      ? missed.forEach((r) => hoverRun(r)(ev))
                      : show(
                          <div>
                            {(planned ?? []).map((p) => (
                              <div key={p.task_id} className="flex items-center gap-1.5 font-medium text-txt">
                                <i className="inline-block h-2.5 w-2.5 rounded-[3px]" style={{ background: colorOf(p.task_id) }} />
                                {p.task_name}
                              </div>
                            ))}
                            <div className="mt-0.5" style={{ color: STROKE.planned }}>
                              {tr("day.tipPlanned")}
                            </div>
                            <div className="mt-0.5">{tr("day.tipDue", { clock: fmtClock(planned?.[0]?.at) })}</div>
                          </div>,
                          ev,
                        )
                  }
                  onMouseLeave={hide}
                />
              )}
              {/* 执行层:每层一个任务色小带,边框编码结果,层可点 */}
              {shown.map((r, i) => {
                const a = stripAttrs(r, r.task_id);
                const y = TOP + i * (layerH + 1.5);
                const clickable = runClickable(r);
                return (
                  <rect
                    key={`${r.run_id}-${i}`}
                    x={x + 0.5}
                    y={y}
                    width={CW - 1}
                    height={layerH}
                    rx={1.5}
                    fill={a.fill}
                    fillOpacity={a.fillOpacity}
                    stroke={a.stroke}
                    strokeWidth={a.strokeWidth}
                    strokeDasharray={a.strokeDasharray}
                    className={r.status === "running" ? "animate-pulse" : undefined}
                    onMouseEnter={hoverRun(r)}
                    onMouseMove={hoverRun(r)}
                    onMouseLeave={hide}
                    onClick={() => clickRun(r)}
                    style={{ cursor: clickable ? "pointer" : "default" }}
                  />
                );
              })}
              {/* 超过 MAX_LAYERS:整格覆盖一层透明命中区,tooltip 列全部 */}
              {rs.length > MAX_LAYERS && (
                <rect
                  x={x}
                  y={TOP}
                  width={CW}
                  height={CH}
                  fill="transparent"
                  onMouseEnter={(ev) => rs.forEach((r) => hoverRun(r)(ev))}
                  onMouseLeave={hide}
                />
              )}
            </g>
          );
        })}

        {/* 轴线与刻度 */}
        <line x1={PAD} y1={H - AXIS} x2={W - PAD} y2={H - AXIS} stroke="rgb(var(--line2))" strokeWidth={1.2} />
        {axisTicks.map((h) => (
          <g key={h}>
            <line x1={hourX(h)} y1={H - AXIS} x2={hourX(h)} y2={H - AXIS + 4} stroke="rgb(var(--line2))" strokeWidth={1} />
            <text x={hourX(h)} y={H - 7} textAnchor="middle" fontSize={11} fill="rgb(var(--faint))">
              {h === 24 ? "24h" : h}
            </text>
          </g>
        ))}

        {/* 现在线(仅今天):白色贯穿 + 顶端圆点与时间 */}
        {isToday && (
          <g>
            <line x1={hourX(nowH)} y1={8} x2={hourX(nowH)} y2={H - AXIS} stroke={STROKE.now} strokeWidth={1.6} />
            <circle cx={hourX(nowH)} cy={9} r={3} fill={STROKE.now} />
            <text x={hourX(nowH) + 5} y={13} fontSize={10.5} fontWeight={600} fill={STROKE.now}>
              {fmtClock(Date.now() / 1000)}
            </text>
          </g>
        )}
      </svg>

      {loaded && (tl?.runs?.length ?? 0) === 0 && (tl?.planned?.length ?? 0) === 0 && (
        <div className="py-2 text-center text-[11px] text-faint">{tr("day.emptyDay")}</div>
      )}
      {(tl?.runs?.length ?? 0) > 0 && onOpenDayRuns && (
        <div className="mt-1 text-right">
          <button onClick={onOpenDayRuns} className="text-[11px] text-violet hover:underline">
            {tr("day.viewAllRuns")}
          </button>
        </div>
      )}

      {tipEl}
    </div>
  );
}
