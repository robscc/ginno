"use client";

/**
 * 聊天内常驻后台任务卡（background-tasks-design.md §6.1，评审决策 4：一个
 * task_id 整条时间线只有这一张卡）。由 toolBlocks 的 bg_run 特判渲染：
 * 工具气泡不再返回，而是这张卡接管到终态之后（不随 tool.end 折叠）。
 *
 * 状态不来自 turn 事件而来自 /api/tasks 轮询：任务的生命周期横跨多个 turn
 * （甚至跨 app 重启——interrupted 卡在重启后仍要显示），流式 block 只负责
 * 把 taskId 带进来。running 时 2s 轮询 + 1s 计时；终态一次拉齐后停表。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Square, TerminalSquare, Trash2 } from "lucide-react";
import {
  getTask,
  removeTask,
  stopTask,
  taskLogs,
  TASK_TERMINAL,
  type TaskRecord,
} from "@/lib/runtime";
import { useChatT } from "./blocks/i18n";

/** bg_run 结果文本的机器头：``task=t_xxxxxxxx status=running pid=123``。 */
export function parseBgTaskId(content: string): string | null {
  const m = /\btask=(t_[0-9a-f]+)/.exec(content || "");
  return m ? m[1] : null;
}

function fmtElapsed(startedAt: number | null | undefined, now = Date.now() / 1000): string {
  if (!startedAt) return "";
  const s = Math.max(0, Math.floor(now - startedAt));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${String(sec).padStart(2, "0")}s`;
  return `${sec}s`;
}

function badgeOf(t: TaskRecord | null): { text: string; color: string; bg: string; spin?: boolean } {
  if (!t) return { text: "…", color: "var(--st-skipped)", bg: "transparent" };
  switch (t.status) {
    case "running":
      return t.watch_muted
        ? { text: "MUTED", color: "var(--st-missed)", bg: "color-mix(in srgb, var(--st-missed) 12%, transparent)" }
        : { text: "RUNNING", color: "var(--st-running)", bg: "color-mix(in srgb, var(--st-running) 12%, transparent)", spin: true };
    case "finished":
      return t.exit_code === 0
        ? { text: `EXIT 0`, color: "var(--st-ok)", bg: "color-mix(in srgb, var(--st-ok) 12%, transparent)" }
        : { text: `EXIT ${t.exit_code ?? "?"}`, color: "var(--st-error)", bg: "color-mix(in srgb, var(--st-error) 12%, transparent)" };
    case "stopped":
      return { text: "STOPPED", color: "var(--st-skipped)", bg: "rgb(var(--card2))" };
    case "timeout":
      return { text: "TIMEOUT", color: "var(--st-missed)", bg: "color-mix(in srgb, var(--st-missed) 12%, transparent)" };
    case "interrupted":
      return { text: "INTERRUPTED", color: "var(--st-missed)", bg: "color-mix(in srgb, var(--st-missed) 12%, transparent)" };
    default:
      return { text: "FAILED TO START", color: "var(--st-error)", bg: "color-mix(in srgb, var(--st-error) 12%, transparent)" };
  }
}

export function TaskCard({ taskId }: { taskId: string }) {
  const { tc } = useChatT();
  const [task, setTask] = useState<TaskRecord | null>(null);
  const [tail, setTail] = useState<string[]>([]);
  const [open, setOpen] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [removed, setRemoved] = useState(false);
  const [tick, setTick] = useState(0);
  const alive = useRef(true);
  const taskRef = useRef<TaskRecord | null>(null);
  useEffect(() => { taskRef.current = task; }, [task]);

  const refresh = useCallback(async () => {
    try {
      const t = await getTask(taskId);
      const logs = await taskLogs(taskId, { tail: 6 });
      if (!alive.current) return;
      setTask(t);
      setTail(logs.lines);
    } catch {
      /* 404 = removed elsewhere; keep last known state */
    }
  }, [taskId]);

  // 单 interval：1s 计时；running 时每 2s 拉状态+尾部。终态后停拉但计时条
  // 已由 dur 固化，interval 留着只为了展开区/按钮的即时性代价最小。
  useEffect(() => {
    alive.current = true;
    void refresh();
    let laps = 0;
    const id = setInterval(() => {
      laps += 1;
      setTick(laps);
      const t = taskRef.current;
      if (t && !TASK_TERMINAL.has(t.status) && laps % 2 === 0) void refresh();
    }, 1000);
    return () => {
      alive.current = false;
      clearInterval(id);
    };
  }, [refresh]);

  if (removed) {
    return (
      <div className="my-1.5 rounded-lg border border-line bg-card px-3 py-2 text-xs text-faint">
        {tc("taskCard.removed")}
      </div>
    );
  }

  const b = badgeOf(task);
  const running = !!task && !TASK_TERMINAL.has(task.status);
  const elapsed = task?.dur || (task?.started_at ? fmtElapsed(task.started_at) : "");
  const expanded = open ?? !running;

  const onAction = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try { await fn(); } catch { /* surfaced by next poll */ }
    setBusy(false);
    await refresh();
  };

  return (
    <div className="my-1.5 overflow-hidden rounded-lg border border-line bg-card"
      style={running ? { borderColor: "color-mix(in srgb, var(--st-running) 35%, rgb(var(--line)))" } : undefined}
      data-testid={`bg-task-${taskId}`}
    >
      <div className="flex items-center gap-2.5 px-3 py-2">
        <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-card2"
          style={{ color: b.color }}>
          <TerminalSquare className="h-3.5 w-3.5" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-[13px] font-semibold">{task?.title || taskId}</span>
            <span className="inline-flex shrink-0 items-center gap-1.5 rounded px-1.5 py-0.5 text-[10px] font-bold tracking-wide"
              style={{ color: b.color, background: b.bg }}>
              {b.spin && <Loader2 className="h-2.5 w-2.5 animate-spin" style={{ color: b.color }} />}
              {b.text}
            </span>
          </div>
          <div className="truncate font-mono text-[11px] text-faint">$ {task?.command || "…"}</div>
        </div>
        <span className="shrink-0 font-mono text-[11px] tabular-nums text-faint" key={tick}>
          {elapsed}
        </span>
        <div className="flex shrink-0 gap-1.5">
          {running ? (
            <button
              disabled={busy}
              onClick={() => onAction(() => stopTask(taskId))}
              className="inline-flex items-center gap-1 rounded-md border border-line2 px-2 py-1 text-[11px] text-muted hover:border-rose-500 hover:text-rose-400 disabled:opacity-50"
            >
              <Square className="h-3 w-3" /> {tc("taskCard.stop")}
            </button>
          ) : (
            <button
              disabled={busy}
              onClick={() => { setRemoved(true); void removeTask(taskId); }}
              className="inline-flex items-center gap-1 rounded-md border border-line2 px-2 py-1 text-[11px] text-muted hover:border-rose-500 hover:text-rose-400 disabled:opacity-50"
            >
              <Trash2 className="h-3 w-3" /> {tc("taskCard.remove")}
            </button>
          )}
        </div>
      </div>
      <div className="truncate px-3 pb-1.5 font-mono text-[10.5px] text-faint">
        ~/.ginno/tasks/{taskId}/output.log
        {running && task?.pid ? ` · pid ${task.pid}` : ""}
        {task?.truncated ? ` · ${tc("taskCard.truncated")}` : ""}
        {task?.detached ? " · detached" : ""}
        {!running && task?.status !== "interrupted" && task?.notify !== false ? ` · ${tc("taskCard.injected")}` : ""}
        {task?.status === "interrupted" ? ` · ${tc("taskCard.interruptedNote")}` : ""}
      </div>
      <details open={expanded} className="border-t border-line bg-base/40">
        <summary
          className="cursor-pointer select-none px-3 py-1.5 text-[11px] text-faint hover:text-muted"
          onClick={(e) => { e.preventDefault(); setOpen(!expanded); }}
        >
          ⌄ {tc("taskCard.outputTail")} ({tail.length})
        </summary>
        <pre className="max-h-36 overflow-y-auto whitespace-pre-wrap break-all px-3 pb-2.5 font-mono text-[11px] leading-relaxed text-muted">
          {tail.join("\n") || tc("taskCard.noOutput")}
        </pre>
      </details>
    </div>
  );
}

/** task_event 块的极薄标记行（blocks.tsx 渲染）：只标注入点，状态在卡片。 */
export function TaskEventBand({ block }: { block: { taskId: string; ev: string; title?: string; exit?: number | null; dur?: string } }) {
  const { tc } = useChatT();
  const color =
    block.ev === "finished"
      ? block.exit === 0 ? "var(--st-ok)" : "var(--st-error)"
      : block.ev === "healthy"
        ? "var(--st-ok)"
        : "var(--st-skipped)";
  const label =
    block.ev === "healthy" ? tc("taskCard.evHealthy")
    : block.ev === "watch" ? tc("taskCard.evWatch")
    : block.ev === "finished" ? tc("taskCard.evFinished", { exit: block.exit ?? "?" })
    : block.ev === "stopped" ? tc("taskCard.evStopped")
    : block.ev === "timeout" ? tc("taskCard.evTimeout")
    : block.ev;
  return (
    <div
      className="my-1 flex items-center gap-1.5 border-l-2 py-0.5 pl-2 text-[11px]"
      style={{ borderColor: color, color: "rgb(var(--muted))", background: "color-mix(in srgb, currentColor 4%, transparent)" }}
    >
      <span style={{ color }}>▸</span>
      <span className="truncate">
        {label} · <span className="font-mono">{block.title || block.taskId}</span>
        {block.dur ? ` · ${block.dur}` : ""}
      </span>
    </div>
  );
}
