/**
 * 运行中任务统一面板的纯逻辑层 + 模块级数据 hub（tasks-overview 方案 §2.2）。
 *
 * 为什么是模块级 hub 而不是组件 state：tab 徽标（总活动数）必须在面板未挂载
 * 时也活着——RightPanel 的 tab 条、RightDock 收起态、侧栏品牌行 ▶N 三个展示位
 * 共享同一份快照与同一个 30s 轮询（badge 轮询节流范式，notice.ts 同款
 * 「模块级 listener 数组」手法，宿主挂载即订阅）。
 *
 * 轮询纪律（方案 §2.2）：首个订阅者出现 → 立即拉一次 + 起 30s interval；
 * 最后一个退订 → 停表（快照保留，重新订阅时再拉）。不做 WS（§2.3 明确出界）。
 */

import {
  emptyTasksOverview,
  getTasksOverview,
  stopTasksOverviewItem,
  type TaskHandle,
  type TaskHandleKind,
  type TasksOverview,
} from "./api/tasksOverview";

export type { TaskHandle, TaskHandleKind, TasksOverview };

/** 轮询间隔（方案 §2.2：30s；进 tab 即拉由首个订阅/手动 refresh 负责）。 */
export const TASKS_OVERVIEW_POLL_MS = 30_000;

/** 面板分组渲染顺序（原型 docs/demos/tasks-overview-journey.html）：turn →
 * schedule → plan → run → goal → delegation；空组不渲染。 */
export const OVERVIEW_GROUP_ORDER: ReadonlyArray<{
  key: keyof TasksOverview;
  kind: TaskHandleKind;
}> = [
  { key: "turns", kind: "turn" },
  { key: "scheduled", kind: "schedule" },
  { key: "subagents", kind: "plan" },
  { key: "runs", kind: "run" },
  { key: "goals", kind: "goal" },
  { key: "delegations", kind: "delegation" },
];

/** 防御性归一化：后端字段缺失/类型走样（旧 sidecar 404 的 {"detail":...}、
 * it_1 未合入的空响应）不得炸组件——一律收敛成合法 TasksOverview。 */
function asHandleArray(v: unknown, kind: TaskHandleKind): TaskHandle[] {
  if (!Array.isArray(v)) return [];
  const out: TaskHandle[] = [];
  for (const raw of v) {
    if (!raw || typeof raw !== "object") continue;
    const h = raw as Record<string, unknown>;
    const id = h.id ?? h.session_id;
    if (typeof id !== "string" || !id) continue;
    const title =
      typeof h.title === "string" && h.title
        ? h.title
        : typeof h.session_title === "string"
          ? h.session_title
          : id;
    // detail 兜底：方案 §2.1 各组的原生副文案字段（plan=items、run=node、
    // goal=progress、delegation=elapsed_s）——后端直出 detail 时零损失，
    // 只带原生字段时行内副文案也不空。
    const fallbackDetail =
      typeof h.items === "string"
        ? h.items
        : typeof h.node === "string"
          ? h.node
          : typeof h.progress === "string"
            ? h.progress
            : typeof h.elapsed_s === "number"
              ? `elapsed ${Math.round(h.elapsed_s)}s`
              : undefined;
    out.push({
      kind,
      id,
      title,
      detail: typeof h.detail === "string" ? h.detail : fallbackDetail,
      // turn/goal 的 id 即 sid；plan 显式带 session_id（方案 §2.1）。
      session_id:
        typeof h.session_id === "string"
          ? h.session_id
          : kind === "turn" || kind === "goal"
            ? id
            : undefined,
      started_at: typeof h.started_at === "number" ? h.started_at : undefined,
      next_fire_at: typeof h.next_fire_at === "string" ? h.next_fire_at : undefined,
      stoppable: h.stoppable === false ? false : true,
      stop_hint: typeof h.stop_hint === "string" ? h.stop_hint : undefined,
    });
  }
  return out;
}

/** 任意未知载荷 → 合法 TasksOverview（六组缺失补空）。 */
export function normalizeTasksOverview(raw: unknown): TasksOverview {
  const src = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  return {
    turns: asHandleArray(src.turns, "turn"),
    scheduled: asHandleArray(src.scheduled, "schedule"),
    subagents: asHandleArray(src.subagents, "plan"),
    runs: asHandleArray(src.runs, "run"),
    goals: asHandleArray(src.goals, "goal"),
    delegations: asHandleArray(src.delegations, "delegation"),
  };
}

export interface OverviewGroup {
  key: keyof TasksOverview;
  kind: TaskHandleKind;
  items: TaskHandle[];
}

/** 分组视图：固定顺序、过滤空组（原型语义：0 条的组头不占位）。 */
export function overviewGroups(data: TasksOverview): OverviewGroup[] {
  return OVERVIEW_GROUP_ORDER.map(({ key, kind }) => ({ key, kind, items: data[key] })).filter(
    (g) => g.items.length > 0,
  );
}

/** tab 总徽标 = 六组活动总数（方案 §2.2）。 */
export function overviewTotal(data: TasksOverview): number {
  return OVERVIEW_GROUP_ORDER.reduce((n, { key }) => n + data[key].length, 0);
}

/** 停止确认文案的语义分支：schedule=暂停可恢复，其余=不可撤销（方案 §2.1
 * 安全边界 / 风险 2：门面统一走暂停，删除仍留在设置页）。 */
export type StopConfirmVariant = "pause" | "irreversible";

