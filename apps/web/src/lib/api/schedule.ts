/**
 * Scheduled tasks.
 *
 * Split out of lib/runtime.ts, which re-exports every name unchanged.
 *
 * 读操作走 json()；**写操作一律 jsonStrict**（schedule-integrity）：非 2xx
 * 直接抛 Error(detail)，绝不把 FastAPI 的 {"detail": ...} 错误体当业务文档
 * 返回——否则 500/400 的「暂停/删除/立即执行」被调用方误判为成功，失败
 * 无从反馈（core.ts 对读改写型调用的既定纪律）。唯一例外是 listSchedule：
 * 它是全页状态源（cfg 直接进 setState），同样必须严格——错误体伪装成配置
 * 会让页面崩在渲染层且 loadedRef=true 后永远不再重试。
 */
import { BASE, H, json, jsonStrict, OVERRIDE_PORT } from "./core";

// ---- scheduled tasks（docs/scheduled-tasks-design.md §5.1；路由/字段名是契约）----

/** 读全量调度配置。严格版（schedule-integrity 收口）：GET /schedule 的
 *  错误体是 JSON（如 500 的 {"detail": ...}），旧的 json() 会把它原样当
 *  配置返回——页面把错误体 set 成 cfg 后 tasks 是 undefined，直接崩在
 *  渲染层且不再重试。这里走 jsonStrict（非 2xx 抛错）+ shape 守卫
 *  （tasks 必须是数组），畸形 200 / 错误体 / 断连统一抛错交给调用方重试。 */
export async function listSchedule(): Promise<import("../types").ScheduleConfig> {
  const r = await jsonStrict<import("../types").ScheduleConfig>(`${BASE}/schedule`);
  if (!r || typeof r !== "object" || !Array.isArray(r.tasks)) {
    throw new Error("malformed schedule config: tasks[] missing");
  }
  return r;
}

// 全局开关 / 保持唤醒。keep_awake 的唯一真值在 runtime（§3.3），前端拿到返回后
// 再同步 Tauri 壳的 caffeinate。类型错（如字符串）会被 runtime 400 拒掉。
export async function putSchedule(body: {
  enabled?: boolean;
  keep_awake?: boolean;
}): Promise<import("../types").ScheduleConfig & { ok?: boolean; error?: string }> {
  return jsonStrict(`${BASE}/schedule`, { method: "PUT", headers: H, body: JSON.stringify(body) });
}

/** 新建任务。间隔 <5 分钟 / workflow 必填输入缺失 → 抛 Error(detail)
 *  （jsonStrict；调用方 catch 后展示 detail）。 */
export async function createScheduleTask(
  task: Partial<import("../types").ScheduleTask>,
): Promise<import("../types").ScheduleTask & { ok?: boolean; error?: string; detail?: string }> {
  return jsonStrict(`${BASE}/schedule/tasks`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(task),
  });
}

export async function patchScheduleTask(
  id: string,
  patch: Partial<import("../types").ScheduleTask>,
): Promise<import("../types").ScheduleTask & { ok?: boolean; error?: string; detail?: string }> {
  return jsonStrict(`${BASE}/schedule/tasks/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: H,
    body: JSON.stringify(patch),
  });
}

export async function deleteScheduleTask(id: string): Promise<{ ok?: boolean; removed?: boolean }> {
  return jsonStrict(`${BASE}/schedule/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/** 立即执行（manual 触发，不计入计划），返回 run_id。
 *  网络层异常 = 回执丢失（请求可能已生效）：调用方按「结果未知」处理，
 *  不得盲目重发。 */
export async function runScheduleTaskNow(
  id: string,
): Promise<{ ok?: boolean; run_id?: string; detail?: string }> {
  return jsonStrict(`${BASE}/schedule/tasks/${encodeURIComponent(id)}/run`, { method: "POST" });
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
