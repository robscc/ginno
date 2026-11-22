"use client";

// 任务面板（scheduled-tasks-design.md §3.2）：卡片行 = 目标图标 + 计划描述 +
// 上次/下次 + 行内启停 / 立即执行 / 编辑 / 删除。全局关时整列降透明度、
// 行内开关不可点；配方已删除 / 单次生命周期终态有专门态。
//
// schedule-integrity 纪律：
// * 「已完成」只认按计划成功（onceOutcome 读 last_scheduled），暂停未执行
//   的一次性任务显示「已暂停」，manual 成功不冒充按计划完成；
// * 暂停/删除失败（断连或 5xx）→ 行内红色反馈，行保留可对账（jsonStrict
//   让非 2xx 抛错，不再被错误体伪装成成功）；
// * 立即执行丢回执（网络层异常）→ 行内黄色「结果未知」提示，引导去执行
//   记录对账，**绝不自动重发**（请求可能已生效，重发=重复执行）；同时触发
//   一次重拉——GET /api/schedule 是 WS 之外的状态恢复路径，WS 断开也能对上。

import { useState } from "react";
import { Pencil, Play, Plus, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import { useGinno } from "@/lib/store";
import type { ScheduleConfig, ScheduleTask } from "@/lib/types";
import { ConfirmModal } from "@/components/ConfirmModal";
import {
  fmtClock,
  fmtDayPrefix,
  fmtDuration,
  isNextRunOverdue,
  onceOutcome,
  planDescription,
} from "./shared";

/** 行内操作反馈（schedule-integrity）：per-task 一条，新操作覆盖旧的。
 *  kind=unknown 仅用于 run-now 回执丢失/结果未知——文案与确定性失败分开；
 *  runsTaskId 附带「查看此任务运行记录」跳转目标（复核缺口④：对账入口
 *  必须可点击，不能只写在文案里）。 */
type ActionFeedback = { taskId: string; kind: "error" | "unknown"; text: string; runsTaskId?: string };

function errText(e: unknown): string {
  return e instanceof Error && e.message ? e.message : String(e || "");
}

/** fetch 网络层失败（断连/超时）统一显示中性文案，不漏浏览器技术串。 */
function isNetworkError(e: unknown): boolean {
  return e instanceof TypeError;
}

/** run-now 结局分型（复核缺口④）——三类必须分开：
 *  · 4xx（ginnoStatus 400-499）：请求已送达且被服务端**明确拒绝**，run 未
 *    开始 → 确定性失败，可安全重试；
 *  · 未知：5xx（服务端内部错——run 可能已经开始）/ 响应解析失败（200 但
 *    body 畸形，SyntaxError）/ 断连超时（TypeError）/ 无状态信息的异常
 *    → **结果未知**，请求可能已生效，绝不提示「可安全重试」。 */
function runNowOutcome(e: unknown): "rejected4xx" | "unknown" {
  const st = (e as { ginnoStatus?: number } | null)?.ginnoStatus;
  if (typeof st === "number" && st >= 400 && st < 500) return "rejected4xx";
  return "unknown";
}

export function TasksPanel({
  cfg,
  onReload,
  onEdit,
  onOpenTaskRuns,
}: {
  cfg: ScheduleConfig;
  onReload: () => void;
  onEdit: (task: ScheduleTask | "new") => void;
  /** run-now「查看此任务运行记录」：跳执行记录页签并按任务过滤（对账入口）。 */
  onOpenTaskRuns: (taskId: string, taskName: string) => void;
}) {
  const g = useGinno();
  const tr = useTranslations("sched");
  const [deleteTarget, setDeleteTarget] = useState<ScheduleTask | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<ActionFeedback | null>(null);
  // 任务列表刷新后旧反馈随行消失；显式清除只在下一次操作时发生（覆盖）。

  // next_run_at 升序（§3.2）；无下次的（暂停/单次完成）排后。
  const tasks = [...cfg.tasks].sort((a, b) => {
    const an = a.enabled ? a.next_run_at ?? Infinity : Infinity;
    const bn = b.enabled ? b.next_run_at ?? Infinity : Infinity;
    return an - bn || b.updated - a.updated;
  });

  const toggle = async (t: ScheduleTask) => {
    if (!cfg.enabled || busyId) return; // 全局关时行内开关不可点（§3.2）
    setBusyId(t.id);
    setFeedback(null);
    try {
      await api.patchScheduleTask(t.id, { enabled: !t.enabled });
      onReload();
    } catch (e) {
      // 断连或 5xx：行内反馈 + 保留行对账（不吞错——吞错会让用户以为已暂停，
      // 而磁盘上任务还在跑）。onReload 兜底对齐真实状态。
      setFeedback({
        taskId: t.id,
        kind: "error",
        text: tr("tasks.toggleFailed", {
          error: isNetworkError(e) ? tr("tasks.netError") : errText(e),
        }),
      });
      onReload();
    } finally {
      setBusyId(null);
    }
  };

  const runNow = async (t: ScheduleTask) => {
    if (busyId) return;
    setBusyId(t.id);
    setFeedback(null);
    try {
      await api.runScheduleTaskNow(t.id);
      onReload();
    } catch (e) {
      // 三分型（复核缺口④）：
      // · 4xx（jsonStrict 抛的 Error 带 ginnoStatus 4xx）= 服务端明确拒绝
      //   （任务不存在/schema 非法等），run 未开始 → 红色确定性失败，可安全重试；
      // · 5xx / 响应解析失败（SyntaxError）/ 断连（TypeError）/ 无状态信息
      //   = **结果未知**——请求可能已在服务端生效，盲目重发 = 重复执行；
      //   黄色「结果未知」+ 可点击的「查看此任务运行记录」引导对账。
      // 两者都触发一次 GET 重拉——WS 不可用/未连接时这是状态恢复的唯一路径
      // （unknown 文案已明确「不会自动收到结果通知」，不承诺 WS 恢复）。
      if (runNowOutcome(e) === "rejected4xx") {
        setFeedback({
          taskId: t.id,
          kind: "error",
          text: tr("tasks.runNowFailed", { error: errText(e) }),
        });
      } else {
        setFeedback({
          taskId: t.id,
          kind: "unknown",
          runsTaskId: t.id,
          text: tr("tasks.runNowUnknown"),
        });
      }
      onReload();
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (t: ScheduleTask) => {
    setDeleteTarget(null);
    setFeedback(null);
    try {
      await api.deleteScheduleTask(t.id);
    } catch (e) {
      // 删除失败：任务仍在列表（若服务端实际删成了，下面的重拉会让行消失，
      // 这条反馈也随之卸载——两态自洽，可对账）。
      setFeedback({
        taskId: t.id,
        kind: "error",
        text: tr("tasks.deleteFailed", {
          error: isNetworkError(e) ? tr("tasks.netError") : errText(e),
        }),
      });
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
          <Plus className="h-3.5 w-3.5" /> {tr("tasks.new")}
        </button>
      </div>

      {tasks.length === 0 ? (
        <div className="rounded-xl border border-dashed border-line py-10 text-center text-sm text-faint">
          {tr("tasks.emptyLine1")}
          <br />
          {tr("tasks.emptyLine2")}
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
            // 单次任务生命周期（schedule-integrity）：只认按计划成功（done /
            // legacyDone——后者还需 last_run 带 trigger=schedule+status=ok 的
            // 完整证据）；「暂停未执行」「错过被停用」「按计划失败」「补跑
            // 排队」「来源未知」各有独立态，不再从 enabled 推导 Done。
            const outcome = onceOutcome(t);
            const onceDone = !t.enabled && (outcome === "done" || outcome === "legacyDone");
            const last = t.last_run;
            // 行内操作反馈只挂在对应对应任务行上（其他行不受影响，保留可对账）。
            const rowFeedback = feedback?.taskId === t.id ? feedback : null;
            // 计划点已过但任务仍启用（sidecar 长时间没跑/旧数据）→ 如实提示
            // 等待调度对账，而不是渲染一个过去时点的「下次」。
            const overdue = t.enabled && isNextRunOverdue(t.next_run_at);
            // 行尾状态文案：once 生命周期终态 > 配方缺失 > 过期 > 常规。
            // catchingUp（排队补跑，无成功证据）与 unknownSource（老数据无
            // 来源）都不显示 Done，如实呈现等待/未知（复核缺口②）。
            const stateText = onceDone
              ? tr("tasks.stateOnceDone")
              : !t.enabled && outcome === "missed"
                ? tr("tasks.stateOnceMissed")
                : !t.enabled && outcome === "failed"
                  ? tr("tasks.stateOnceFailed")
                  : !t.enabled && outcome === "skipped"
                    ? tr("tasks.stateOnceSkipped")
                    : !t.enabled && outcome === "running"
                      ? tr("tasks.stateOnceRunning")
                      : !t.enabled && outcome === "catchingUp"
                        ? tr("tasks.stateOnceCatchingUp")
                        : !t.enabled && outcome === "unknownSource"
                          ? tr("tasks.stateUnknownSource")
                          : wfMissing
                            ? tr("tasks.stateWfMissing")
                            : overdue
                              ? tr("tasks.stateOverdue", {
                                  day: fmtDayPrefix(t.next_run_at),
                                  clock: fmtClock(t.next_run_at),
                                })
                              : t.enabled && cfg.enabled && t.next_run_at
                                ? tr("tasks.stateNext", {
                                    day: fmtDayPrefix(t.next_run_at),
                                    clock: fmtClock(t.next_run_at),
                                  })
                                : tr("tasks.statePaused");
            const agent = promptTarget?.agent_id
              ? g.agents.find((a) => a.id === promptTarget.agent_id)?.name
              : null;
            return (
              <div
                key={t.id}
                data-task-id={t.id}
                className={`rounded-xl border bg-card p-3.5 transition-opacity ${
                  cfg.enabled ? "" : "opacity-60"
                }`}
              >
                {/* 首行可换行：窄卡下放不下的内容整体落到第二行，而不是把
                    右侧操作菜单挤出卡片边界（菜单被截断/不可见的根因就是
                    所有元素 shrink-0 且行不可 wrap——溢出直接画出卡片外）。 */}
                <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
                  <span className="shrink-0 text-base leading-none" title={isWf ? tr("tasks.targetWorkflow") : tr("tasks.targetPrompt")}>
                    {isWf ? "⚡" : "💬"}
                  </span>
                  <span className="min-w-0 truncate text-sm font-medium text-txt" title={t.name}>
                    {t.name}
                  </span>
                  <span className="shrink-0 text-xs text-muted" title={planDescription(t.schedule)}>
                    {planDescription(t.schedule)}
                  </span>
                  {!isWf && (
                    <span className="hidden max-w-[180px] shrink-0 truncate text-xs text-faint sm:inline-block sm:align-middle" title={agent ?? promptTarget?.project_slug ?? undefined}>
                      {agent ? tr("tasks.agentSuffix", { name: agent }) : ""}
                      {promptTarget?.project_slug
                        ? tr("tasks.projectSuffix", { slug: promptTarget.project_slug })
                        : ""}
                    </span>
                  )}
                  {wfMissing && (
                    <span className="shrink-0 rounded-full border border-red/40 bg-red/10 px-1.5 text-[10px] leading-4 text-red">
                      {tr("tasks.recipeDeleted")}
                    </span>
                  )}
                  {onceDone && (
                    <span className="shrink-0 rounded-full border border-green/40 bg-green/10 px-1.5 text-[10px] leading-4 text-green">
                      {outcome === "legacyDone" ? tr("tasks.doneLegacy") : tr("tasks.done")}
                    </span>
                  )}
                  {!t.enabled && outcome === "missed" && (
                    <span className="shrink-0 rounded-full border border-yellow/40 bg-yellow/10 px-1.5 text-[10px] leading-4 text-yellow">
                      {tr("tasks.onceMissed")}
                    </span>
                  )}
                  {/* 排队补跑（复核缺口②）：计划点错过但已进补跑队列——
                      无成功证据，黄色「补跑排队」而非绿色 Done。 */}
                  {!t.enabled && outcome === "catchingUp" && (
                    <span className="shrink-0 rounded-full border border-yellow/40 bg-yellow/10 px-1.5 text-[10px] leading-4 text-yellow">
                      {tr("tasks.onceCatchingUp")}
                    </span>
                  )}
                  {/* 来源未知（复核缺口②）：老数据 last_run 无 trigger——
                      无从证明按计划成功，灰色中性标，绝不绿。 */}
                  {!t.enabled && outcome === "unknownSource" && (
                    <span className="shrink-0 rounded-full border border-line2 bg-card px-1.5 text-[10px] leading-4 text-faint">
                      {tr("tasks.onceUnknownSource")}
                    </span>
                  )}

                  {/* shrink-0 保住操作菜单永不压缩；wrap 后整组右对齐落到新行 */}
                  <span className="ml-auto flex shrink-0 items-center gap-1.5">
                    <button
                      onClick={() => toggle(t)}
                      disabled={!cfg.enabled || busyId === t.id}
                      title={t.enabled ? tr("tasks.pauseTitle") : tr("tasks.enableTitle")}
                      className={`text-lg leading-none transition-colors disabled:cursor-not-allowed ${
                        t.enabled ? "text-green" : "text-faint"
                      } ${cfg.enabled ? "hover:opacity-80" : ""}`}
                    >
                      {t.enabled ? "●" : "○"}
                    </button>
                    <button
                      onClick={() => runNow(t)}
                      disabled={busyId === t.id}
                      title={tr("tasks.runNowTitle")}
                      className="flex items-center rounded-lg border border-line px-2 py-1 text-[11px] text-muted transition-colors hover:border-violet/50 hover:text-violet disabled:opacity-40"
                    >
                      <Play className="h-3 w-3" />
                      <span className="ml-1 hidden md:inline">{tr("tasks.runNow")}</span>
                    </button>
                    <button
                      onClick={() => onEdit(t)}
                      title={tr("tasks.editTitle")}
                      className="rounded-lg border border-line p-1.5 text-muted transition-colors hover:border-line2 hover:text-txt"
                    >
                      <Pencil className="h-3 w-3" />
                    </button>
                    <button
                      onClick={() => setDeleteTarget(t)}
                      title={tr("tasks.deleteTitle")}
                      className="rounded-lg border border-line p-1.5 text-muted transition-colors hover:border-red/50 hover:text-red"
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </span>
                </div>
                <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 pl-7 text-[11px] text-faint">
                  {isWf ? (
                    <span>
                      {tr("tasks.workflow", {
                        name:
                          g.workflows.find((w) => w.id === wfTarget?.workflow_id)?.name ??
                          (wfMissing ? tr("tasks.workflowDeleted") : wfTarget?.workflow_id ?? ""),
                      })}
                    </span>
                  ) : null}
                  {/* P0-1：上次执行失败整行标红并追加「上次失败」小标；成功
                      与否分开记账——last_success 不被失败覆盖（后端 last_success）。 */}
                  <span className={last?.status === "error" ? "text-red" : undefined}>
                    {tr("tasks.last", {
                      value: last
                        ? `${fmtDayPrefix(last.started)} ${fmtClock(last.started)} ${
                            last.status === "ok" ? "✓" : last.status === "running" ? "▨" : "✕"
                          }${last.finished ? ` · ${fmtDuration(last.finished - (last.started ?? 0))}` : ""}`
                        : tr("tasks.neverRun"),
                    })}
                    {last?.status === "error" && (
                      <span className="ml-1 rounded-full border border-red/40 bg-red/10 px-1.5 text-[10px] leading-4">
                        {tr("tasks.lastFailed")}
                      </span>
                    )}
                  </span>
                  {/* 上次成功单独一行：失败时它才是「昨天到底跑没跑」的答案。 */}
                  {t.last_success ? (
                    <span className={last?.status === "error" ? "text-green" : undefined}>
                      {tr("tasks.lastSuccess", {
                        value: `${fmtDayPrefix(t.last_success.finished)} ${fmtClock(t.last_success.finished)}`,
                      })}
                    </span>
                  ) : last ? (
                    <span>{tr("tasks.neverSucceeded")}</span>
                  ) : null}
                  <span>{stateText}</span>
                  {!cfg.enabled && <span className="text-yellow">{tr("tasks.pausedGlobal")}</span>}
                </div>
                {/* 行内操作反馈（schedule-integrity）：确定性失败=红，结果
                    未知=黄。保留在行内而不是 toast——行还在，反馈与行同在才
                    可对账；下一条消息（重拉后的新数据）不冲掉它。unknown 分
                    支附「查看此任务运行记录」按钮（复核缺口④）：对账入口要
                    可点击，不能只写在文案里。 */}
                {rowFeedback && (
                  <div
                    data-testid={`task-action-feedback-${t.id}`}
                    className={`mt-1.5 ml-7 rounded-md border px-2 py-1 text-[11px] leading-relaxed ${
                      rowFeedback.kind === "unknown"
                        ? "border-yellow/40 bg-yellow/10 text-yellow"
                        : "border-red/40 bg-red/10 text-red"
                    }`}
                  >
                    <div>{rowFeedback.text}</div>
                    {rowFeedback.kind === "unknown" && rowFeedback.runsTaskId === t.id && (
                      <button
                        data-testid={`task-view-runs-${t.id}`}
                        onClick={() => onOpenTaskRuns(t.id, t.name)}
                        className="mt-1 rounded-md bg-yellow/20 px-2 py-0.5 text-[11px] font-medium text-yellow hover:bg-yellow/30"
                      >
                        {tr("tasks.viewRuns")}
                      </button>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {deleteTarget && (
        <ConfirmModal
          title={tr("tasks.deleteModalTitle")}
          message={tr("tasks.deleteModalMessage", { name: deleteTarget.name })}
          confirmLabel={tr("tasks.deleteModalConfirm")}
          onConfirm={() => remove(deleteTarget)}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}