export function stopConfirmVariant(kind: TaskHandleKind): StopConfirmVariant {
  return kind === "schedule" ? "pause" : "irreversible";
}

/** 句柄是否有可跳转目标（行点击/查看按钮的可达性判定）。 */
export function handleJumpTarget(h: TaskHandle): { type: "session"; sid: string } | {
  type: "workflow";
} | { type: "schedule" } | null {
  if (h.kind === "run") return { type: "workflow" };
  if (h.kind === "schedule") return { type: "schedule" };
  if (h.session_id) return { type: "session", sid: h.session_id };
  return null;
}

// ---- 模块级 hub -------------------------------------------------------------

export interface TasksOverviewState {
  data: TasksOverview;
  /** 最近一次拉取是否成功（false = sidecar 不可达/旧版无端点，保留旧数据）。 */
  ok: boolean;
  /** 是否已成功拉到过（区分「空态」与「从未加载」）。 */
  loaded: boolean;
}

let state: TasksOverviewState = { data: emptyTasksOverview(), ok: false, loaded: false };
let listeners: Array<(s: TasksOverviewState) => void> = [];
let pollTimer: ReturnType<typeof setInterval> | null = null;
let inFlight: Promise<void> | null = null;

function emit() {
  for (const l of listeners) l(state);
}

/** 六组句柄的等值判定（id/kind/title/detail 级）——30s 轮询数值不变时保引用
 * bailout，避免每个周期都触发订阅组件全量重渲染（SidebarFooter 同款纪律）。 */
function sameOverview(a: TasksOverview, b: TasksOverview): boolean {
  for (const { key } of OVERVIEW_GROUP_ORDER) {
    const x = a[key];
    const y = b[key];
    if (x.length !== y.length) return false;
    for (let i = 0; i < x.length; i++) {
      const p = x[i];
      const q = y[i];
      if (
        p.id !== q.id ||
        p.title !== q.title ||
        p.detail !== q.detail ||
        p.stoppable !== q.stoppable ||
        p.stop_hint !== q.stop_hint
      ) {
        return false;
      }
    }
  }
  return true;
}

function setState(next: TasksOverviewState) {
  const unchanged =
    state.ok === next.ok &&
    state.loaded === next.loaded &&
    sameOverview(state.data, next.data);
  if (unchanged) {
    // 内容不变：保留旧 data 引用，订阅组件的浅比较不触发重渲染。
    state = { ...next, data: state.data };
    return;
  }
  state = next;
  emit();
}

/** 立即重拉（进 tab、手动刷新、停止失败回滚共用）。并发调用共享同一 promise。 */
export function refreshTasksOverview(): Promise<void> {
  if (inFlight) return inFlight;
  inFlight = (async () => {
    try {
      const data = normalizeTasksOverview(await getTasksOverview());
      setState({ data, ok: true, loaded: true });
    } catch {
      // sidecar 未起 / it_1 未合入：保旧快照，只降 ok 标记。
      setState({ data: state.data, ok: false, loaded: state.loaded });
    } finally {
      inFlight = null;
    }
  })();
  return inFlight;
}

function startPolling() {
  if (pollTimer) return;
  void refreshTasksOverview();
  pollTimer = setInterval(() => void refreshTasksOverview(), TASKS_OVERVIEW_POLL_MS);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

/** 订阅快照：首个订阅起轮询，最后一个退订停表。注册即回放当前快照。 */
export function subscribeTasksOverview(l: (s: TasksOverviewState) => void): () => void {
  listeners.push(l);
  if (listeners.length === 1) startPolling();
  l(state);
  return () => {
    listeners = listeners.filter((x) => x !== l);
    if (listeners.length === 0) stopPolling();
  };
}

/** 当前快照（非响应式读，供一次性判断）。 */
export function tasksOverviewSnapshot(): TasksOverviewState {
  return state;
}

/**
 * 乐观移除：POST 期间先把句柄摘掉（badge/行即时反馈）。
 * 失败路径由调用方 refreshTasksOverview() 回滚重查（truth 在服务端）。
 */
export function optimisticRemoveHandle(kind: TaskHandleKind, id: string): void {
  const next = emptyTasksOverview();
  for (const { key, kind: k } of OVERVIEW_GROUP_ORDER) {
    next[key] = k === kind ? state.data[key].filter((h) => h.id !== id) : state.data[key];
  }
  setState({ data: next, ok: state.ok, loaded: state.loaded });
}

/**
 * 停止门面调用 + 乐观 UI（方案 §2.2）：先摘行，POST 成功即结束；失败回滚
 * （重拉恢复真实列表）并向上抛错供面板展示错误提示。
 */
export async function stopHandleWithRollback(h: TaskHandle): Promise<void> {
  const prev = state.data;
  optimisticRemoveHandle(h.kind, h.id);
  try {
    await stopTasksOverviewItem(h.kind, h.id);
  } catch (err) {
    // 回滚：以服务端真值恢复（prev 只作 fetch 再失败时的兜底）。
    try {
      await refreshTasksOverview();
    } catch {
      setState({ data: prev, ok: state.ok, loaded: state.loaded });
    }
    throw err;
  }
}

/** 测试专用：清空 hub 单例状态（生产代码不得调用）。 */
export function __resetTasksOverviewHubForTests(): void {
  stopPolling();
  listeners = [];
  inFlight = null;
  state = { data: emptyTasksOverview(), ok: false, loaded: false };
}
