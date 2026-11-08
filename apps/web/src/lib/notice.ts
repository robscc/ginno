/**
 * 全局通知中心总线（notice-center-design.md；mods toast 收编见
 * notice-unification-design.md §4；未读/已读通道见同文档 §3）。
 *
 * 为什么是模块级总线而不是 React context：通知的**主要来源是 socket 回调**
 * （`useChatStreamEngine` 的 WS 事件处理活在组件树之外，且闭包生命周期长于
 * 单次 render），context 的 hook 够不到那里。沿用 mods 时代已验证的
 * 「模块级 listener 数组」手法，宿主 `NoticeHost` 挂载后订阅即可，调用点零依赖。
 *
 * 这里是**全应用唯一**的悬浮通知出口（模型热切、连接器 fallback、mods
 * toast、MCP 设置回执……统一经 pushNotice 进右下角容器渲染）。
 *
 * P1 未读/已读通道（§3）：
 *  - 每条 notice 同时进「toast 队列」（mode:"toast" 才弹卡）与「收件箱」
 *    （全部进，含 silent）；toast 超时自动消失**不算已读**，条目留在收件箱；
 *  - 已读状态机四种触发全部收敛到 markNoticeRead / markAllNoticesRead；
 *  - 收件箱持久化 localStorage（`ginno:noticebox`，最近 100 条滚动窗口，
 *    只存语义数据不含动画态），加载失败静默重置；
 *  - `countInBadge:false`（session 投影条目）不计入未读数——badge 的会话
 *    部分只信 session store，防双算（§3.3）。
 */

export type NoticeLevel = "info" | "success" | "warn" | "error";

/** 归因标签：只用于埋点/将来做分类过滤，不影响渲染。 */
export type NoticeSource = "model" | "connector" | "mod" | "general";

/** 渲染模式：toast 弹卡片；silent 只进收件箱+badge（不打扰型，§3.1）。 */
export type NoticeMode = "toast" | "silent";

export interface NoticeAction {
  label: string;
  onClick: () => void;
}

/** 持久化后的行为重建元数据：action 回调不可序列化，收件箱条目跨重启
 * 保留「点开会话」这类可重建行为（P1 仅 session 一种）。 */
export interface NoticeActionMeta {
  kind: "session";
  id: string;
}

export interface NoticeOptions {
  /** 同一 key 在 TTL 内**刷新**已存在的那条（重置倒计时）而非新堆一条。
   *  解决「多窗口/多视图收到同一 WS 事件各弹一条」的噪声。
   *  P1 起收件箱同样按 key 刷新（不堆叠，刷新视为新未读）。 */
  dedupeKey?: string;
  /** 自动消失毫秒；`0` = 常驻（需手动关）。默认 error 8000，其余 6000。
   *  silent 模式不弹卡片，该值无效果。 */
  duration?: number;
  /** 主按钮（最多一个），如「Model settings」。 */
  action?: NoticeAction;
  /** 来源标签。 */
  source?: NoticeSource;
  /** 渲染模式，默认 toast（§3.1）。 */
  mode?: NoticeMode;
  /** 语义标签（如 "session"）：收件箱据此选择条目行为与图标。 */
  kind?: string;
  /** 是否计入 badge 未读数，默认 true。`kind:"session"` 投影条目必须
   *  false——会话未读只信 session store，防双算（§3.3）。 */
  countInBadge?: boolean;
  /** 行为重建元数据（跨持久化保留的点击行为）。 */
  actionMeta?: NoticeActionMeta;
  /** false = 纯瞬时 toast：不进通知中心、不计 badge、不留档（2026-11-08
   *  用户反馈「切换模型没必要进通知面板」）。默认 true（进收件箱留档）。
   *  适用于操作确认类反馈；错误/结果回传类保持默认。 */
  inbox?: boolean;
}

export interface Notice extends NoticeOptions {
  id: number;
  level: NoticeLevel;
  title?: string;
  text: string;
  /** 进场标记：false → 首帧透明，下一帧转 true 触发 CSS 过渡。 */
  entered: boolean;
}

/** 收件箱条目：语义数据的持久化形态（不含 entered/action 等不可序列化态）。 */
export interface InboxEntry {
  id: number;
  level: NoticeLevel;
  title?: string;
  text: string;
  createdAt: number;
  /** null = 未读（§3.1）。 */
  readAt: number | null;
  mode: NoticeMode;
  kind?: string;
  countInBadge: boolean;
  source?: NoticeSource;
  dedupeKey?: string;
  actionMeta?: NoticeActionMeta;
}

export const NOTICE_DEDUPE_TTL_MS = 6000;
export const NOTICE_DEFAULT_DURATION_MS = 6000;
export const NOTICE_ERROR_DURATION_MS = 8000;
/** toast 堆叠上限：超出挤掉最旧的一条。 */
export const NOTICE_MAX = 3;
/** 进出场过渡时长，与 NoticeHost 的 CSS duration 保持一致。 */
export const NOTICE_EXIT_MS = 180;

/** 收件箱持久化 key 与滚动窗口上限（§3.4）。 */
export const NOTICEBOX_KEY = "ginno:noticebox";
export const NOTICEBOX_MAX = 100;

