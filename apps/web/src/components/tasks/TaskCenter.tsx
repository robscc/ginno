"use client";

/**
 * 任务中心（background-tasks-design.md §6.2，实现形态改为路由页——仓库全局
 * 视图的惯例，见设计文档 §12 偏差记录）。Running 置顶 + Recent 分组；选中
 * 任务展开日志视图（offset 增量跟随）。无 New Task 入口（评审决策 3）。
 *
 * 打开期间 2s 轮询（任务天然跨 session，无 per-task WS 可订阅）。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Loader2, RefreshCw, Square, TerminalSquare, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import {
  listTasks,
  removeTask,
  stopTask,
  taskLogs,
  TASK_TERMINAL,
  type TaskRecord,
} from "@/lib/runtime";

function fmtElapsed(startedAt: number | null | undefined): string {
  if (!startedAt) return "";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - startedAt));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${String(sec).padStart(2, "0")}s`;
  return `${sec}s`;
}

function statusColor(t: TaskRecord): string {
  if (t.status === "running") return "var(--st-running)";
  if (t.status === "finished") return t.exit_code === 0 ? "var(--st-ok)" : "var(--st-error)";
  if (t.status === "interrupted" || t.status === "timeout") return "var(--st-missed)";
  return "var(--st-skipped)";
}

// 非 hook 场景下 tr 的宽松类型（与 blocks/i18n.ts 的 cast 一致）：避免把
// ReturnType<typeof useTranslations> 穿过 props 导致过度类型实例化（TS2589）。
type Tr = (key: string, values?: Record<string, string | number>) => string;

function statusLabel(tr: Tr, t: TaskRecord): string {
  if (t.status === "finished") return t.exit_code === 0 ? tr("exit0") : tr("exitN", { n: t.exit_code ?? "?" });
  return tr(t.status === "running" ? "running" : t.status === "stopped" ? "stopped"
    : t.status === "timeout" ? "timeout" : t.status === "interrupted" ? "interrupted" : "failedStart");
}

/** 单任务行 + 展开的日志视图（跟随到底部，offset 增量拉取）。 */
function TaskRow({ task, selected, onSelect, tr }: {
  task: TaskRecord; selected: boolean; onSelect: () => void;
  tr: Tr;
}) {
  const [lines, setLines] = useState<string[]>([]);
  const [offset, setOffset] = useState(-1);
  const [follow, setFollow] = useState(true);
  const [busy, setBusy] = useState(false);
  const logRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    if (!selected) return;
    let stop = false;
    const pull = async () => {
      try {
        const res = await taskLogs(task.id, offset < 0 ? { tail: 200 } : { offset });
        if (stop) return;
        if (offset < 0) {
          setLines(res.lines);
          setOffset(res.total);
        } else if (res.lines.length) {
          setLines((prev) => [...prev, ...res.lines].slice(-2000));
          setOffset(res.total);
        }
      } catch { /* transient */ }
    };
    void pull();
    const id = task.status === "running" ? setInterval(pull, 2000) : undefined;
    return () => { stop = true; if (id) clearInterval(id); };
  }, [selected, task.status, task.id, offset]);

  useEffect(() => {
    if (follow && logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [lines, follow]);

  const running = task.status === "running";

  return (
    <div className={`mb-1.5 overflow-hidden rounded-xl border bg-card ${selected ? "border-violet" : "border-line"}`}>
      <button onClick={onSelect} className="flex w-full items-center gap-2.5 px-3 py-2 text-left">
        {selected ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-faint" /> : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-faint" />}
        <span
          className={`h-2 w-2 shrink-0 rounded-full ${running ? "animate-pulse" : ""}`}
          style={{ background: statusColor(task) }}
        />
        <div className="min-w-0 flex-1">
          <div className="truncate text-[13px] font-semibold">{task.title || task.id}</div>
          <div className="truncate font-mono text-[11px] text-faint">$ {task.command}</div>
        </div>
        <div className="shrink-0 text-right">
          <div className="font-mono text-[10.5px] tabular-nums text-faint">
            {running ? fmtElapsed(task.started_at) : task.dur || ""}
          </div>
          <div className="text-[10.5px]" style={{ color: statusColor(task) }}>{statusLabel(tr, task)}</div>
        </div>
      </button>
      {selected && (
        <div className="border-t border-line">
          <pre ref={logRef} className="max-h-56 overflow-y-auto whitespace-pre-wrap break-all bg-base/40 px-3 py-2 font-mono text-[10.5px] leading-relaxed text-muted">
            {lines.length ? lines.join("\n") : tr("noOutput")}
          </pre>
          <div className="flex items-center gap-2 px-3 py-2">
            {running ? (
              <button
                disabled={busy}
                onClick={async () => { setBusy(true); try { await stopTask(task.id); } catch {} setBusy(false); }}
                className="inline-flex items-center gap-1 rounded-md border border-line2 px-2 py-1 text-[11px] text-muted hover:border-rose-500 hover:text-rose-400 disabled:opacity-50"
              >
                <Square className="h-3 w-3" /> {tr("stop")}
              </button>
            ) : (
              <button
                disabled={busy}
                onClick={async () => { setBusy(true); try { await removeTask(task.id); } catch {} setBusy(false); }}
                className="inline-flex items-center gap-1 rounded-md border border-line2 px-2 py-1 text-[11px] text-muted hover:border-rose-500 hover:text-rose-400 disabled:opacity-50"
              >
                <Trash2 className="h-3 w-3" /> {tr("remove")}
              </button>
            )}
            <label className="ml-auto flex cursor-pointer select-none items-center gap-1.5 text-[11px] text-muted">
              <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} className="accent-violet" />
              {tr("follow")}
            </label>
            <span className="font-mono text-[10.5px] text-faint">{task.id} · {((task.log_bytes || 0) / 1024).toFixed(0)} KB</span>
          </div>
        </div>
      )}
    </div>
  );
}

