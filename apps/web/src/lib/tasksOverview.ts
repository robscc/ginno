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
  type StopOverviewItemRequest,
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

/**
 * 行/停止的完整身份键：kind + id + session_id。plan_id 全局不唯一（同一
 * plan_id 可能出现在不同会话的枚举里），只有连 session_id 一起比才是同一行
 * ——React key、停止在途集合、成功停止时间戳都用它，防止并发停止/乐观移除
 * 误伤同 id 的别会话句柄。turn/goal 的 id 即 session_id，缺失时派生补齐
 * （与 normalizeTasksOverview 的派生一致，保证两侧键恒等）。
 */
export function handleKey(h: Pick<TaskHandle, "kind" | "id" | "session_id">): string {
  const sid = h.session_id ?? (h.kind === "turn" || h.kind === "goal" ? h.id : undefined);
  return sid ? `${h.kind}:${h.id}:${sid}` : `${h.kind}:${h.id}`;
}

/**
 * next_fire_at（epoch 秒）→ 本地语言/时区的可读时间。locale 由调用方传
 * （面板取 next-intl 的 useLocale()），时区不传即系统本地时区；测试可注入
 * 固定 timeZone 断言。无效值（<=0/NaN）返回空串，调用方自行跳过渲染。
 */
export function formatNextFireAt(epochSec: number, locale: string, timeZone?: string): string {
  if (!Number.isFinite(epochSec) || epochSec <= 0) return "";
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "short",
    timeStyle: "short",
    ...(timeZone ? { timeZone } : {}),
  }).format(new Date(epochSec * 1000));
}

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
      // 契约（2026-11-21 修订）：next_fire_at 是正的 epoch 秒数字；旧字符串
      // （"明天 08:00"）/NaN/非正数一律丢弃——展示层按本地语言/时区统一格式化。
      next_fire_at:
        typeof h.next_fire_at === "number" && Number.isFinite(h.next_fire_at) && h.next_fire_at > 0
          ? h.next_fire_at
          : undefined,
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
  /** 拉取在途（仅首拉/从未成功后的重试置位；常规 30s 轮询不打扰视图）。 */
  loading: boolean;
}

let state: TasksOverviewState = {
  data: emptyTasksOverview(),
  ok: false,
  loaded: false,
  loading: false,
};
let listeners: Array<(s: TasksOverviewState) => void> = [];
let pollTimer: ReturnType<typeof setInterval> | null = null;
let inFlight: Promise<boolean> | null = null;
/**
 * 停止在途的完整身份键（handleKey）：POST /stop 未落定时，轮询可能拉回
 * 「服务端尚未消化停止」的旧快照，把乐观移除的行闪回。刷新结果在写入
 * state 前按此集合过滤；POST 结束后清除（失败路径随后 refresh 以服务端
 * 真值回滚）。
 */
const stoppingIds = new Set<string>();
// 只记录当前 GET 飞行期间成功停止的身份；请求结束即可释放，不保留历史句柄。
let stoppedDuringFetch: Set<string> | null = null;

function emit() {
  for (const l of listeners) l(state);
}

/** 六组句柄的等值判定——30s 轮询数值不变时保引用 bailout，避免每个周期都
 * 触发订阅组件全量重渲染（SidebarFooter 同款纪律）。必须覆盖所有「改变展示
 * 或跳转」的字段：title/detail（文本）、session_id（跳转目标）、started_at
 * （相对时间展示）、next_fire_at（schedule 行时间）、stoppable/stop_hint
 * （按钮态）。 */
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
        p.session_id !== q.session_id ||
        p.started_at !== q.started_at ||
        p.next_fire_at !== q.next_fire_at ||
        p.stoppable !== q.stoppable ||
        p.stop_hint !== q.stop_hint
      ) {
        return false;
      }
    }
  }
  return true;
}

function setState(next: TasksOverviewState, stale?: Set<string>) {
  let data = next.data;
  if (stoppingIds.size || stale?.size) {
    const filtered = emptyTasksOverview();
    let touched = false;
    for (const { key } of OVERVIEW_GROUP_ORDER) {
      const kept = data[key].filter((h) => {
        const k = handleKey(h);
        if (stoppingIds.has(k) || stale?.has(k)) {
          touched = true;
          return false;
        }
        return true;
      });
      filtered[key] = kept;
    }
    if (touched) data = filtered;
  }
  const unchanged =
    state.ok === next.ok &&
    state.loaded === next.loaded &&
    state.loading === next.loading &&
    sameOverview(state.data, data);
  if (unchanged) {
    // 内容不变：保留旧 data 引用，订阅组件的浅比较不触发重渲染。
    state = { ...next, data: state.data };
    return;
  }
  state = { ...next, data };
  emit();
}

