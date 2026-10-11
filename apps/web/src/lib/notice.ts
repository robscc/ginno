/**
 * 全局通知中心总线（notice-center-design.md）。
 *
 * 为什么是模块级总线而不是 React context：通知的**主要来源是 socket 回调**
 * （`useChatStreamEngine` 的 WS 事件处理活在组件树之外，且闭包生命周期长于
 * 单次 render），context 的 hook 够不到那里。沿用 ModToastHost 已验证的
 * 「模块级 listener 数组」手法，宿主 `NoticeHost` 挂载后订阅即可，调用点零依赖。
 *
 * 与 chat/mod/ModToastHost（右下角，mods 子系统自己的回执）并存：
 * 这里是**跨视图、需要用户知晓**的通知的统一出口（模型热切、连接器 fallback …）。
 */

export type NoticeLevel = "info" | "success" | "warn" | "error";

/** 归因标签：只用于埋点/将来做分类过滤，不影响渲染。 */
export type NoticeSource = "model" | "connector" | "mod" | "general";

export interface NoticeAction {
  label: string;
  onClick: () => void;
}

export interface NoticeOptions {
  /** 同一 key 在 TTL 内**刷新**已存在的那条（重置倒计时）而非新堆一条。
   *  解决「多窗口/多视图收到同一 WS 事件各弹一条」的噪声。 */
  dedupeKey?: string;
  /** 自动消失毫秒；`0` = 常驻（需手动关）。默认 error 8000，其余 6000。 */
  duration?: number;
  /** 主按钮（最多一个），如「Model settings」。 */
  action?: NoticeAction;
  /** 来源标签。 */
  source?: NoticeSource;
}

export interface Notice extends NoticeOptions {
  id: number;
  level: NoticeLevel;
  title?: string;
  text: string;
  /** 进场标记：false → 首帧透明，下一帧转 true 触发 CSS 过渡。 */
  entered: boolean;
}

export const NOTICE_DEDUPE_TTL_MS = 6000;
export const NOTICE_DEFAULT_DURATION_MS = 6000;
export const NOTICE_ERROR_DURATION_MS = 8000;
/** 堆叠上限：超出挤掉最旧的一条。 */
export const NOTICE_MAX = 3;
/** 进出场过渡时长，与 NoticeHost 的 CSS duration 保持一致。 */
export const NOTICE_EXIT_MS = 180;

type Listener = (notices: Notice[]) => void;

interface TimerEntry {
  t: ReturnType<typeof setTimeout> | null;
  /** 计划消失的绝对时刻；暂停期间保留，恢复时据此算剩余。 */
  expiresAt: number;
  paused: boolean;
}

/** 模块级单例状态——宿主只订阅不 own，卸载/重挂不丢在途通知。 */
let items: Notice[] = [];
let listeners: Listener[] = [];
let uid = 0;
const timers = new Map<number, TimerEntry>();

function emit() {
  for (const l of listeners) {
    try {
      l(items);
    } catch {
      /* 单个订阅者异常不影响其他 */
    }
  }
}

function durationFor(level: NoticeLevel): number {
  return level === "error" ? NOTICE_ERROR_DURATION_MS : NOTICE_DEFAULT_DURATION_MS;
}

function arm(id: number, duration: number) {
  const entry = timers.get(id);
  if (entry?.t) clearTimeout(entry.t);
  if (!duration) {
    timers.set(id, { t: null, expiresAt: Infinity, paused: false });
    return;
  }
  timers.set(id, {
    t: setTimeout(() => dismissNotice(id), duration),
    expiresAt: Date.now() + duration,
    paused: false,
  });
}

/** 移除一条通知：先 entered=false 走出场过渡，再真正摘除。幂等。 */
export function dismissNotice(id: number) {
  const entry = timers.get(id);
  if (entry?.t) clearTimeout(entry.t);
  timers.delete(id);
  const cur = items.find((n) => n.id === id);
  if (!cur || !cur.entered) return;
  items = items.map((n) => (n.id === id ? { ...n, entered: false } : n));
  emit();
  setTimeout(() => {
    if (!items.some((n) => n.id === id)) return;
    items = items.filter((n) => n.id !== id);
    emit();
  }, NOTICE_EXIT_MS);
}

/** 悬停：暂停倒计时（常驻通知是 no-op）。 */
export function pauseNotice(id: number) {
  const entry = timers.get(id);
  if (!entry || entry.paused || entry.expiresAt === Infinity) return;
  if (entry.t) clearTimeout(entry.t);
  timers.set(id, { ...entry, t: null, paused: true });
}

/** 移出：按剩余时间续走倒计时。 */
export function resumeNotice(id: number) {
  const entry = timers.get(id);
  if (!entry?.paused) return;
  const remaining = Math.max(0, entry.expiresAt - Date.now());
  timers.set(id, {
    t: setTimeout(() => dismissNotice(id), remaining),
    expiresAt: Date.now() + remaining,
    paused: false,
  });
}

/**
 * 推一条通知。宿主未挂载时只进模块状态（挂载后立即回放），
 * 因此「通知在 Host 挂载前到达」不会像旧实现那样被静默丢弃。
 */
export function pushNotice(level: NoticeLevel, text: string, opts: NoticeOptions = {}) {
  if (!text) return;

  if (opts.dedupeKey) {
    const prev = items.find((n) => n.dedupeKey === opts.dedupeKey && n.entered);
    if (prev) {
      items = items.map((n) =>
        n.id === prev.id ? { ...n, ...opts, level, text, id: n.id, entered: true } : n,
      );
      emit();
      arm(prev.id, opts.duration ?? durationFor(level));
      return;
    }
    // 正在退场的同 key 残影直接踢掉，避免它和新的那条并存一瞬。
    items = items.filter((n) => n.dedupeKey !== opts.dedupeKey);
  }

  const n: Notice = { id: ++uid, level, text, ...opts, entered: false };
  items = [...items.slice(-(NOTICE_MAX - 1)), n];
  emit();
  // 首帧透明 → 下一帧转 true，让 CSS 过渡真的跑起来。
  requestAnimationFrame(() => {
    items = items.map((x) => (x.id === n.id ? { ...x, entered: true } : x));
    emit();
  });
  arm(n.id, opts.duration ?? durationFor(level));
}

export function subscribeNotices(l: Listener): () => void {
  listeners.push(l);
  l(items);
  return () => {
    listeners = listeners.filter((x) => x !== l);
  };
}

/** 清空全部（登出/测试）。 */
export function clearNotices() {
  for (const { t } of timers.values()) if (t) clearTimeout(t);
  timers.clear();
  items = [];
  emit();
}