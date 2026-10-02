"use client";

// 当天时间条（scheduled-tasks-design.md §3.4）：24h 自绘 SVG。已执行画实心
// 圆角条（ok 青绿 / error 红 / skipped 灰 / missed 空心短刻度），进行中青绿+
// 呼吸动画并延伸到现在线；未到的计划点画空心小方块；同时段重叠按 2–3 条
// lane 错开；现在线每分钟移动；hover 出 useTip 简介，点击执行段按目标分流。

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { ScheduleRun, ScheduleTimeline } from "@/lib/types";
import { useTip } from "@/components/settings/usage/charts";
import { dateStr, fmtClock, fmtDuration, runClickable, statusMeta } from "./shared";

// SVG 几何：等比 viewBox，宽度固定 1000；上下留白 + lane 高度。
const W = 1000;
const PAD_X = 8;
const LANE_H = 12;
const BAR_H = 8;
const TOP = 14;
const AXIS_H = 18;
const MAX_LANES = 3;

const msOf = (sec: number) => {
  const d = new Date(sec * 1000);
  return (d.getHours() * 3600 + d.getMinutes() * 60 + d.getSeconds()) * 1000;
};
const xOfMs = (ms: number) => PAD_X + (ms / 86400000) * (W - PAD_X * 2);