type Listener = (notices: Notice[]) => void;
type InboxListener = (entries: InboxEntry[]) => void;
type CountListener = (count: number) => void;

interface TimerEntry {
  t: ReturnType<typeof setTimeout> | null;
  /** 计划消失的绝对时刻；暂停期间保留，恢复时据此算剩余。 */
  expiresAt: number;
  paused: boolean;
}

/** 模块级单例状态——宿主只订阅不 own，卸载/重挂不丢在途通知。 */
let items: Notice[] = [];
let inbox: InboxEntry[] = [];
let listeners: Listener[] = [];
let inboxListeners: InboxListener[] = [];
let countListeners: CountListener[] = [];
let uid = 0;
const timers = new Map<number, TimerEntry>();

/** node（vitest）/SSR 无 rAF 时同步执行——toast 进场动画降级为直接出现。 */
const nextFrame: (fn: () => void) => void =
  typeof requestAnimationFrame === "function"
    ? (fn) => requestAnimationFrame(fn)
    : (fn) => fn();

// ---------------------------------------------------------------------------
// 持久化（§3.4）：localStorage 只存收件箱语义数据；损坏/不可用一律静默重置。
// ---------------------------------------------------------------------------

function isPlainEntry(v: unknown): v is InboxEntry {
  if (typeof v !== "object" || v === null) return false;
  const e = v as Partial<InboxEntry>;
  return (
    typeof e.id === "number" &&
    typeof e.text === "string" &&
    typeof e.level === "string" &&
    typeof e.createdAt === "number" &&
    (e.readAt === null || typeof e.readAt === "number")
  );
}

function saveNoticebox() {
  if (typeof localStorage === "undefined") return;
  try {
    localStorage.setItem(NOTICEBOX_KEY, JSON.stringify(inbox.slice(-NOTICEBOX_MAX)));
  } catch {
    /* quota/unavailable — 收件箱保底只在内存 */
  }
}

/** 从 localStorage 恢复收件箱。损坏数据静默重置为空（不抛、不 toast——
 *  通知系统自己不能靠通知报错）。导出供测试模拟「重启重载」。 */
export function hydrateNoticebox(): InboxEntry[] {
  if (typeof localStorage === "undefined") return inbox;
  try {
    const raw = localStorage.getItem(NOTICEBOX_KEY);
    if (!raw) return inbox;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) throw new Error("not an array");
    // 返回类型注解给 mode 三元提供 NoticeMode 上下文（否则字面量 widening
    // 成 string，TS2322）。
    const valid = parsed.filter(isPlainEntry).map(
      (e): InboxEntry => ({
        ...e,
        countInBadge: e.countInBadge !== false,
        mode: e.mode === "silent" ? "silent" : "toast",
      }),
    );
    inbox = valid.slice(-NOTICEBOX_MAX);
    uid = inbox.reduce((m, e) => Math.max(m, e.id), 0);
    emitInbox();
    return inbox;
  } catch {
    inbox = [];
    emitInbox();
    return inbox;
  }
}

if (typeof window !== "undefined") hydrateNoticebox();

// ---------------------------------------------------------------------------
// 事件分发
// ---------------------------------------------------------------------------

function emit() {
  for (const l of listeners) {
    try {
      l(items);
    } catch {
      /* 单个订阅者异常不影响其他 */
    }
  }
}

function unreadIn(entries: InboxEntry[]): number {
  return entries.filter((e) => e.readAt === null && e.countInBadge).length;
}

