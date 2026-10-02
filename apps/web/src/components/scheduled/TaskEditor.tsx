"use client";

// 任务编辑模态（scheduled-tasks-design.md §3.2）。交互对齐 GoalEditor：
// 目标类型分段控件 [💬 对话 | ⚡ Workflow] → 对应字段；计划四选一分段控件。
// workflow 输入表单直接复用 ContextEditor（按配方 context.schema 渲染、
// initial 预填），保存值落 target.context_override；必填缺失不可保存。
// 间隔下限 5 分钟（§10 决议 2，前端侧校验；API 侧再校）。

import { useMemo, useState } from "react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { SchedulePlan, ScheduleTask, ScheduleTarget } from "@/lib/types";
import { ContextEditor } from "@/components/workflow/ContextEditor";
import { WEEKDAY_LABEL } from "./shared";

type ScheduleKind = SchedulePlan["kind"];

const pad = (n: number) => String(n).padStart(2, "0");

/** 现在往后的默认单次时间（明天同一时刻），datetime-local 需要本地 "YYYY-MM-DDTHH:mm"。 */
function defaultOnce(): string {
  const d = new Date(Date.now() + 86400000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function TaskEditor({
  initial,
  onClose,
  onSaved,
}: {
  /** null = 新建。 */
  initial: ScheduleTask | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const g = useGinno();
  const [name, setName] = useState(initial?.name ?? "");
  const [targetType, setTargetType] = useState<"prompt" | "workflow">(initial?.target.type ?? "prompt");
  const [prompt, setPrompt] = useState(
    initial?.target.type === "prompt" ? initial.target.prompt : "",
  );
  const [agentId, setAgentId] = useState(
    initial?.target.type === "prompt" ? initial.target.agent_id ?? g.agents[0]?.id ?? "" : g.agents[0]?.id ?? "",
  );
  // 无项目列表接口（runtime 只用 "default"）——文本输入，默认当前项目。
  const [projectSlug, setProjectSlug] = useState(
    initial?.target.type === "prompt" ? initial.target.project_slug ?? "default" : "default",
  );
  const [workflowId, setWorkflowId] = useState(
    initial?.target.type === "workflow" ? initial.target.workflow_id : "",
  );
  const [contextOverride, setContextOverride] = useState<Record<string, unknown>>(
    initial?.target.type === "workflow" ? initial.target.context_override ?? {} : {},
  );

  // ---- 计划 ----
  const [kind, setKind] = useState<ScheduleKind>(initial?.schedule.kind ?? "daily");
  const [intervalMinutes, setIntervalMinutes] = useState(
    initial?.schedule.kind === "interval" ? initial.schedule.minutes : 30,
  );
  // 间隔的显示单位（分钟/小时）；intervalMinutes 始终是分钟真值。
  const [intervalUnit, setIntervalUnit] = useState<"m" | "h">(
    initial?.schedule.kind === "interval" && initial.schedule.minutes % 60 === 0 ? "h" : "m",
  );
  const [dailyAt, setDailyAt] = useState(initial?.schedule.kind === "daily" ? initial.schedule.at : "09:30");
  const [weeklyWeekday, setWeeklyWeekday] = useState(
    initial?.schedule.kind === "weekly" ? initial.schedule.weekday : 1,
  );
  const [weeklyAt, setWeeklyAt] = useState(initial?.schedule.kind === "weekly" ? initial.schedule.at : "18:00");
  const [onceAt, setOnceAt] = useState(
    initial?.schedule.kind === "once" ? initial.schedule.at.slice(0, 16) : defaultOnce(),
  );

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const wf = g.workflows.find((w) => w.id === workflowId) ?? null;
  // 编辑中的任务原指向的配方 id（判别联合先拆开，回调里要用）。
  const initialWfId = initial?.target.type === "workflow" ? initial.target.workflow_id : null;
  // 有 context.schema 定义的配方才出表单（§3.2：无 context 定义的折叠隐藏）。
  const schemaProps = useMemo(
    () =>
      (wf?.dsl as { context?: { schema?: { properties?: Record<string, unknown>; required?: string[] } } } | undefined)
        ?.context?.schema?.properties ?? null,
    [wf],
  );
  const requiredKeys = useMemo(
    () =>
      ((wf?.dsl as { context?: { schema?: { required?: string[] } } } | undefined)?.context?.schema
        ?.required ?? []) as string[],
    [wf],
  );

  const buildSchedule = (): SchedulePlan | { error: string } => {
    switch (kind) {
      case "interval": {
        const m = Number(intervalMinutes);
        if (!Number.isFinite(m) || m < 5) return { error: "间隔下限为 5 分钟" };
        return { kind: "interval", minutes: Math.round(m) };
      }
      case "daily":
        if (!dailyAt) return { error: "请选择每日执行时间" };
        return { kind: "daily", at: dailyAt };
      case "weekly":
        if (!weeklyAt) return { error: "请选择每周执行时间" };
        return { kind: "weekly", weekday: weeklyWeekday, at: weeklyAt };
      case "once": {
        if (!onceAt) return { error: "请选择单次执行时间" };
        // datetime-local 给 "YYYY-MM-DDTHH:mm"；契约是本地时间 "…THH:mm:ss"。
        return { kind: "once", at: onceAt.length === 16 ? `${onceAt}:00` : onceAt };
      }
    }
  };

  const save = async () => {
    setError(null);
    if (!name.trim()) return setError("请填写任务名称");
    let target: ScheduleTarget;
    if (targetType === "prompt") {
      if (!prompt.trim()) return setError("请填写提示词");
      target = {
        type: "prompt",
        prompt: prompt.trim(),
        agent_id: agentId || null,
        project_slug: projectSlug.trim() || "default",
      };
    } else {
      if (!workflowId) return setError("请选择 Workflow 配方");
      // 必填输入缺失 → 不可保存（§3.2；配方后续被改出必填项时到点由 runtime 记
      // error("missing_input")，这里只拦编辑当下的缺失）。
      for (const k of requiredKeys) {
        const v = contextOverride[k];
        if (v === undefined || v === null || v === "") {
          return setError(`Workflow 输入「${k}」为必填，请补全后再保存`);
        }
      }
      target = { type: "workflow", workflow_id: workflowId, context_override: contextOverride };
    }
    const plan = buildSchedule();
    if ("error" in plan) return setError(plan.error);

    setBusy(true);
    try {
      const body = { name: name.trim(), target, schedule: plan, enabled: initial?.enabled ?? true };
      const r = initial
        ? await api.patchScheduleTask(initial.id, body)
        : await api.createScheduleTask(body);
      // 400（间隔<5 / 必填缺失）→ {detail}；ok:false 兜底。
      const msg = (r as { detail?: string; error?: string }).detail ?? (r as { error?: string }).error;
      if ((r as { ok?: boolean }).ok === false || !(r as { id?: string }).id) {
        setError(msg || "保存失败");
        return;
      }
      onSaved();
      onClose();
    } catch {
      setError("无法连接运行时");
    } finally {
      setBusy(false);
    }
  };

  const segBtn = (active: boolean) =>
    `rounded-md px-2.5 py-1 text-xs transition-colors ${
      active ? "bg-violet/20 text-violet" : "text-muted hover:text-txt"
    }`;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onMouseDown={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="定时任务编辑"
    >
      <div
        className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-xl border bg-card p-4 shadow-2xl"
        style={{ borderColor: "var(--line)" }}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="text-sm font-semibold text-txt">{initial ? "编辑定时任务" : "新建定时任务"}</div>

        {/* 目标类型分段控件（模态第一项，§3.2） */}
        <div className="mt-3">
          <div className="field-label">目标类型</div>
          <div className="inline-flex rounded-lg border border-line bg-base/40 p-0.5">
            <button className={segBtn(targetType === "prompt")} onClick={() => setTargetType("prompt")}>
              💬 对话
            </button>
            <button className={segBtn(targetType === "workflow")} onClick={() => setTargetType("workflow")}>
              ⚡ Workflow
            </button>
          </div>
        </div>

        <div className="mt-3">
          <div className="field-label">名称</div>
          <input
            className="field"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="例如：日志巡检"
          />
        </div>

        {targetType === "prompt" ? (
          <>
            <div className="mt-3">
              <div className="field-label">提示词（每次执行作为一条用户消息发给 Agent）</div>
              <textarea
                className="field"
                rows={4}
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder="汇总 ~/.ginno/logs/sidecar.log 过去 24h 的 ERROR，给出_top3 修复建议…"
              />
            </div>
            <div className="mt-3 grid grid-cols-2 gap-3">
              <div>
                <div className="field-label">Agent</div>
                <select className="field" value={agentId} onChange={(e) => setAgentId(e.target.value)}>
                  {g.agents.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <div className="field-label">项目</div>
                <input
                  className="field"
                  value={projectSlug}
                  onChange={(e) => setProjectSlug(e.target.value)}
                  placeholder="default"
                />
              </div>
            </div>
          </>
        ) : (
          <>
            <div className="mt-3">
              <div className="field-label">Workflow 配方</div>
              <select
                className="field"
                value={workflowId}
                onChange={(e) => {
                  setWorkflowId(e.target.value);
                  setContextOverride({}); // 换配方清掉旧输入，表单按新 initial 预填
                }}
              >
                <option value="">选择配方…</option>
                {g.workflows.map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.name}
                    {w.version ? ` v${w.version}` : ""}
                  </option>
                ))}
              </select>
              {/* 配方在列表中找不到（任务指向已删除配方）仍允许保存——触发时由
                  runtime 记 error("workflow_missing")（§3.2）。 */}
              {initialWfId && !g.workflows.some((w) => w.id === initialWfId) && (
                  <div className="mt-1 text-[11px] text-red">
                    原配方已删除，可改选其它配方或保留（到点将记为失败）
                  </div>
                )}
            </div>
            {wf && schemaProps && Object.keys(schemaProps).length > 0 && (
              <div className="mt-3 rounded-lg border border-line bg-base/30 p-2.5">
                {/* 复用 ContextEditor：按 schema 渲染、initial（叠加任务已存的
                    context_override）预填。key 保证换配方/进编辑时重置内部状态。 */}
                <ContextEditor
                  key={wf.id}
                  dsl={{
                    ...wf.dsl,
                    context: {
                      ...(wf.dsl as { context?: object })?.context,
                      initial: {
                        ...((wf.dsl as { context?: { initial?: Record<string, unknown> } })?.context
                          ?.initial ?? {}),
                        ...contextOverride,
                      },
                    },
                  } as never}
                  onChange={setContextOverride}
                />
                <div className="mt-1 text-[10px] text-faint">保存值随任务存储，到点作为本次运行的输入。</div>
              </div>
            )}
          </>
        )}

        {/* 计划四选一分段控件 */}
        <div className="mt-3">
          <div className="field-label">计划</div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="inline-flex flex-wrap rounded-lg border border-line bg-base/40 p-0.5">
              {(
                [
                  ["interval", "间隔"],
                  ["daily", "每日"],
                  ["weekly", "每周"],
                  ["once", "单次"],
                ] as Array<[ScheduleKind, string]>
              ).map(([k, label]) => (
                <button key={k} className={segBtn(kind === k)} onClick={() => setKind(k)}>
                  {label}
                </button>
              ))}
            </div>
            {kind === "interval" && (
              <span className="flex items-center gap-1.5 text-xs text-muted">
                每
                <input
                  type="number"
                  min={intervalUnit === "h" ? 1 : 5}
                  className="field w-20 px-2 py-1"
                  value={intervalUnit === "h" ? intervalMinutes / 60 : intervalMinutes}
                  onChange={(e) => {
                    const v = Number(e.target.value);
                    if (!Number.isFinite(v)) return;
                    setIntervalMinutes(intervalUnit === "h" ? Math.max(60, Math.round(v) * 60) : Math.max(5, Math.round(v)));
                  }}
                />
                <select
                  className="field w-20 px-1 py-1"
                  value={intervalUnit}
                  onChange={(e) => {
                    const unit = e.target.value as "m" | "h";
                    setIntervalUnit(unit);
                    // 切单位时把真值归一到合法档位（小时 ≥1h、分钟 ≥5m）。
                    setIntervalMinutes((prev) =>
                      unit === "h" ? Math.max(60, Math.round(prev / 60) * 60) : Math.max(5, prev % 60 || 5),
                    );
                  }}
                >
                  <option value="m">分钟</option>
                  <option value="h">小时</option>
                </select>
              </span>
            )}
            {kind === "daily" && (
              <input
                type="time"
                className="field w-32 px-2 py-1"
                value={dailyAt}
                onChange={(e) => setDailyAt(e.target.value)}
              />
            )}
            {kind === "weekly" && (
              <span className="flex items-center gap-1.5 text-xs text-muted">
                周
                <select
                  className="field w-16 px-1 py-1"
                  value={weeklyWeekday}
                  onChange={(e) => setWeeklyWeekday(Number(e.target.value))}
                >
                  {WEEKDAY_LABEL.map((w, i) => (
                    <option key={i} value={i}>
                      {w}
                    </option>
                  ))}
                </select>
                <input
                  type="time"
                  className="field w-32 px-2 py-1"
                  value={weeklyAt}
                  onChange={(e) => setWeeklyAt(e.target.value)}
                />
              </span>
            )}
            {kind === "once" && (
              <input
                type="datetime-local"
                className="field w-56 px-2 py-1"
                value={onceAt}
                onChange={(e) => setOnceAt(e.target.value)}
              />
            )}
          </div>
          {kind === "interval" && (
            <div className="mt-1 text-[10px] text-faint">间隔下限 5 分钟（防雪崩）。</div>
          )}
        </div>

        {error && <div className="mt-3 rounded-lg border border-red/40 bg-red/10 px-3 py-2 text-xs text-red">{error}</div>}

        <div className="mt-4 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:text-txt"
          >
            取消
          </button>
          <button
            disabled={busy}
            onClick={save}
            className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
          >
            {busy ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}
