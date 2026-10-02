"use client";

// 任务面板（scheduled-tasks-design.md §3.2）：卡片行 = 目标图标 + 计划描述 +
// 上次/下次 + 行内启停 / 立即执行 / 编辑 / 删除。全局关时整列降透明度、
// 行内开关不可点；配方已删除 / 单次已完成有专门态。

import { useState } from "react";
import { Pencil, Play, Plus, Trash2 } from "lucide-react";
import * as api from "@/lib/runtime";
import { useGinno } from "@/lib/store";
import type { ScheduleConfig, ScheduleTask } from "@/lib/types";
import { ConfirmModal } from "@/components/ConfirmModal";
import { fmtClock, fmtDayPrefix, fmtDuration, planDescription } from "./shared";

export function TasksPanel({
  cfg,
  onReload,
  onEdit,
}: {
  cfg: ScheduleConfig;
  onReload: () => void;
  onEdit: (task: ScheduleTask | "new") => void;
}) {
  const g = useGinno();
  const [deleteTarget, setDeleteTarget] = useState<ScheduleTask | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  // next_run_at 升序（§3.2）；无下次的（暂停/单次完成）排后。
  const tasks = [...cfg.tasks].sort((a, b) => {
    const an = a.enabled ? a.next_run_at ?? Infinity : Infinity;
    const bn = b.enabled ? b.next_run_at ?? Infinity : Infinity;
    return an - bn || b.updated - a.updated;
  });

  const toggle = async (t: ScheduleTask) => {
    if (!cfg.enabled || busyId) return; // 全局关时行内开关不可点（§3.2）
    setBusyId(t.id);
    try {
      await api.patchScheduleTask(t.id, { enabled: !t.enabled });
      onReload();
    } catch {
      /* 端点不可达——下次刷新对账 */
    } finally {
      setBusyId(null);
    }
  };

  const runNow = async (t: ScheduleTask) => {
    if (busyId) return;
    setBusyId(t.id);
    try {
      await api.runScheduleTaskNow(t.id);
      onReload();
    } catch {
      /* ignore */
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (t: ScheduleTask) => {
    setDeleteTarget(null);
    try {
      await api.deleteScheduleTask(t.id);
    } catch {
      /* ignore */
    }
    onReload();
  };

  return (
    <div>
      <div className="mb-2 flex justify-end">
        <button
          onClick={() => onEdit("new")}
          className="flex items-center gap-1.5 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:bg-violet/90"
        >
          <Plus className="h-3.5 w-3.5" /> New Task
        </button>
      </div>

      {tasks.length === 0 ? (
        <div className="rounded-xl border border-dashed border-line py-10 text-center text-sm text-faint">
          No scheduled tasks yet.
          <br />
          Click "New Task" to set up your first one — a prompt or a workflow.
        </div>
      ) : (
        <div className={`space-y-2 transition-opacity ${cfg.enabled ? "" : "opacity-50"}`}>
          {tasks.map((t) => {
            const isWf = t.target.type === "workflow";
            // 判别联合先拆开（JSX 回调里不保留 narowing）。
            const promptTarget = t.target.type === "prompt" ? t.target : null;
            const wfTarget = t.target.type === "workflow" ? t.target : null;
            // 配方已删除（§3.2/§7）：workflow_id 在 store 配方列表里找不到。
            const wfMissing = !!wfTarget && !g.workflows.some((w) => w.id === wfTarget.workflow_id);
            // 单次任务执行完自动置 disabled → 「已完成」态（§7）。
            const onceDone = t.schedule.kind === "once" && !t.enabled;
            const last = t.last_run;
            const agent = promptTarget?.agent_id
              ? g.agents.find((a) => a.id === promptTarget.agent_id)?.name
              : null;
            return (
              <div
                key={t.id}
                className={`rounded-xl border bg-card p-3.5 transition-opacity ${
                  cfg.enabled ? "" : "opacity-60"
                }`}
              >
                <div className="flex items-center gap-2.5">
                  <span className="shrink-0 text-base leading-none" title={isWf ? "Workflow target" : "Prompt target"}>
                    {isWf ? "⚡" : "💬"}
                  </span>
                  <span className="min-w-0 truncate text-sm font-medium text-txt">{t.name}</span>
                  <span className="shrink-0 text-xs text-muted">{planDescription(t.schedule)}</span>
                  {!isWf && (
                    <span className="hidden shrink-0 text-xs text-faint sm:inline">
                      {agent ? `agent: ${agent}` : ""}
                      {promptTarget?.project_slug ? ` · project: ${promptTarget.project_slug}` : ""}
                    </span>
                  )}
                  {wfMissing && (
                    <span className="shrink-0 rounded-full border border-red/40 bg-red/10 px-1.5 text-[10px] leading-4 text-red">
                      Recipe deleted
                    </span>
                  )}
                  {onceDone && (
                    <span className="shrink-0 rounded-full border border-green/40 bg-green/10 px-1.5 text-[10px] leading-4 text-green">
                      Done
                    </span>
                  )}

                  <span className="ml-auto flex shrink-0 items-center gap-1.5">
                    <button
                      onClick={() => toggle(t)}
                      disabled={!cfg.enabled || busyId === t.id}
                      title={t.enabled ? "Pause this task" : "Enable this task"}
                      className={`text-lg leading-none transition-colors disabled:cursor-not-allowed ${
                        t.enabled ? "text-green" : "text-faint"
                      } ${cfg.enabled ? "hover:opacity-80" : ""}`}
                    >
                      {t.enabled ? "●" : "○"}
                    </button>
                    <button
                      onClick={() => runNow(t)}
                      disabled={busyId === t.id}
                      title="Run once now (does not advance the schedule)"
                      className="flex items-center rounded-lg border border-line px-2 py-1 text-[11px] text-muted transition-colors hover:border-violet/50 hover:text-violet disabled:opacity-40"
                    >
                      <Play className="h-3 w-3" />
                      <span className="ml-1 hidden md:inline">Run now</span>
                    </button>
                    <button
                      onClick={() => onEdit(t)}
                      title="Edit task"
                      className="rounded-lg border border-line p-1.5 text-muted transition-colors hover:border-line2 hover:text-txt"
                    >
                      <Pencil className="h-3 w-3" />
                    </button>
                    <button
                      onClick={() => setDeleteTarget(t)}
                      title="Delete task (runs and sessions are kept)"
                      className="rounded-lg border border-line p-1.5 text-muted transition-colors hover:border-red/50 hover:text-red"
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </span>
                </div>
                <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 pl-7 text-[11px] text-faint">
                  {isWf ? (
                    <span>
                      workflow:{" "}
                      {g.workflows.find((w) => w.id === wfTarget?.workflow_id)?.name ??
                        (wfMissing ? "deleted" : wfTarget?.workflow_id)}
                    </span>
                  ) : null}
                  <span>
                    Last:{" "}
                    {last
                      ? `${fmtDayPrefix(last.started)} ${fmtClock(last.started)} ${
                          last.status === "ok" ? "✓" : last.status === "running" ? "▨" : "✕"
                        }${last.finished ? ` · ${fmtDuration(last.finished - (last.started ?? 0))}` : ""}`
                      : "never run"}
                  </span>
                  <span>
                    {onceDone
                      ? "Paused · one-shot task finished"
                      : wfMissing
                        ? "Paused · recipe deleted, edit to re-pick"
                        : t.enabled && cfg.enabled && t.next_run_at
                          ? `Next: ${fmtDayPrefix(t.next_run_at)} ${fmtClock(t.next_run_at)}`
                          : "Paused"}
                  </span>
                  {!cfg.enabled && <span className="text-yellow">Paused (globally disabled)</span>}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {deleteTarget && (
        <ConfirmModal
          title="Delete scheduled task"
          message={`Delete task "${deleteTarget.name}"? Its run history and shadow sessions are kept (ledger data is never destroyed), but the task will no longer fire.`}
          confirmLabel="Delete"
          onConfirm={() => remove(deleteTarget)}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}