function emitInbox() {
  // 收件箱展示序：最新在前。
  const view = [...inbox].reverse();
  for (const l of inboxListeners) {
    try {
      l(view);
    } catch {
      /* ignore */
    }
  }
  const n = unreadIn(inbox);
  for (const l of countListeners) {
    try {
      l(n);
    } catch {
      /* ignore */
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

/** 移除一条 toast：先 entered=false 走出场过渡，再真正摘除。幂等。
 *  只作用于 toast 队列——收件箱条目与已读状态不受影响（超时消失≠已读，§3.2）。 */
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
 *
 * P1：无论 mode 如何都进收件箱；mode:"toast" 才弹卡片（§3.1）。
 */
export function pushNotice(level: NoticeLevel, text: string, opts: NoticeOptions = {}) {
  if (!text) return;
  const now = Date.now();
  const mode: NoticeMode = opts.mode === "silent" ? "silent" : "toast";
  const countInBadge = opts.countInBadge !== false;

  // ---- 第一步：收件箱（默认进；dedupeKey 命中则原位刷新，不堆叠。
  // 刷新视为新内容 → 重新未读，§3.3：会话投影已读后又来新回复应重新点亮）。
  // inbox:false（2026-11-08 用户反馈「切换模型没必要进通知面板」）：纯
  // 瞬时 toast——只弹几秒即逝、不进通知中心、不计 badge、不留档；适用
  // 于"操作确认"类反馈（模型切换等），错误/回传类仍默认留档。
  const ephemeral = opts.inbox === false;
  let entryId = 0;
  if (!ephemeral) {
    const prevEntry = opts.dedupeKey ? inbox.find((e) => e.dedupeKey === opts.dedupeKey) : undefined;
    if (prevEntry) {
    entryId = prevEntry.id;
    inbox = inbox.map((e) =>
      e.id === prevEntry.id
        ? {
            ...e,
            level,
            text,
            createdAt: now,
            readAt: null,
            kind: opts.kind,
            countInBadge,
            mode,
            source: opts.source,
            actionMeta: opts.actionMeta,
          }
        : e,
    );
  } else {
    entryId = ++uid;
    inbox = [
      ...inbox,
      {
        id: entryId,
        level,
        text,
        createdAt: now,
        readAt: null,
        mode,
        kind: opts.kind,
        countInBadge,
        source: opts.source,
        dedupeKey: opts.dedupeKey,
        actionMeta: opts.actionMeta,
      },
    ];
  }
  } // (!ephemeral)
  if (!ephemeral) {
    inbox = inbox.slice(-NOTICEBOX_MAX);
    saveNoticebox();
    emitInbox();
  } else {
    entryId = ++uid;
  }

  // ---- 第二步：toast 队列（仅 toast 模式；silent 到此为止）。
  if (mode === "silent") return;
  if (opts.dedupeKey) {
    // 原有刷新语义：重置倒计时；同 key 正在退场的残影踢掉防并存一瞬。
    const prev = items.find((n) => n.dedupeKey === opts.dedupeKey && n.entered);
    if (prev) {
      items = items.map((n) =>
        n.id === prev.id
          ? { ...n, ...opts, level, text, id: entryId, entered: true }
          : n,
      );
      emit();
      arm(entryId, opts.duration ?? durationFor(level));
      return;
    }
    items = items.filter((n) => n.dedupeKey !== opts.dedupeKey);
  }

  const n: Notice = { id: entryId, level, text, ...opts, entered: false };
  items = [...items.slice(-(NOTICE_MAX - 1)), n];
  emit();
  // 首帧透明 → 下一帧转 true，让 CSS 过渡真的跑起来。
  nextFrame(() => {
    items = items.map((x) => (x.id === n.id ? { ...x, entered: true } : x));
    emit();
  });
  arm(n.id, opts.duration ?? durationFor(level));
}

/** 已读：inbox 条目 readAt=now（幂等——已读再点不动），toast 若还在弹
 *  一并收卡。dedupeKey 用于「toast 与 inbox id 因刷新错位」时的兜底寻址。
 *  四种触发（§3.2）在 UI 层全部收敛到本函数。 */
export function markNoticeRead(id: number, dedupeKey?: string) {
  let hit = inbox.find((e) => e.id === id);
  if (!hit && dedupeKey) hit = inbox.find((e) => e.dedupeKey === dedupeKey);
  dismissNotice(id);
  if (!hit || hit.readAt !== null) return;
  const now = Date.now();
  inbox = inbox.map((e) => (e.id === hit.id ? { ...e, readAt: now } : e));
  saveNoticebox();
  emitInbox();
}

/** 收件箱「全部已读」（§3.2 触发四）。 */
export function markAllNoticesRead() {
  if (!inbox.some((e) => e.readAt === null)) return;
  const now = Date.now();
  inbox = inbox.map((e) => (e.readAt === null ? { ...e, readAt: now } : e));
  saveNoticebox();
  emitInbox();
}

/** 按 dedupeKey 标已读（session 投影双写入口，§3.3：用户点开会话时
 *  clearSessionUnread 顺带把收件箱里的对应投影条目清掉）。幂等。 */
export function markNoticeReadByKey(dedupeKey: string) {
  if (!inbox.some((e) => e.dedupeKey === dedupeKey && e.readAt === null)) return;
  const now = Date.now();
  inbox = inbox.map((e) => (e.dedupeKey === dedupeKey && e.readAt === null ? { ...e, readAt: now } : e));
  saveNoticebox();
  emitInbox();
}

/** 当前 badge 口径的未读数（countInBadge:false 的投影条目不计，§3.3）。 */
export function unreadNoticeCount(): number {
  return unreadIn(inbox);
}

export function subscribeNotices(l: Listener): () => void {
  listeners.push(l);
  l(items);
  return () => {
    listeners = listeners.filter((x) => x !== l);
  };
}

/** 收件箱订阅（最新在前，滚动窗口 ≤100 条）。 */
export function subscribeInbox(l: InboxListener): () => void {
  inboxListeners.push(l);
  l([...inbox].reverse());
  return () => {
    inboxListeners = inboxListeners.filter((x) => x !== l);
  };
}

/** 未读数订阅（badge 专用 selector）。 */
export function subscribeUnreadCount(l: CountListener): () => void {
  countListeners.push(l);
  l(unreadIn(inbox));
  return () => {
    countListeners = countListeners.filter((x) => x !== l);
  };
}

/** 清空全部（登出/测试）：toast 队列 + 收件箱 + 持久化。 */
export function clearNotices() {
  for (const { t } of timers.values()) if (t) clearTimeout(t);
  timers.clear();
  items = [];
  inbox = [];
  saveNoticebox();
  emit();
  emitInbox();
}