/** 立即重拉（进 tab、手动刷新、停止失败回滚、后台性能项共用）。
 * 并发调用共享同一 promise；返回本轮是否拉取成功（false = sidecar 不可达，
 * 旧快照保留）。 */
export function refreshTasksOverview(): Promise<boolean> {
  if (inFlight) return inFlight;
  const stale = new Set<string>();
  stoppedDuringFetch = stale;
  inFlight = (async () => {
    try {
      // 仅首拉/从未成功后的重试置 loading（常规 30s 轮询不打扰已加载视图）。
      if (!state.loaded) setState({ ...state, loading: true });
      const raw = await getTasksOverview();
      const data = normalizeTasksOverview(raw);
      setState({ data, ok: true, loaded: true, loading: false }, stale);
      return true;
    } catch {
      // sidecar 未起 / it_1 未合入：保旧快照，只降 ok 标记。
      setState({ data: state.data, ok: false, loaded: state.loaded, loading: false });
      return false;
    } finally {
      stoppedDuringFetch = null;
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
 * 乐观移除：POST 期间先把句柄摘掉（badge/行即时反馈）。按完整身份
 * （handleKey）过滤——同 plan_id 不同会话的两行只能摘目标行。
 * 失败路径由调用方 stopHandleWithRollback 回滚（truth 在服务端）。
 */
export function optimisticRemoveHandle(target: Pick<TaskHandle, "kind" | "id" | "session_id">): void {
  const key = handleKey(target);
  const next = emptyTasksOverview();
  for (const { key: groupKey, kind } of OVERVIEW_GROUP_ORDER) {
    next[groupKey] =
      kind === target.kind
        ? state.data[groupKey].filter((h) => handleKey(h) !== key)
        : state.data[groupKey];
  }
  setState({ data: next, ok: state.ok, loaded: state.loaded, loading: state.loading });
}

/** TaskHandle → 强类型停止请求：plan 缺少身份时在乐观移除与发请求前拒绝。 */
function stopRequestOf(h: TaskHandle): StopOverviewItemRequest {
  if (h.kind === "plan") {
    if (!h.session_id?.trim()) throw new Error("Missing plan session_id");
    return { kind: "plan", id: h.id, session_id: h.session_id };
  }
  return h.session_id
    ? { kind: h.kind, id: h.id, session_id: h.session_id }
    : { kind: h.kind, id: h.id };
}

/** 双失败兜底：POST 与紧随的刷新都失败时，只把刚移除的这一项还原——
 * 绝不整表回写 prev（那会把并发期间已成功停止的其他句柄一并复活）。 */
function restoreSingleHandle(h: TaskHandle): void {
  const k = handleKey(h);
  if (state.data[OVERVIEW_GROUP_ORDER.find((g) => g.kind === h.kind)!.key].some(
    (x) => handleKey(x) === k,
  )) {
    return; // 已在（并发刷新带回了真值），不重复塞。
  }
  const next = emptyTasksOverview();
  for (const { key, kind } of OVERVIEW_GROUP_ORDER) {
    next[key] = kind === h.kind ? [...state.data[key], h] : state.data[key];
  }
  setState({ data: next, ok: state.ok, loaded: state.loaded, loading: state.loading });
}

/**
 * 停止门面调用 + 乐观 UI（方案 §2.2）：先摘行，POST 成功即结束；失败先重拉
 * 服务端真值回滚，重拉也失败（双失败）才退化为只还原本项。全程向上抛错供
 * 面板展示错误提示。
 */
export async function stopHandleWithRollback(h: TaskHandle): Promise<void> {
  const key = handleKey(h);
  const request = stopRequestOf(h);
  stoppingIds.add(key);
  optimisticRemoveHandle(h);
  try {
    await stopTasksOverviewItem(request);
    stoppingIds.delete(key);
    stoppedDuringFetch?.add(key);
  } catch (err) {
    stoppingIds.delete(key);
    // 回滚：优先以服务端真值恢复；GET 也失败（双失败）时只还原本项。
    const refreshed = await refreshTasksOverview().catch(() => false);
    if (!refreshed) restoreSingleHandle(h);
    throw err;
  }
}

/** 测试专用：清空 hub 单例状态（生产代码不得调用）。 */
export function __resetTasksOverviewHubForTests(): void {
  stopPolling();
  listeners = [];
  inFlight = null;
  stoppingIds.clear();
  stoppedDuringFetch = null;
  state = { data: emptyTasksOverview(), ok: false, loaded: false, loading: false };
}
