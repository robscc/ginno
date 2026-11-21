/**
 * 运行中任务统一面板（tasks-overview 方案 §2.1）的 API 客户端。
 *
 * it_1（后端聚合端点）未合入期间，`getTasksOverview` 遇 404/旧 sidecar 抛错，
 * 由 lib/tasksOverview 的 hub 兜底为空态——前端先落地不阻塞联调。
 */

import { BASE, H } from "./core";

/** 六类句柄的 kind（与后端聚合 API 的分组一一对应）。 */
export type TaskHandleKind = "turn" | "schedule" | "plan" | "run" | "goal" | "delegation";

/**
 * 句柄三态（2026-11-21 分类修订）：总览视图按「执行中 / 等你处理 / 已排期」
 * 三档分组。phase 由服务端按真源判定（parked turn → waiting、schedule →
 * scheduled、其余 → running），旧 sidecar 无此字段时前端按 kind/stoppable
 * 兜底派生（见 normalizeTasksOverview）。
 */
export type TaskPhase = "running" | "waiting" | "scheduled";

export const TASK_PHASES: readonly TaskPhase[] = ["running", "waiting", "scheduled"];

/** 统一句柄形状（方案 §2.1）：跨六类可共用的渲染/停止字段。 */
export interface TaskHandle {
  kind: TaskHandleKind;
  /** 三态分类（服务端真源判定；normalizeTasksOverview 恒赋值；手写句柄
   * 字面量可省略——读取走 phaseOf()，缺省按 running 处理）。 */
  phase?: TaskPhase;
  /** turn/goal = session id；schedule/plan/run/delegation = 各域自己的 id。 */
  id: string;
  title: string;
  /** 行内副文案（turn 的「第 3 轮 · 已 12 分钟」、plan 的 items 等）。 */
  detail?: string;
  /** 可跳转会话的句柄带 sid；delegation 无（外部会话）。 */
  session_id?: string;
  /** epoch 秒（turn/plan/run/goal/delegation）。 */
  started_at?: number;
  /** schedule 专用：下次触发的 epoch 秒（2026-11-21 契约修订：后端
   * next_run_at 本就是数字，前端格式化为本地语言/时区展示，不再收字符串）。 */
  next_fire_at?: number;
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

/**
 * 停止请求的强类型载荷：plan 的 cancel 是 per-session 路由（plan_id 全局
 * 不唯一），后端对缺 session_id 的 plan 停止回 400（tasks_overview.py
 * StopRequest 契约）——类型层面就强制带上，编译期拦住漏参调用。
 */
export type StopOverviewItemRequest =
  | { kind: Extract<TaskHandleKind, "plan">; id: string; session_id: string }
  | { kind: Exclude<TaskHandleKind, "plan">; id: string; session_id?: string };

/** 统一停止门面：POST /api/tasks/overview/stop。plan 必带 session_id，
 * 其余五类 session_id 可选透传（有则一并送，服务端不依赖）。 */
export async function stopTasksOverviewItem(req: StopOverviewItemRequest): Promise<void> {
  const body: Record<string, unknown> = { kind: req.kind, id: req.id };
  if (req.session_id) body.session_id = req.session_id;
  const res = await fetch(`${BASE}/tasks/overview/stop`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`tasks/overview/stop ${res.status}`);
}
