/**
 * 运行中任务统一面板（tasks-overview 方案 §2.1）的 API 客户端。
 *
 * it_1（后端聚合端点）未合入期间，`getTasksOverview` 遇 404/旧 sidecar 抛错，
 * 由 lib/tasksOverview 的 hub 兜底为空态——前端先落地不阻塞联调。
 */

import { BASE, H } from "./core";

/** 六类句柄的 kind（与后端聚合 API 的分组一一对应）。 */
export type TaskHandleKind = "turn" | "schedule" | "plan" | "run" | "goal" | "delegation";

/** 统一句柄形状（方案 §2.1）：跨六类可共用的渲染/停止字段。 */
export interface TaskHandle {
  kind: TaskHandleKind;
  /** turn/goal = session id；schedule/plan/run/delegation = 各域自己的 id。 */
  id: string;
  title: string;
  /** 行内副文案（turn 的「第 3 轮 · 已 12 分钟」、plan 的 items 等）。 */
  detail?: string;
  /** 可跳转会话的句柄带 sid；delegation 无（外部会话）。 */
  session_id?: string;
  /** epoch 秒（turn/plan/run/goal/delegation）。 */
  started_at?: number;
  /** schedule 专用：下次触发时间的可读串。 */
  next_fire_at?: string;
  /** false = 按钮置灰 + tooltip 显示 stop_hint（如 parked ask_user 拒停）。 */
  stoppable?: boolean;
  /** stoppable=false 时的原因说明。 */
  stop_hint?: string;
}

/** GET /api/tasks/overview 的载荷（六组全量）。 */
export interface TasksOverview {
  turns: TaskHandle[];
  scheduled: TaskHandle[];
  subagents: TaskHandle[];
  runs: TaskHandle[];
  goals: TaskHandle[];
  delegations: TaskHandle[];
}

export function emptyTasksOverview(): TasksOverview {
  return {
    turns: [],
    scheduled: [],
    subagents: [],
    runs: [],
    goals: [],
    delegations: [],
  };
}

/** 拉一次总览。非 2xx（旧 sidecar 404 / sidecar 未起）抛错，调用方兜底。 */
export async function getTasksOverview(): Promise<TasksOverview> {
  const res = await fetch(`${BASE}/tasks/overview`, { headers: H });
  if (!res.ok) throw new Error(`tasks/overview ${res.status}`);
  return (await res.json()) as TasksOverview;
}

/** 统一停止门面：POST /api/tasks/overview/stop，body 只带 {kind, id}。 */
export async function stopTasksOverviewItem(kind: TaskHandleKind, id: string): Promise<void> {
  const res = await fetch(`${BASE}/tasks/overview/stop`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ kind, id }),
  });
  if (!res.ok) throw new Error(`tasks/overview/stop ${res.status}`);
}
