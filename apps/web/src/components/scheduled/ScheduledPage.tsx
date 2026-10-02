"use client";

// 定时任务页（scheduled-tasks-design.md §3.1）：头部（总开关 / 保持唤醒 /
// 下次执行）→ 当天时间条 → 页签（任务 | 执行记录）。页面打开期间订阅
// /api/ws/schedule（run_started/run_finished/task_updated/missed → 刷新），
// 关页即断，不轮询（§6）。

import { useCallback, useEffect, useRef, useState } from "react";
import { AlarmClock, Loader2, RefreshCw } from "lucide-react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { ScheduleConfig, ScheduleTask } from "@/lib/types";
import { setKeepAwake } from "@/lib/desktop";
import { DayTimeline } from "./DayTimeline";
import { RunsPanel } from "./RunsPanel";
import { TasksPanel } from "./TasksPanel";
import { TaskEditor } from "./TaskEditor";
import { dateStr, fmtClock, fmtDayPrefix } from "./shared";

export function ScheduledPage() {
  const g = useGinno();
  const [cfg, setCfg] = useState<ScheduleConfig | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<"tasks" | "runs">("tasks");
  const [editing, setEditing] = useState<ScheduleTask | "new" | null>(null);
  const [timelineDate, setTimelineDate] = useState(() => dateStr(new Date()));
  // 时间条点击某天 → 执行记录页签带单日过滤跳入（§3.5）。
  const [runsDateFilter, setRunsDateFilter] = useState<string | null>(null);
  // WS/操作后的统一刷新号：RunsPanel/DayTimeline 以它为依赖重拉。
  const [refreshKey, setRefreshKey] = useState(0);
  // keep_awake 联动提示（§3.3 不强制）：本次会话内可关掉。
  const [hintDismissed, setHintDismissed] = useState(false);
  const bump = useCallback(() => setRefreshKey((n) => n + 1), []);

  const refresh = useCallback(async () => {
    try {
      setCfg(await api.listSchedule());
    } catch {
      /* sidecar 未起——保留上次数据 */
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 事件通道（§5.1）：四种事件都落到「重拉配置 + 时间条 + 记录」。断线静默
  // ——刷新按钮与操作后的显式重拉兜底。
  useEffect(() => {
    let ws: WebSocket | null = null;
    try {
      ws = new WebSocket(api.wsScheduleUrl());
    } catch {
      return; // 预渲染环境
    }
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data) as { type?: string };
        if (
          msg.type === "run_started" ||
          msg.type === "run_finished" ||
          msg.type === "task_updated" ||
          msg.type === "missed"
        ) {
          void refresh();
          bump();
        }
      } catch {
        /* 非 JSON 帧 */
      }
    };
    return () => {
      try {
        ws?.close();
      } catch {
        /* noop */
      }
    };
  }, [refresh, bump]);

  const putGlobal = async (patch: { enabled?: boolean; keep_awake?: boolean }) => {
    setBusy(true);
    try {
      const r = await api.putSchedule(patch);
      if (r?.tasks) setCfg(r);
      else void refresh(); // 返回体不带全量配置时退回重拉
      bump();
      // keep_awake 的壳同步在 PUT 成功之后（§3.3：先 runtime 后壳）；纯 web /
      // dev 无 Tauri 时 setKeepAwake 直接返回 false，优雅降级。
      if (patch.keep_awake !== undefined) void setKeepAwake(patch.keep_awake);
    } catch {
      /* ignore */
    } finally {
      setBusy(false);
    }
  };

  // 头部「下次执行」：全局开时 enabled 任务的最近 next_run_at。
  const nextRun = (() => {
    if (!cfg?.enabled) return null;
    let best: { at: number; name: string } | null = null;
    for (const t of cfg.tasks) {
      if (!t.enabled || !t.next_run_at) continue;
      if (!best || t.next_run_at < best.at) best = { at: t.next_run_at, name: t.name };
    }
    return best;
  })();
  const nextIn = nextRun
    ? (() => {
        const s = Math.max(0, Math.round(nextRun.at - Date.now() / 1000));
        if (s < 60) return `${s}s 后`;
        if (s < 3600) return `${Math.floor(s / 60)}m 后`;
        if (s < 86400) return `${Math.floor(s / 3600)}h 后`;
        return `${Math.floor(s / 86400)}d 后`;
      })()
    : null;

  const showKeepAwakeHint =
    !!cfg?.enabled && !cfg?.keep_awake && !hintDismissed && (cfg?.tasks.length ?? 0) > 0;

  const toggle = (
    on: boolean,
    onClick: () => void,
    label: string,
    title: string,
  ) => (
    <button
      onClick={onClick}
      disabled={busy || !loaded}
      title={title}
      className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition-colors disabled:opacity-50 ${
        on ? "border-green/50 bg-green/10 text-green" : "border-line2 text-faint hover:text-muted"
      }`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${on ? "bg-green" : "bg-faint"}`} />
      {label} {on ? "●" : "○"}
    </button>
  );

  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
      <header className="mb-5 flex flex-wrap items-center gap-2">
        <AlarmClock className="h-5 w-5 text-faint" />
        <h1 className="text-lg font-semibold text-txt">Scheduled Tasks</h1>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {toggle(
            !!cfg?.enabled,
            () => void putGlobal({ enabled: !cfg?.enabled }),
            "总开关",
            cfg?.enabled ? "关闭后所有任务暂停触发" : "开启后任务按计划触发",
          )}
          {toggle(
            !!cfg?.keep_awake,
            () => void putGlobal({ keep_awake: !cfg?.keep_awake }),
            "保持唤醒",
            "防止闲置睡眠；屏幕可能变暗，合盖仍会睡眠",
          )}
          {nextRun && (
            <span className="text-xs text-faint" title="最近一个启用任务的下次执行">
              下次: {fmtDayPrefix(nextRun.at)} {fmtClock(nextRun.at)} {nextRun.name}
              {nextIn ? `（${nextIn}）` : ""}
            </span>
          )}
          <button
            onClick={() => {
              void refresh();
              bump();
            }}
            title="刷新"
            className="rounded-md p-1.5 text-faint hover:bg-card hover:text-txt"
          >
            <RefreshCw className="h-4 w-4" />
          </button>
        </div>
      </header>

      {/* 保持唤醒能力边界的小字（§3.3 文案诚实） */}
      <div className="mb-3 text-[11px] leading-relaxed text-faint">
        保持唤醒 = 阻止闲置系统睡眠（屏幕可能变暗/锁定，系统不睡）；不能阻止合盖睡眠、手动睡眠或电池耗尽。
        {!cfg?.keep_awake && " 睡眠期间到点的任务会记为「错过」，不补跑。"}
      </div>

      {/* 联动提示（§3.3，不强制）：有任务且全局开但 keep_awake 关。 */}
      {showKeepAwakeHint && (
        <div className="mb-3 flex items-center gap-2 rounded-lg border border-yellow/40 bg-yellow/10 px-3 py-2 text-xs text-yellow">
          <span className="flex-1">建议开启保持唤醒，否则睡眠期间任务会错过</span>
          <button
            onClick={() => void putGlobal({ keep_awake: true })}
            className="rounded-md bg-yellow/20 px-2 py-1 text-[11px] font-medium text-yellow hover:bg-yellow/30"
          >
            开启
          </button>
          <button
            onClick={() => setHintDismissed(true)}
            aria-label="关闭提示"
            className="rounded-md p-0.5 text-yellow/70 hover:text-yellow"
          >
            ✕
          </button>
        </div>
      )}

      {!loaded ? (
        <div className="flex items-center gap-2 py-10 text-sm text-faint">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading scheduled tasks…
        </div>
      ) : !cfg ? (
        <div className="py-10 text-sm text-faint">无法连接运行时，请确认 Ginno 正在运行。</div>
      ) : (
        <>
          <DayTimeline
            date={timelineDate}
            onDateChange={(d) => setTimelineDate(d)}
            refreshKey={refreshKey}
            onOpenDayRuns={() => {
              setRunsDateFilter(timelineDate);
              setTab("runs");
            }}
          />

          {/* 页签（任务 | 执行记录） */}
          <div className="mb-3 mt-5 flex items-center gap-1 border-b border-line">
            {(
              [
                ["tasks", `任务${cfg.tasks.length ? `（${cfg.tasks.length}）` : ""}`],
                ["runs", "执行记录"],
              ] as Array<["tasks" | "runs", string]>
            ).map(([k, label]) => (
              <button
                key={k}
                onClick={() => {
                  setTab(k);
                  if (k === "tasks") setRunsDateFilter(null);
                }}
                className={`-mb-px border-b-2 px-3 py-1.5 text-sm transition-colors ${
                  tab === k ? "border-violet text-txt" : "border-transparent text-faint hover:text-muted"
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          {tab === "tasks" ? (
            <TasksPanel cfg={cfg} onReload={() => { void refresh(); bump(); }} onEdit={setEditing} />
          ) : (
            <RunsPanel
              cfg={cfg}
              refreshKey={refreshKey}
              dateFilter={runsDateFilter}
              onClearDateFilter={() => setRunsDateFilter(null)}
            />
          )}
        </>
      )}

      {editing !== null && (
        <TaskEditor
          initial={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            void refresh();
            bump();
          }}
        />
      )}
    </div>
  );
}
