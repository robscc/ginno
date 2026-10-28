/**
 * Background tasks (docs/background-tasks-design.md §6.2/§7 REST).
 *
 * 任务中心页与聊天内 TaskCard 都以轮询消费（1–2s while running）；WS 是
 * per-session 的，任务天然跨 session，轮询是既定契约。
 */

import { BASE, H, json } from "./core";

export interface TaskRecord {
  id: string;
  command: string;
  title: string;
  cwd: string;
  status: "running" | "finished" | "stopped" | "timeout" | "failed_to_start" | "interrupted";
  exit_code: number | null;
  pid: number | null;
  origin_session: string | null;
  notify: boolean;
  mode: string;
  healthy_pattern?: string;
  timeout_s: number | null;
  detached: boolean;
  created_at: number;
  started_at: number | null;
  ended_at: number | null;
  log_bytes?: number;
  truncated?: boolean;
  watch_muted?: boolean;
  dur?: string;
  interrupted_note?: string;
  exit_note?: string;
}

export const TASK_TERMINAL: ReadonlySet<string> = new Set([
  "finished",
  "stopped",
  "timeout",
  "failed_to_start",
  "interrupted",
]);

export function listTasks() {
  return json<TaskRecord[]>(`${BASE}/tasks`);
}

export function getTask(taskId: string) {
  return json<TaskRecord>(`${BASE}/tasks/${taskId}`);
}

export function taskLogs(taskId: string, opts: { tail?: number; offset?: number } = {}) {
  const q = new URLSearchParams();
  if (opts.tail !== undefined) q.set("tail", String(opts.tail));
  if (opts.offset !== undefined) q.set("offset", String(opts.offset));
  return json<{ status: string; lines: string[]; total: number }>(
    `${BASE}/tasks/${taskId}/logs?${q.toString()}`,
  );
}

export function stopTask(taskId: string) {
  return json<{ ok: boolean; task: TaskRecord }>(`${BASE}/tasks/${taskId}/stop`, {
    method: "POST",
    headers: H,
  });
}

export function removeTask(taskId: string) {
  return json<{ ok: boolean }>(`${BASE}/tasks/${taskId}`, { method: "DELETE", headers: H });
}
