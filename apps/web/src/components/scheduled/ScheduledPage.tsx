"use client";

// 定时任务页（scheduled-tasks-design.md §3.1）：头部（总开关 / 保持唤醒 /
// 下次执行）→ 当天时间条 → 页签（任务 | 执行记录）。页面打开期间订阅
// /api/ws/schedule（run_started/run_finished/task_updated/missed → 刷新），
// 关页即断，不轮询（§6）。

import { useCallback, useEffect, useRef, useState } from "react";
import { AlarmClock, Loader2, RefreshCw } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { ScheduleConfig, ScheduleTask } from "@/lib/types";
import { setKeepAwake } from "@/lib/desktop";
import { DayTimeline } from "./DayTimeline";
import { RunsPanel } from "./RunsPanel";
import { UnseenBadge } from "./UnseenBadge";
import { TasksPanel } from "./TasksPanel";
import { TaskEditor } from "./TaskEditor";
import { dateStr, fmtClock, fmtDayPrefix, MAX_AUTO_RETRIES, nextRetryDelayMs } from "./shared";

export function ScheduledPage() {
  const g = useGinno();
  const tr = useTranslations("sched");
  const [cfg, setCfg] = useState<ScheduleConfig | null>(null);
  // 首载成功标记：true 后 GET 失败保留旧数据（不再进入重试态）。ref 供稳定
  // 的 refresh 回调读取（避免 loaded 进依赖导致首载成功后重复拉取）。
  const loadedRef = useRef(false);
  // 首载失败重试（schedule-integrity）：初次 GET 错误**只重试**——不弹
  // 「runtime 挂了」式断言（可能只是 sidecar 正在启动/瞬时网络抖动），期间
  // 配置未知（cfg=null），所有修改开关一律锁住，防止对着猜测状态误操作。
  // 计数器驱动有界退避（见 shared.nextRetryDelayMs）：0=未在重试，>MAX=
  // 自动重试已放弃（只留手动按钮）。旧布尔版在 retrying 已 true 时第二
  // 次失败 setRetrying(true) 是 no-op，setTimeout 只排过一次——「无限重试」
  // 从未成立（复核缺口③）；计数器每次 +1 必然重排 effect。
  const [retryAttempt, setRetryAttempt] = useState(0);
  const retrying = retryAttempt > 0;
  const retryGaveUp = retryAttempt > MAX_AUTO_RETRIES;
  // 已成功过但最近一次刷新失败（WS 断开/瞬时故障）：保留旧数据渲染，
  // 但必须亮 stale 横幅——静默降级会让用户把过期 tasks 当现值操作。
  const [stale, setStale] = useState(false);
  // GET 乱序守卫（复核缺口③）：WS 事件/手动刷新/退避定时器可能并发发起
  // listSchedule，慢的旧响应后到会覆盖新响应。每次请求自增序号，只接受
  // 最新一代的结果/错误。
  const reqSeq = useRef(0);
  const [busy, setBusy] = useState(false);
  // 全局开关写入失败（断连/5xx）的行内反馈：挂在头部开关下，可对账。
  const [globalError, setGlobalError] = useState<string | null>(null);
  const [tab, setTab] = useState<"tasks" | "runs">("tasks");
  const [editing, setEditing] = useState<ScheduleTask | "new" | null>(null);
  const [timelineDate, setTimelineDate] = useState(() => dateStr(new Date()));
  // 时间条点击某天 → 执行记录页签带单日过滤跳入（§3.5）。
  const [runsDateFilter, setRunsDateFilter] = useState<string | null>(null);
  // run-now 回执丢失的「查看此任务运行记录」入口（复核缺口④）：带 task
  // 过滤跳进执行记录页签——对账路径必须可点击，不能只写在文案里。
  const [runsTaskFilter, setRunsTaskFilter] = useState<{ id: string; name: string } | null>(null);
  // WS/操作后的统一刷新号：RunsPanel/DayTimeline 以它为依赖重拉。
  const [refreshKey, setRefreshKey] = useState(0);
  // keep_awake 联动提示（§3.3 不强制）：本次会话内可关掉。
  const [hintDismissed, setHintDismissed] = useState(false);
  const bump = useCallback(() => setRefreshKey((n) => n + 1), []);

  const refresh = useCallback(async () => {
    const seq = ++reqSeq.current;
    try {
      const r = await api.listSchedule();
      // 过期响应：已有更新的请求发起，本次结果作废（防慢响应覆盖新数据）。
      if (seq !== reqSeq.current) return;
      setCfg(r);
      loadedRef.current = true;
      setRetryAttempt(0);
      setStale(false);
      setGlobalError(null);
    } catch {
      if (seq !== reqSeq.current) return;
      // 从未成功过 → 进入有界退避（见下），不把「连不上」渲染成结论；
      // 已成功过 → 保留上次数据 + stale 标记（不静默降级）。
      if (!loadedRef.current) setRetryAttempt((n) => n + 1);
      else setStale(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 首载失败的自动重试（复核缺口③修正）：计数器驱动退避——1s 起指数翻倍、
  // 30s 封顶、连续 MAX_AUTO_RETRIES 次失败后停止（文案如实说明「已停止」，
  // 留手动按钮）。组件卸载即停。开销可忽略（一条 GET /api/schedule）。
  useEffect(() => {
    const delay = nextRetryDelayMs(retryAttempt);
    if (delay <= 0) return;
    const t = setTimeout(() => void refresh(), delay);
    return () => clearTimeout(t);
  }, [retryAttempt, refresh]);

  // 事件通道（§5.1）：四种事件都落到「重拉配置 + 时间条 + 记录」。断线静默
  // ——刷新按钮与操作后的显式重拉兜底。
  useEffect(() => {
    let ws: WebSocket | null = null;
    try {
      ws = api.openSocket(api.wsScheduleUrl());
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
          msg.type === "missed" ||
          msg.type === "runs_seen"
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
    setGlobalError(null);
    try {
      const r = await api.putSchedule(patch);
      if (r?.tasks) setCfg(r);
      else void refresh(); // 返回体不带全量配置时退回重拉
      bump();
      // keep_awake 的壳同步在 PUT 成功之后（§3.3：先 runtime 后壳）；纯 web /
      // dev 无 Tauri 时 setKeepAwake 直接返回 false，优雅降级。
      if (patch.keep_awake !== undefined) void setKeepAwake(patch.keep_awake);
    } catch (e) {
      // 写失败必须可见（schedule-integrity）：吞错会让用户以为开关已切换，
      // 而磁盘状态未变。行内红条 + 保留当前显示（以重拉对账为准）。
      const msg = e instanceof TypeError ? tr("tasks.netError") : e instanceof Error && e.message ? e.message : "";
      setGlobalError(tr("page.globalWriteFailed", { error: msg }));
      void refresh();
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
        const sec = Math.max(0, Math.round(nextRun.at - Date.now() / 1000));
        if (sec < 60) return tr("page.inSec", { n: sec });
        if (sec < 3600) return tr("page.inMin", { n: Math.floor(sec / 60) });
        if (sec < 86400) return tr("page.inHour", { n: Math.floor(sec / 3600) });
        return tr("page.inDay", { n: Math.floor(sec / 86400) });
      })()
    : null;

  // 调度租约防呆（scheduler-standby）：本实例抢不到 flock 时定时任务全部
  // 静默不执行。正常场景（dev + 应用并开）由另一实例跑，短时 standby 不扰
  // 民；超过 5 分钟仍 standby → 高概率是残留进程持锁（2026-10-20 实例：dev
  // sidecar 孤儿持锁 6 天），挂横幅点名 holder_pid。nowSec 只为跨过 5 分钟
  // 阈值时重渲染。
  const standby = cfg?.scheduler?.lease === "standby" ? cfg.scheduler : null;
  const [nowSec, setNowSec] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!standby) return;
    const t = setInterval(() => setNowSec(Date.now() / 1000), 30000);
    return () => clearInterval(t);
  }, [standby]);
  const standbyStale =
    !!standby &&
    (standby.standby_since == null || nowSec - standby.standby_since > 300);

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
      // 配置未知（cfg=null：首载/重试中）时锁住所有修改开关（schedule-integrity）：
      // 显示的 on/off 是猜测值，此时点击会把猜测写进 runtime。
      disabled={busy || !cfg}
      title={title}
      className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
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
        <h1 className="text-lg font-semibold text-txt">{tr("page.title")}</h1>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {toggle(
            !!cfg?.enabled,
            () => void putGlobal({ enabled: !cfg?.enabled }),
            tr("page.enabled"),
            cfg?.enabled ? tr("page.enabledOnTitle") : tr("page.enabledOffTitle"),
          )}
          {toggle(
            !!cfg?.keep_awake,
            () => void putGlobal({ keep_awake: !cfg?.keep_awake }),
            tr("page.keepAwake"),
            tr("page.keepAwakeTitle"),
          )}
          {nextRun && (
            <span className="text-xs text-faint" title={tr("page.nextTitle")}>
              {tr("page.next", {
                day: fmtDayPrefix(nextRun.at),
                clock: fmtClock(nextRun.at),
                name: nextRun.name,
              })}
              {nextIn ? ` (${nextIn})` : ""}
            </span>
          )}
          <button
            onClick={() => {
              void refresh();
              bump();
            }}
            title={tr("page.refresh")}
            className="rounded-md p-1.5 text-faint hover:bg-card hover:text-txt"
          >
            <RefreshCw className="h-4 w-4" />
          </button>
        </div>
      </header>

      {/* 调度租约防呆横幅：standby 超 5 分钟（scheduler-standby）。 */}
      {standbyStale && (
        <div className="mb-3 rounded-lg border border-red/40 bg-red/10 px-3 py-2 text-xs leading-relaxed text-red">
          {tr("page.standbyBanner", {
            pid: standby?.holder_pid != null ? `PID ${standby.holder_pid}` : tr("page.standbyUnknownHolder"),
          })}
        </div>
      )}

      {/* 保持唤醒能力边界的小字（§3.3 文案诚实） */}
      <div className="mb-3 text-[11px] leading-relaxed text-faint">
        {tr("page.keepAwakeHint")}
        {!cfg?.keep_awake && tr("page.keepAwakeHintOff")}
      </div>

      {/* 联动提示（§3.3，不强制）：有任务且全局开但 keep_awake 关。 */}
      {showKeepAwakeHint && (
        <div className="mb-3 flex items-center gap-2 rounded-lg border border-yellow/40 bg-yellow/10 px-3 py-2 text-xs text-yellow">
          <span className="flex-1">{tr("page.keepAwakeNudge")}</span>
          <button
            onClick={() => void putGlobal({ keep_awake: true })}
            className="rounded-md bg-yellow/20 px-2 py-1 text-[11px] font-medium text-yellow hover:bg-yellow/30"
          >
            {tr("page.enable")}
          </button>
          <button
            onClick={() => setHintDismissed(true)}
            aria-label={tr("page.dismiss")}
            className="rounded-md p-0.5 text-yellow/70 hover:text-yellow"
          >
            ✕
          </button>
        </div>
      )}

      {/* 全局开关写失败的行内反馈（schedule-integrity）：红条可对账，重拉成功即清。 */}
      {globalError && (
        <div className="mb-3 rounded-lg border border-red/40 bg-red/10 px-3 py-2 text-xs leading-relaxed text-red">
          {globalError}
        </div>
      )}

      {!cfg ? (
        <div className="flex flex-wrap items-center gap-3 py-10 text-sm text-faint">
          <Loader2 className="h-4 w-4 animate-spin" />
          {/* 首载/重试中：绝不渲染「runtime 挂了」式断言——GET 错误只重试
              （schedule-integrity）。退避放弃后如实说明停止状态，手动按钮
              给用户一条立即重试路径（点击会重置退避计数，重新开始一轮）。 */}
          <span>{retryGaveUp ? tr("page.loadRetryStopped") : retrying ? tr("page.loadRetrying") : tr("page.loading")}</span>
          {(retrying || retryGaveUp) && (
            <button
              onClick={() => {
                setRetryAttempt(0);
                void refresh();
              }}
              className="rounded-md border border-line px-2 py-1 text-xs text-muted transition-colors hover:border-line2 hover:text-txt"
            >
              {tr("page.retryNow")}
            </button>
          )}
        </div>
      ) : (
        <>
          {/* 刷新失败但已有旧数据（复核缺口③）：stale 横幅必须可见——
              静默降级会让用户把过期 tasks 当现值操作。重试按钮就地可点。 */}
          {stale && (
            <div
              data-testid="schedule-stale-banner"
              className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-yellow/40 bg-yellow/10 px-3 py-2 text-xs leading-relaxed text-yellow"
            >
              <span className="flex-1">{tr("page.staleData")}</span>
              <button
                onClick={() => {
                  setRetryAttempt(0);
                  void refresh();
                }}
                className="rounded-md bg-yellow/20 px-2 py-1 text-[11px] font-medium text-yellow hover:bg-yellow/30"
              >
                {tr("page.retryNow")}
              </button>
            </div>
          )}
          <DayTimeline
            date={timelineDate}
            onDateChange={(d) => setTimelineDate(d)}
            refreshKey={refreshKey}
            onOpenDayRuns={() => {
              setRunsDateFilter(timelineDate);
              setTab("runs");
            }}
          />

          {/* 页签（任务 | 执行记录）。执行记录带未读徽章（schedule-unseen）：
              实心红圆 + 白色数字（与侧栏同一形），后随 error / missed 分桶
              小字——一眼看出有多少结果没看、其中多少是坏的。全部已读后消失。 */}
          <div className="mb-3 mt-5 flex items-center gap-1 border-b border-line">
            {(
              [
                [
                  "tasks",
                  cfg.tasks.length ? tr("page.tabTasksCount", { count: cfg.tasks.length }) : tr("page.tabTasks"),
                ],
                ["runs", tr("page.tabRuns")],
              ] as Array<["tasks" | "runs", string]>
            ).map(([k, label]) => {
              const unseen = k === "runs" ? cfg.unseen : undefined;
              return (
                <button
                  key={k}
                  onClick={() => {
                    setTab(k);
                    if (k === "tasks") setRunsDateFilter(null);
                  }}
                  className={`-mb-px flex items-center gap-1.5 border-b-2 px-3 py-1.5 text-sm transition-colors ${
                    tab === k ? "border-violet text-txt" : "border-transparent text-faint hover:text-muted"
                  }`}
                >
                  {label}
                  {!!unseen && unseen.total > 0 && (
                    <span className="flex items-center gap-1.5 text-[11px] font-normal">
                      <UnseenBadge
                        n={unseen.total}
                        tone={unseen.error > 0 || unseen.missed > 0 ? "red" : "violet"}
                      />
                      {unseen.error > 0 && (
                        <span className="text-red/70" title={tr("status.error")}>
                          ✕{unseen.error}
                        </span>
                      )}
                      {unseen.missed > 0 && (
                        <span className="text-faint" title={tr("status.missed")}>
                          ◌{unseen.missed}
                        </span>
                      )}
                    </span>
                  )}
                </button>
              );
            })}
          </div>

          {tab === "tasks" ? (
            <TasksPanel
              cfg={cfg}
              onReload={() => { void refresh(); bump(); }}
              onEdit={setEditing}
              onOpenTaskRuns={(id, name) => {
                setRunsTaskFilter({ id, name });
                setRunsDateFilter(null); // run-now 对账看该任务全部记录，不吃单日过滤
                setTab("runs");
              }}
            />
          ) : (
            <RunsPanel
              cfg={cfg}
              refreshKey={refreshKey}
              dateFilter={runsDateFilter}
              onClearDateFilter={() => setRunsDateFilter(null)}
              taskFilter={runsTaskFilter}
              onClearTaskFilter={() => setRunsTaskFilter(null)}
              onSeenChange={() => {
                void refresh();
                bump();
              }}
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