export function DayTimeline({
  date,
  onDateChange,
  refreshKey,
  onOpenDayRuns,
}: {
  /** YYYY-MM-DD（本地时区）。 */
  date: string;
  onDateChange: (d: string) => void;
  refreshKey: number;
  /** 「查看这天全部记录」→ 执行记录页签带单日过滤跳入（§3.5）。 */
  onOpenDayRuns?: () => void;
}) {
  const g = useGinno();
  // workflow 执行的回放（RunSubSessionView）只在工作区路由——从这里点开推回 "/"。
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

  // 现在线每分钟移动（§3.4）。
  const [, setTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 30000);
    return () => clearInterval(t);
  }, []);

  const todayStr = dateStr(new Date());
  const isToday = date === todayStr;
  // 当地「此刻」在一天内的毫秒偏移（Date.now()%86400000 是 UTC 语义，不能用）。
  const now = new Date();
  const nowMs = isToday
    ? (now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds()) * 1000 + now.getMilliseconds()
    : 0;

  // lane 分配：按开始时间排序，依次放进「上一段已结束」的最小可用 lane。
  const lanes = useMemo(() => {
    const runs = (tl?.runs ?? []).filter((r) => r.status !== "missed");
    runs.sort((a, b) => (a.started_at ?? a.scheduled_at ?? 0) - (b.started_at ?? b.scheduled_at ?? 0));
    const laneEnd: number[] = [];
    const out = new Map<string, number>();
    for (const r of runs) {
      const start = r.started_at ?? r.scheduled_at ?? 0;
      // running 的条延伸到现在线，占位也按现在算。
      const end = r.status === "running" && isToday ? Date.now() / 1000 : r.finished_at ?? start + 60;
      let lane = 0;
      while (lane < MAX_LANES && laneEnd[lane] !== undefined && laneEnd[lane] > start + 1) lane++;
      if (lane < MAX_LANES) {
        laneEnd[lane] = end;
        out.set(r.run_id, lane);
      }
      // 超过 MAX_LANES 的极端重叠：不画条（记录列表仍可见），不做缩放（§3.4）。
    }
    return out;
  }, [tl, isToday]);

  const height = TOP + LANE_H * MAX_LANES + AXIS_H;

  const hoverRun = (r: ScheduleRun) => (ev: React.MouseEvent) => {
    const meta = statusMeta(r.status);
    const dur =
      r.started_at && (r.finished_at || r.status === "running")
        ? fmtDuration((r.finished_at ?? Date.now() / 1000) - r.started_at)
        : null;
    show(
      <div>
        <div className="font-medium text-txt">
          {r.target_type === "workflow" ? "⚡" : "💬"} {r.task_name}
        </div>
        <div className="mt-0.5" style={{ color: meta.color }}>
          {meta.glyph} {meta.label}
          {r.status === "missed" ? "（机器睡眠或应用未运行）" : ""}
        </div>
        <div className="mt-0.5">
          {r.status === "missed"
            ? `计划 ${fmtClock(r.scheduled_at)}`
            : `开始 ${fmtClock(r.started_at)}${r.finished_at ? ` – 结束 ${fmtClock(r.finished_at)}` : ""}${
                dur ? `（${dur}）` : ""
              }`}
        </div>
        {r.summary && <div className="mt-1 line-clamp-2 text-txt/80">{r.summary.slice(0, 160)}</div>}
        {runClickable(r) && <div className="mt-1 text-faint">点击查看回放</div>}
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

  // 日期 ‹ 今天 › 切换（§3.4：可回看历史日；未来日只显示计划刻度）。
  const shiftDate = (days: number) => {
    const d = new Date(date + "T00:00:00");
    d.setDate(d.getDate() + days);
    onDateChange(dateStr(d));
  };

  const axisTicks = [0, 4, 8, 12, 16, 20, 24];

  return (
    <div className="rounded-xl border border-line bg-card p-3">
      <div className="mb-1 flex items-center gap-2 text-xs">
        <span className="font-medium text-txt">{isToday ? "今天" : date}</span>
        <button
          onClick={() => shiftDate(-1)}
          title="前一天"
          className="rounded p-0.5 text-faint hover:bg-card2 hover:text-txt"
        >
          <ChevronLeft className="h-3.5 w-3.5" />
        </button>
        <button
          onClick={() => shiftDate(1)}
          disabled={date >= todayStr}
          title="后一天"
          className="rounded p-0.5 text-faint hover:bg-card2 hover:text-txt disabled:opacity-30"
        >
          <ChevronRight className="h-3.5 w-3.5" />
        </button>
        {!isToday && (
          <button onClick={() => onDateChange(todayStr)} className="text-[11px] text-violet hover:underline">
            回到今天
          </button>
        )}
        {/* 图例（§3.1 头部示意） */}
        <span className="ml-auto flex items-center gap-3 text-[10px] text-faint">
          <span className="flex items-center gap-1">
            <i className="inline-block h-2 w-3 rounded-[2px]" style={{ background: "#14b8a6" }} /> 成功
          </span>
          <span className="flex items-center gap-1">
            <i className="inline-block h-2 w-3 rounded-[2px]" style={{ background: "#ef4444" }} /> 失败
          </span>
          <span className="flex items-center gap-1">
            <i className="inline-block h-2 w-3 animate-pulse rounded-[2px]" style={{ background: "#14b8a6" }} />{" "}
            进行中
          </span>
          <span className="flex items-center gap-1">
            <i className="inline-block h-2 w-2 border border-faint" /> 计划
          </span>
        </span>
      </div>

      <svg viewBox={`0 0 ${W} ${height}`} className="w-full" role="img" aria-label="当天执行时间条">
        {/* 轴线与刻度 */}
        <line x1={PAD_X} y1={height - AXIS_H} x2={W - PAD_X} y2={height - AXIS_H} stroke="var(--line)" strokeWidth={1} />
        {axisTicks.map((h) => {
          const x = xOfMs(h * 3600000);
          return (
            <g key={h}>
              <line x1={x} y1={height - AXIS_H} x2={x} y2={height - AXIS_H + 4} stroke="var(--line2)" strokeWidth={1} />
              <text x={x} y={height - 4} textAnchor="middle" fontSize={10} fill="var(--faint)">
                {h}
              </text>
            </g>
          );
        })}

        {/* lane 基线（淡） */}
        {Array.from({ length: MAX_LANES }).map((_, i) => (
          <line
            key={i}
            x1={PAD_X}
            y1={TOP + i * LANE_H + BAR_H / 2}
            x2={W - PAD_X}
            y2={TOP + i * LANE_H + BAR_H / 2}
            stroke="var(--line)"
            strokeWidth={0.5}
            strokeDasharray="2 6"
            opacity={0.5}
          />
        ))}

        {/* 已执行时段 */}
        {(tl?.runs ?? []).map((r) => {
          const lane = lanes.get(r.run_id);
          const y = TOP + (lane ?? 0) * LANE_H;
          const meta = statusMeta(r.status);
          if (r.status === "missed") {
            // missed 无时长：画在计划点的空心短刻度（§3.4），不可点。
            const at = r.scheduled_at;
            if (at == null || new Date(at * 1000).toDateString() !== new Date(date + "T00:00:00").toDateString())
              return null;
            const x = xOfMs(msOf(at));
            return (
              <rect
                key={r.run_id}
                x={x - 2}
                y={y}
                width={4}
                height={BAR_H}
                fill="none"
                stroke={meta.color}
                strokeWidth={1}
                rx={1}
                onMouseEnter={hoverRun(r)}
                onMouseMove={hoverRun(r)}
                onMouseLeave={hide}
              />
            );
          }
          if (lane === undefined) return null;
          const startSec = r.started_at ?? r.scheduled_at;
          if (startSec == null) return null;
          const startMs = msOf(startSec);
          const endMs = r.finished_at ? msOf(r.finished_at) : r.status === "running" && isToday ? nowMs : startMs + 60000;
          const x = xOfMs(startMs);
          const w = Math.max(3, xOfMs(endMs) - x);
          const clickable = runClickable(r);
          return (
            <rect
              key={r.run_id}
              x={x}
              y={y}
              width={w}
              height={BAR_H}
              rx={3}
              fill={meta.color}
              opacity={r.status === "skipped_overlap" ? 0.45 : r.status === "error" ? 0.85 : 0.9}
              className={r.status === "running" ? "animate-pulse" : undefined}
              onMouseEnter={hoverRun(r)}
              onMouseMove={hoverRun(r)}
              onMouseLeave={hide}
              onClick={() => clickRun(r)}
              style={{ cursor: clickable ? "pointer" : "default" }}
            />
          );
        })}

        {/* 未到计划点：enabled 任务 + 全局开的当天剩余触发（§3.4 空心小方块） */}
        {(tl?.planned ?? []).map((p, i) => {
          const x = xOfMs(msOf(p.at));
          if (x < PAD_X || x > W - PAD_X) return null;
          return (
            <rect
              key={`p-${p.task_id}-${i}`}
              x={x - 3}
              y={TOP + (MAX_LANES - 1) * LANE_H}
              width={6}
              height={6}
              fill="none"
              stroke="var(--faint)"
              strokeWidth={1}
              onMouseEnter={(ev) =>
                show(
                  <div>
                    <div className="font-medium text-txt">{p.task_name}</div>
                    <div className="mt-0.5">计划 {fmtClock(p.at)}</div>
                  </div>,
                  ev,
                )
              }
              onMouseMove={(ev) =>
                show(
                  <div>
                    <div className="font-medium text-txt">{p.task_name}</div>
                    <div className="mt-0.5">计划 {fmtClock(p.at)}</div>
                  </div>,
                  ev,
                )
              }
              onMouseLeave={hide}
            />
          );
        })}

        {/* 现在线（仅今天） */}
        {isToday && (
          <g>
            <line x1={xOfMs(nowMs)} y1={4} x2={xOfMs(nowMs)} y2={height - AXIS_H} stroke="var(--violet, #8b5cf6)" strokeWidth={1} />
            <text x={xOfMs(nowMs) + 3} y={10} fontSize={9} fill="#8b5cf6">
              {fmtClock(Date.now() / 1000)}
            </text>
          </g>
        )}
      </svg>

      {loaded && (tl?.runs?.length ?? 0) === 0 && (tl?.planned?.length ?? 0) === 0 && (
        <div className="py-2 text-center text-[11px] text-faint">这一天没有执行，也没有排期中的计划点。</div>
      )}
      {(tl?.runs?.length ?? 0) > 0 && onOpenDayRuns && (
        <div className="mt-1 text-right">
          <button onClick={onOpenDayRuns} className="text-[11px] text-violet hover:underline">
            查看这天全部记录 →
          </button>
        </div>
      )}

      {tipEl}
    </div>
  );
}
