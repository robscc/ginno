/**
 * Scheduled tasks.
 *
 * Split out of lib/runtime.ts, which re-exports every name unchanged.
 */
import { BASE, H, json, OVERRIDE_PORT } from "./core";

// ---- scheduled tasks（docs/scheduled-tasks-design.md §5.1；路由/字段名是契约）----

export async function listSchedule(): Promise<import("../types").ScheduleConfig> {
  return json(`${BASE}/schedule`);
}

// 全局开关 / 保持唤醒。keep_awake 的唯一真值在 runtime（§3.3），前端拿到返回后
// 再同步 Tauri 壳的 caffeinate。
export async function putSchedule(body: {
  enabled?: boolean;
  keep_awake?: boolean;
}): Promise<import("../types").ScheduleConfig & { ok?: boolean; error?: string }> {
  return json(`${BASE}/schedule`, { method: "PUT", headers: H, body: JSON.stringify(body) });
}

/** 新建任务。间隔 <5 分钟 / workflow 必填输入缺失 → HTTP 400 {detail}
 *  （json() 不抛错，按 summarizeSessionToDsl 的约定透出 detail）。 */
export async function createScheduleTask(
  task: Partial<import("../types").ScheduleTask>,
): Promise<import("../types").ScheduleTask & { ok?: boolean; error?: string; detail?: string }> {
  return json(`${BASE}/schedule/tasks`, { method: "POST", headers: H, body: JSON.stringify(task) });
}

export async function patchScheduleTask(
  id: string,
  patch: Partial<import("../types").ScheduleTask>,
): Promise<import("../types").ScheduleTask & { ok?: boolean; error?: string; detail?: string }> {
  return json(`${BASE}/schedule/tasks/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: H,
    body: JSON.stringify(patch),
  });
}

export async function deleteScheduleTask(id: string): Promise<{ ok?: boolean; removed?: boolean }> {
  return json(`${BASE}/schedule/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/** 立即执行（manual 触发，不计入计划），返回 run_id。 */
export async function runScheduleTaskNow(
  id: string,
): Promise<{ ok?: boolean; run_id?: string; detail?: string }> {
  return json(`${BASE}/schedule/tasks/${encodeURIComponent(id)}/run`, { method: "POST" });
}

export async function listScheduleRuns(
  opts: {
    date?: string;
    task_id?: string;
    target_type?: string;
    status?: string;
    trigger?: string;
    /** "unseen" | "seen"（schedule-unseen 过滤） */
    seen?: string;
    sort?: "asc" | "desc";
    page?: number;
  } = {},
): Promise<import("../types").ScheduleRunsPage> {
  const q = new URLSearchParams();
  if (opts.date) q.set("date", opts.date);
  if (opts.task_id) q.set("task_id", opts.task_id);
  if (opts.target_type) q.set("target_type", opts.target_type);
  if (opts.status) q.set("status", opts.status);
  if (opts.trigger) q.set("trigger", opts.trigger);
  if (opts.seen) q.set("seen", opts.seen);
  if (opts.sort) q.set("sort", opts.sort);
  if (opts.page) q.set("page", String(opts.page));
  const s = q.toString();
  return json(`${BASE}/schedule/runs${s ? `?${s}` : ""}`);
}

/** 影子会话 fork 为普通会话（schedule-followup）：新会话复制该轮全部历史
 *  （id 重铸），返回新会话 id；影子会话与后续定时轮次不受影响。 */
export async function forkScheduleRun(runId: string) {
  return json<{ ok: boolean; session_id?: string; error?: string }>(
    `${BASE}/schedule/runs/${encodeURIComponent(runId)}/fork`,
    { method: "POST", headers: H },
  );
}

/** 标记执行结果已读（schedule-unseen）：单条 {run_id} 或一键 {all:true}。
 *  返回最新 unseen 摘要；侧栏红点/页签角标经 runs_seen WS 事件即时刷新。 */
export async function markScheduleRunsSeen(
  body: { run_id?: string; all?: boolean },
): Promise<{ ok?: boolean; unseen?: import("../types").ScheduleUnseen }> {
  return json(`${BASE}/schedule/runs/seen`, { method: "POST", headers: H, body: JSON.stringify(body) });
}

export async function getScheduleTimeline(
  date: string,
): Promise<import("../types").ScheduleTimeline> {
  return json(`${BASE}/schedule/timeline?date=${encodeURIComponent(date)}`);
}

// 页面打开期间的事件通道（§6）：run_started / run_finished / task_updated /
// missed → 各页自行刷新；关页即断，不轮询。URL 模式照 wsConnectorsUrl()。
export function wsScheduleUrl(): string {
  const proto = typeof window !== "undefined" && window.location.protocol === "https:" ? "wss:" : "ws:";
  if (typeof window !== "undefined") {
    const host = OVERRIDE_PORT
      ? `${window.location.hostname}:${OVERRIDE_PORT}`
      : window.location.host;
    return `${proto}//${host}/api/ws/schedule`;
  }
  return `ws://127.0.0.1:${OVERRIDE_PORT ?? 8787}/api/ws/schedule`;
}
