/**
 * 运行计数 hub —— 常驻徽标的轻量订阅源（SidebarHeader ▶N / RightPanel
 * 「运行」tab），30s 轮询 /api/tasks/counts。
 *
 * 与 lib/tasksOverview（明细 hub）的分工：徽标常驻每个页面，不能为它挂
 * 面板或拉六源明细——counts 端点只回六个数字，服务端跑 mtime 增量索引。
 * 打开「运行」面板时明细仍走既有的 tasksOverview 列表订阅，本 hub 不
 * 感知面板；面板打开期间把轮询间隔降到 5s（停止动作后徽标 ≤5s 收敛），
 * 关闭恢复 30s（setTasksCountsPollMs，由 RightPanel 驱动）。
 *
 * 纪律与 tasksOverview hub 同款：失败保旧快照只降 ok 标记；内容不变保
 * 引用 bailout；首订阅起轮询、末退订停表；并发刷新共享同一 promise。
 */

import { emptyTasksCounts, getTasksCounts, type TasksCounts } from "./api/tasksCounts";

export const TASKS_COUNTS_POLL_MS = 30_000;
/** 面板打开期间的快轮询间隔（用户刚做过停止等操作，徽标要跟手）。 */
export const TASKS_COUNTS_OPEN_PANEL_POLL_MS = 5_000;
const POLL_MS_FLOOR = 1_000;

export interface TasksCountsState {
  counts: TasksCounts;
  /** 最近一次拉取是否成功（false = sidecar 不可达/旧版无端点，保留旧数据）。 */
  ok: boolean;
  /** 是否已成功拉到过（区分「空态」与「从未加载」）。 */
  loaded: boolean;
}

let state: TasksCountsState = { counts: emptyTasksCounts(), ok: false, loaded: false };
let listeners: Array<(s: TasksCountsState) => void> = [];
let pollTimer: ReturnType<typeof setInterval> | null = null;
let inFlight: Promise<void> | null = null;
let pollMs = TASKS_COUNTS_POLL_MS;

function emit() {
  for (const l of listeners) l(state);
}

function setState(next: TasksCountsState) {
  const unchanged =
    state.ok === next.ok &&
    state.loaded === next.loaded &&
    (Object.keys(state.counts) as Array<keyof TasksCounts>).every(
      (k) => state.counts[k] === next.counts[k],
    );
  if (unchanged) {
    // 数值不变：保引用 bailout，订阅组件的浅比较不触发重渲染。
    state = { ...next, counts: state.counts };
    return;
  }
  state = { ...next, counts: next.counts };
  emit();
}

/** 立即重拉。并发调用共享同一 promise（不叠加请求）。 */
export function refreshTasksCounts(): Promise<void> {
  if (inFlight) return inFlight;
  inFlight = (async () => {
    try {
      const counts = await getTasksCounts();
      setState({ counts, ok: true, loaded: true });
    } catch {
      // sidecar 未起 / 旧版无端点：保旧快照，只降 ok 标记。
      setState({ counts: state.counts, ok: false, loaded: state.loaded });
    } finally {
      inFlight = null;
    }
  })();
  return inFlight;
}

function startPolling() {
  if (pollTimer) return;
  void refreshTasksCounts();
  pollTimer = setInterval(() => void refreshTasksCounts(), pollMs);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

/**
 * 调整轮询间隔（RightPanel：运行 tab 打开 5s / 关闭 30s）。轮询未激活时
 * 只记值，下次 startPolling 生效；激活中则原地换表（不清已排定的下一跳，
 * 只影响之后的节奏）。
 */
export function setTasksCountsPollMs(ms: number): void {
  const next = Math.max(POLL_MS_FLOOR, Math.floor(ms));
  if (next === pollMs) return;
  pollMs = next;
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = setInterval(() => void refreshTasksCounts(), pollMs);
  }
}

/** 订阅快照：首个订阅起轮询，最后一个退订停表。注册即回放当前快照。 */
export function subscribeTasksCounts(l: (s: TasksCountsState) => void): () => void {
  listeners.push(l);
  if (listeners.length === 1) startPolling();
  l(state);
  return () => {
    listeners = listeners.filter((x) => x !== l);
    if (listeners.length === 0) stopPolling();
  };
}

/** 当前快照（非响应式读，供 useState 惰性初始化）。 */
export function tasksCountsSnapshot(): TasksCountsState {
  return state;
}

/** 测试专用：清空 hub 单例状态（生产代码不得调用）。 */
export function __resetTasksCountsHubForTests(): void {
  stopPolling();
  listeners = [];
  inFlight = null;
  pollMs = TASKS_COUNTS_POLL_MS;
  state = { counts: emptyTasksCounts(), ok: false, loaded: false };
}