export function TaskCenter() {
  const tr = useTranslations("tasks");
  // 穿 props 的宽松形态（与 blocks/i18n.ts 的 cast 同理）：严格 Translator 的
  // 字面量 key 参数无法逆变赋给 Tr，转型一次。
  const trLoose = tr as unknown as Tr;
  const [tasks, setTasks] = useState<TaskRecord[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setTasks(await listTasks());
    } catch {
      /* sidecar 未起——保留上次数据 */
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = setInterval(refresh, 2000);
    return () => clearInterval(id);
  }, [refresh]);

  const running = (tasks || []).filter((t) => !TASK_TERMINAL.has(t.status));
  const recent = (tasks || []).filter((t) => TASK_TERMINAL.has(t.status));

  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
      <div className="mb-4 flex items-center gap-2">
        <TerminalSquare className="h-5 w-5 text-violet" />
        <h1 className="text-lg font-semibold text-txt">{tr("title")}</h1>
        <span className="rounded-full border border-line bg-card px-2 py-0.5 text-[11px] text-muted">
          {running.length} / {tasks?.length ?? 0}
        </span>
        <button onClick={() => void refresh()} className="ml-auto rounded-md p-1.5 text-faint hover:text-txt" title={tr("refresh")}>
          <RefreshCw className="h-4 w-4" />
        </button>
      </div>

      {!tasks ? (
        <div className="flex items-center gap-2 py-16 text-sm text-faint">
          <Loader2 className="h-4 w-4 animate-spin" /> {tr("loading")}
        </div>
      ) : tasks.length === 0 ? (
        <div className="py-16 text-center text-sm text-faint">{tr("empty")}</div>
      ) : (
        <>
          {running.length > 0 && (
            <>
              <div className="px-1 pb-1.5 pt-2 text-[11px] font-semibold uppercase tracking-wider text-faint">
                {tr("runningGroup")} · {running.length}
              </div>
              {running.map((t) => (
                <TaskRow key={t.id} task={t} tr={trLoose} selected={selected === t.id} onSelect={() => setSelected(selected === t.id ? null : t.id)} />
              ))}
            </>
          )}
          {recent.length > 0 && (
            <>
              <div className="px-1 pb-1.5 pt-4 text-[11px] font-semibold uppercase tracking-wider text-faint">{tr("recentGroup")}</div>
              {recent.map((t) => (
                <TaskRow key={t.id} task={t} tr={trLoose} selected={selected === t.id} onSelect={() => setSelected(selected === t.id ? null : t.id)} />
              ))}
            </>
          )}
        </>
      )}
    </div>
  );
}
