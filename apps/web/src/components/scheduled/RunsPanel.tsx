"use client";

// 执行记录面板（scheduled-tasks-design.md §3.5）：时间升/降序切换 + 任务/
// 目标/状态过滤 + 日期范围（近 1/7/30 天，客户端过滤）+ 50 行/页分页。
// 行点击按目标分流进回放（prompt → 影子会话回放；workflow → run 视图），
// missed/skipped 行不可点。

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowDown, ArrowUp, Loader2 } from "lucide-react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { ScheduleConfig, ScheduleRun } from "@/lib/types";
import {
  fmtClock,
  fmtDayPrefix,
  fmtDuration,
  runClickable,
  statusMeta,
} from "./shared";

const PAGE_SIZE = 50;
// 范围过滤在客户端做（API 的 date 是单日），最多向后取 6 页兜底。
const MAX_PAGES = 6;

const RANGE_OPTIONS: Array<[string, string]> = [
  ["1", "近 1 天"],
  ["7", "近 7 天"],
  ["30", "近 30 天"],
];

export function RunsPanel({
  cfg,
  refreshKey,
  dateFilter,
  onClearDateFilter,
}: {
  cfg: ScheduleConfig;
  refreshKey: number;
  /** 时间条点进来的单日过滤（YYYY-MM-DD）；null = 用范围选择。 */
  dateFilter: string | null;
  onClearDateFilter: () => void;
}) {
  const g = useGinno();
  // workflow 目标的回放是工作区路由里的 RunSubSessionView——从本页点开要推回 "/"。
  const router = useRouter();
  const [rows, setRows] = useState<ScheduleRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [sort, setSort] = useState<"desc" | "asc">("desc");
  const [taskId, setTaskId] = useState("");
  const [targetType, setTargetType] = useState("");
  const [status, setStatus] = useState("");
  const [range, setRange] = useState("7");
  const [page, setPage] = useState(0);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    (async () => {
      const acc: ScheduleRun[] = [];
      for (let p = 1; p <= MAX_PAGES; p++) {
        try {
          const r = await api.listScheduleRuns({
            task_id: taskId || undefined,
            target_type: targetType || undefined,
            status: status || undefined,
            date: dateFilter ?? undefined,
            sort,
            page: p,
          });
          acc.push(...(r.rows ?? []));
          // 拿满一页才继续；总数/短页都说明到底了。
          if ((r.rows?.length ?? 0) < PAGE_SIZE) break;
        } catch {
          break;
        }
      }
      if (alive) {
        setRows(acc);
        setLoading(false);
        setPage(0);
      }
    })();
    return () => {
      alive = false;
    };
  }, [taskId, targetType, status, sort, dateFilter, refreshKey]);

  // 日期范围（近 N 天）客户端过滤；dateFilter 模式下服务端已按单日过滤。
  const filtered = useMemo(() => {
    if (dateFilter) return rows;
    const cutoff = Date.now() / 1000 - Number(range) * 86400;
    return rows.filter((r) => (r.scheduled_at ?? r.started_at ?? 0) >= cutoff);
  }, [rows, range, dateFilter]);

  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageRows = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  const openRun = (r: ScheduleRun) => {
    if (!runClickable(r)) return;
    if (r.target_type === "workflow" && r.workflow_run_id) {
      g.openRunView(r.workflow_run_id);
      router.push("/");
    } else if (r.target_type === "prompt") {
      // 影子会话回放（ScheduleRunView）在 main 顶层挂载，不离开当前路由。
      g.openScheduleRun(r);
    }
  };

  return (
    <div>
      {/* 过滤行（§3.5）：排序 / 任务 / 目标 / 状态 / 日期 */}
      <div className="mb-3 flex flex-wrap items-center gap-2 text-xs text-muted">
        <button
          onClick={() => setSort((s) => (s === "desc" ? "asc" : "desc"))}
          className="flex items-center gap-1 rounded-lg border border-line px-2 py-1 hover:text-txt"
          title="切换时间排序"
        >
          时间 {sort === "desc" ? <ArrowDown className="h-3 w-3" /> : <ArrowUp className="h-3 w-3" />}
        </button>
        <select className="field w-auto px-2 py-1 text-xs" value={taskId} onChange={(e) => setTaskId(e.target.value)}>
          <option value="">任务: 全部</option>
          {cfg.tasks.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
        <select
          className="field w-auto px-2 py-1 text-xs"
          value={targetType}
          onChange={(e) => setTargetType(e.target.value)}
        >
          <option value="">目标: 全部</option>
          <option value="prompt">💬 对话</option>
          <option value="workflow">⚡ Workflow</option>
        </select>
        <select className="field w-auto px-2 py-1 text-xs" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">状态: 全部</option>
          <option value="ok">成功</option>
          <option value="error">失败</option>
          <option value="running">进行中</option>
          <option value="missed">错过</option>
          <option value="skipped_overlap">跳过(重叠)</option>
        </select>
        {dateFilter ? (
          <span className="flex items-center gap-1.5 rounded-lg border border-violet/50 bg-violet/10 px-2 py-1 text-violet">
            {dateFilter}
            <button onClick={onClearDateFilter} title="清除日期过滤" className="text-violet/70 hover:text-violet">
              ✕
            </button>
          </span>
        ) : (
          <select className="field w-auto px-2 py-1 text-xs" value={range} onChange={(e) => setRange(e.target.value)}>
            {RANGE_OPTIONS.map(([v, label]) => (
              <option key={v} value={v}>
                {label}
              </option>
            ))}
          </select>
        )}
        <span className="ml-auto text-[11px] text-faint">{filtered.length} 条</span>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 py-10 text-sm text-faint">
          <Loader2 className="h-4 w-4 animate-spin" /> 正在读取执行记录…
        </div>
      ) : pageRows.length === 0 ? (
        <div className="rounded-xl border border-dashed border-line py-10 text-center text-sm text-faint">
          该条件下没有执行记录。
        </div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-line">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-line text-[11px] text-faint">
                <th className="px-3 py-2 font-medium">时间</th>
                <th className="px-3 py-2 font-medium">任务</th>
                <th className="px-3 py-2 font-medium">目标</th>
                <th className="px-3 py-2 font-medium">触发</th>
                <th className="px-3 py-2 font-medium">状态</th>
                <th className="px-3 py-2 font-medium">耗时</th>
                <th className="px-3 py-2 font-medium">摘要</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.map((r) => {
                const meta = statusMeta(r.status);
                const clickable = runClickable(r);
                const dur =
                  r.started_at && r.finished_at ? fmtDuration(r.finished_at - r.started_at) : r.started_at ? "…" : "—";
                return (
                  <tr
                    key={r.run_id}
                    onClick={() => openRun(r)}
                    className={`border-b border-line/60 last:border-0 ${
                      clickable ? "cursor-pointer transition-colors hover:bg-card2/60" : ""
                    }`}
                    title={clickable ? "点击查看执行回放" : r.status === "missed" ? "错过（机器睡眠或应用未运行）" : undefined}
                  >
                    <td className="whitespace-nowrap px-3 py-2 text-muted tabular-nums">
                      {fmtDayPrefix(r.scheduled_at ?? r.started_at)} {fmtClock(r.started_at ?? r.scheduled_at)}
                    </td>
                    <td className="max-w-[160px] truncate px-3 py-2 text-txt" title={r.task_name}>
                      {r.task_name}
                    </td>
                    <td className="px-3 py-2">{r.target_type === "workflow" ? "⚡" : "💬"}</td>
                    <td className="px-3 py-2 text-faint">{r.trigger === "manual" ? "手动" : "计划"}</td>
                    <td className="whitespace-nowrap px-3 py-2">
                      <span
                        className="inline-flex items-center gap-1 rounded-full border px-1.5 py-px"
                        style={{
                          borderColor: meta.hollow ? undefined : meta.color + "55",
                          color: meta.color,
                          ...(meta.hollow ? { borderStyle: "dashed" as const } : {}),
                        }}
                        title={r.status === "missed" ? "错过（机器睡眠或应用未运行）" : r.error || undefined}
                      >
                        {meta.glyph} {meta.label}
                      </span>
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-faint tabular-nums">{dur}</td>
                    <td className="max-w-[280px] px-3 py-2 text-muted">
                      <div className="truncate" title={r.summary ?? undefined}>
                        {r.summary ?? (r.error ? <span className="text-red">{r.error}</span> : "—")}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* 50 行/页分页（§3.5，与请求日志一致） */}
      {pages > 1 && (
        <div className="mt-2 flex items-center justify-end gap-2 text-xs text-muted">
          <button
            disabled={page === 0}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            className="rounded-lg border border-line px-2 py-1 disabled:opacity-40"
          >
            上一页
          </button>
          <span className="text-faint tabular-nums">
            {page + 1} / {pages}
          </span>
          <button
            disabled={page >= pages - 1}
            onClick={() => setPage((p) => Math.min(pages - 1, p + 1))}
            className="rounded-lg border border-line px-2 py-1 disabled:opacity-40"
          >
            下一页
          </button>
        </div>
      )}
    </div>
  );
}
