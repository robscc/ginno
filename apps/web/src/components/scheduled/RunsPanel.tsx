"use client";

// 执行记录面板（scheduled-tasks-design.md §3.5）：时间升/降序切换 + 任务/
// 目标/状态过滤 + 日期范围（近 1/7/30 天，客户端过滤）+ 50 行/页分页。
// 行点击按目标分流进回放（prompt → 影子会话回放；workflow → run 视图），
// missed/skipped 行不可点。

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowDown, ArrowUp, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
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

export function RunsPanel({
  cfg,
  refreshKey,
  dateFilter,
  onClearDateFilter,
  onSeenChange,
}: {
  cfg: ScheduleConfig;
  refreshKey: number;
  /** 时间条点进来的单日过滤（YYYY-MM-DD）；null = 用范围选择。 */
  dateFilter: string | null;
  onClearDateFilter: () => void;
  /** 已读状态变化（单条/全部）：父级重拉 cfg（页签角标）+ bump 本面板。 */
  onSeenChange: () => void;
}) {
  const g = useGinno();
  const tr = useTranslations("sched");
  // 范围选项（catalog 驱动；原模块级英文常量迁入 sched.runs.range*）。
  const rangeOptions: Array<[string, string]> = [
    ["1", tr("runs.range1d")],
    ["7", tr("runs.range7d")],
    ["30", tr("runs.range30d")],
  ];
  // workflow 目标的回放是工作区路由里的 RunSubSessionView——从本页点开要推回 "/"。
  const router = useRouter();
  const [rows, setRows] = useState<ScheduleRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [sort, setSort] = useState<"desc" | "asc">("desc");
  const [taskId, setTaskId] = useState("");
  const [targetType, setTargetType] = useState("");
  const [status, setStatus] = useState("");
  const [seen, setSeen] = useState<"" | "unseen" | "seen">("");
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
            seen: seen || undefined,
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
  }, [taskId, targetType, status, seen, sort, dateFilter, refreshKey]);

  // 日期范围（近 N 天）客户端过滤；dateFilter 模式下服务端已按单日过滤。
  const filtered = useMemo(() => {
    if (dateFilter) return rows;
    const cutoff = Date.now() / 1000 - Number(range) * 86400;
    return rows.filter((r) => (r.scheduled_at ?? r.started_at ?? 0) >= cutoff);
  }, [rows, range, dateFilter]);

  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageRows = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  // schedule-unseen：点开一条结果 = 已读。可点行进回放；missed/skipped 没有
  // 回放可进，点击仅消红点（否则错过记录永远无法标已读）。已读先本地乐观
  // 置位（行红点立即消失），父级 onSeenChange 刷新页签角标，侧栏红点由
  // runs_seen WS 事件即时消除。
  const markOne = (r: ScheduleRun) => {
    if (r.seen !== false) return;
    setRows((prev) => prev.map((x) => (x.run_id === r.run_id ? { ...x, seen: true } : x)));
    void api.markScheduleRunsSeen({ run_id: r.run_id }).then(() => onSeenChange());
  };

  const markAll = () => {
    setRows((prev) => prev.map((x) => ({ ...x, seen: true })));
    void api.markScheduleRunsSeen({ all: true }).then(() => onSeenChange());
  };

  const openRun = (r: ScheduleRun) => {
    if (!runClickable(r)) {
      markOne(r); // missed/skipped：无回放，点击只消红点
      return;
    }
    markOne(r);
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
          title={tr("runs.sortTitle")}
        >
          {tr("runs.time")} {sort === "desc" ? <ArrowDown className="h-3 w-3" /> : <ArrowUp className="h-3 w-3" />}
        </button>
        <select className="field w-auto px-2 py-1 text-xs" value={taskId} onChange={(e) => setTaskId(e.target.value)}>
          <option value="">{tr("runs.taskAll")}</option>
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
          <option value="">{tr("runs.targetAll")}</option>
          <option value="prompt">{tr("runs.targetPrompt")}</option>
          <option value="workflow">{tr("runs.targetWorkflow")}</option>
        </select>
        <select className="field w-auto px-2 py-1 text-xs" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">{tr("runs.statusAll")}</option>
          <option value="ok">{tr("status.ok")}</option>
          <option value="error">{tr("status.error")}</option>
          <option value="running">{tr("status.running")}</option>
          <option value="missed">{tr("status.missed")}</option>
          <option value="skipped_overlap">{tr("status.skipped")}</option>
        </select>
        {/* 已读过滤（schedule-unseen）——与状态过滤正交，可组合 */}
        <select
          className="field w-auto px-2 py-1 text-xs"
          value={seen}
          onChange={(e) => setSeen(e.target.value as "" | "unseen" | "seen")}
        >
          <option value="">{tr("runs.seenAll")}</option>
          <option value="unseen">{tr("runs.seenUnseen")}</option>
          <option value="seen">{tr("runs.seenSeen")}</option>
        </select>
        {(cfg.unseen?.total ?? 0) > 0 && (
          <button
            onClick={markAll}
            title={tr("runs.markAllSeenTitle")}
            className={`flex items-center gap-1 rounded-lg border px-2 py-1 transition-colors ${
              (cfg.unseen?.error ?? 0) + (cfg.unseen?.missed ?? 0) > 0
                ? "border-red/40 text-red hover:bg-red/10"
                : "border-violet/40 text-violet hover:bg-violet/10"
            }`}
          >
            ● {tr("runs.markAllSeen", { n: cfg.unseen?.total ?? 0 })}
          </button>
        )}
        {dateFilter ? (
          <span className="flex items-center gap-1.5 rounded-lg border border-violet/50 bg-violet/10 px-2 py-1 text-violet">
            {dateFilter}
            <button onClick={onClearDateFilter} title={tr("runs.clearDate")} className="text-violet/70 hover:text-violet">
              ✕
            </button>
          </span>
        ) : (
          <select className="field w-auto px-2 py-1 text-xs" value={range} onChange={(e) => setRange(e.target.value)}>
            {rangeOptions.map(([v, label]) => (
              <option key={v} value={v}>
                {label}
              </option>
            ))}
          </select>
        )}
        <span className="ml-auto whitespace-nowrap text-[11px] text-faint">{tr("runs.count", { n: filtered.length })}</span>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 py-10 text-sm text-faint">
          <Loader2 className="h-4 w-4 animate-spin" /> {tr("runs.loading")}
        </div>
      ) : pageRows.length === 0 ? (
        <div className="rounded-xl border border-dashed border-line py-10 text-center text-sm text-faint">
          {tr("runs.empty")}
        </div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-line">
          {/* table-fixed：列宽由表头定死、表总宽恒等于容器宽——摘要列吃剩余
              宽度并 truncate，任何窗口宽度下都不再出现横向滚动条（GINNO-6）。
              之前 auto 布局下各列宽度上限之和 ≈ 820px（时间~108 + 任务160 +
              目标48 + 触发48 + 状态~110 + 耗时~64 + 摘要304），远超页面列的
              可用宽 720px（max-w-3xl 768 − px-6×2），min-w-[700px] 的下限根本
              不起约束作用，默认宽度下也稳定溢出 ~100px 出滚动条。窄容器时
              各固定列宽不变、摘要先被压缩，极端窄才可能溢出（wrapper 兜底
              横向滚动）。「打开」hover 入口在摘要列内 shrink-0，不受挤压。 */}
          <table className="w-full table-fixed text-left text-xs">
            <thead>
              <tr className="whitespace-nowrap border-b border-line text-[11px] text-faint">
                <th className="w-[108px] px-3 py-2 font-medium">{tr("runs.colTime")}</th>
                <th className="w-[112px] px-3 py-2 font-medium">{tr("runs.colTask")}</th>
                <th className="w-10 px-3 py-2 font-medium">{tr("runs.colTarget")}</th>
                <th className="w-12 px-3 py-2 font-medium">{tr("runs.colTrigger")}</th>
                <th className="w-[128px] px-3 py-2 font-medium">{tr("runs.colStatus")}</th>
                <th className="w-16 px-3 py-2 font-medium">{tr("runs.colDuration")}</th>
                <th className="px-3 py-2 font-medium">{tr("runs.colSummary")}</th>
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
                    className={`group border-b border-line/60 last:border-0 ${
                      clickable ? "cursor-pointer transition-colors hover:bg-card2/60" : ""
                    }`}
                    title={clickable ? tr("runs.rowClick") : r.status === "missed" ? tr("runs.rowMissed") : undefined}
                  >
                    <td className="overflow-hidden whitespace-nowrap px-3 py-2 text-muted tabular-nums">
                      {/* 未读点（schedule-unseen）：颜色跟状态走——红=错误、
                          黄=错过、紫=成功/跳过；点开该行即消除 */}
                      {r.seen === false && (
                        <span
                          className={`mr-1.5 inline-block h-1.5 w-1.5 rounded-full align-middle ${
                            r.status === "error" ? "bg-red" : r.status === "missed" ? "bg-yellow" : "bg-violet"
                          }`}
                        />
                      )}
                      {fmtDayPrefix(r.scheduled_at ?? r.started_at)} {fmtClock(r.started_at ?? r.scheduled_at)}
                    </td>
                    <td className="overflow-hidden px-3 py-2 text-txt" title={r.task_name}>
                      <div className="truncate">{r.task_name}</div>
                    </td>
                    <td className="px-3 py-2">{r.target_type === "workflow" ? "⚡" : "💬"}</td>
                    <td className="overflow-hidden whitespace-nowrap px-3 py-2 text-faint">{r.trigger === "manual" ? tr("trigger.manual") : r.trigger === "catch_up" ? tr("trigger.catchUp") : tr("trigger.scheduled")}</td>
                    <td className="overflow-hidden whitespace-nowrap px-3 py-2">
                      <span
                        className="inline-flex items-center gap-1 rounded-full border px-1.5 py-px"
                        style={{
                          borderColor: meta.hollow ? undefined : meta.color,
                          color: meta.color,
                          ...(meta.hollow ? { borderStyle: "dashed" as const } : {}),
                        }}
                        title={r.status === "missed" ? tr("runs.rowMissed") : r.error || undefined}
                      >
                        {meta.glyph} {meta.label}
                      </span>
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-faint tabular-nums">{dur}</td>
                    <td className="overflow-hidden px-3 py-2 text-muted">
                      <div className="flex items-center gap-1.5">
                        <div className="min-w-0 flex-1 truncate" title={r.summary ?? undefined}>
                          {r.summary ?? (r.error ? <span className="text-red">{r.error}</span> : "—")}
                        </div>
                        {/* 显式回放入口：hover 才出现，让「整行可点」可被发现 */}
                        {clickable && (
                          <span className="shrink-0 rounded-md border border-violet/40 px-1.5 py-px text-[10px] text-violet opacity-0 transition-opacity group-hover:opacity-100">
                            {tr("runs.open")}
                          </span>
                        )}
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
            {tr("runs.prev")}
          </button>
          <span className="text-faint tabular-nums">
            {page + 1} / {pages}
          </span>
          <button
            disabled={page >= pages - 1}
            onClick={() => setPage((p) => Math.min(pages - 1, p + 1))}
            className="rounded-lg border border-line px-2 py-1 disabled:opacity-40"
          >
            {tr("runs.next")}
          </button>
        </div>
      )}
    </div>
  );
}
