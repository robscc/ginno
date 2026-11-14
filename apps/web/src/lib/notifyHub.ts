/**
 * Notification hub — V2 trigger-surface extensions for OS notifications.
 *
 * The V1 gate (turn done → notifyNative when not watching) lives inline in
 * chat/engine/handle.ts. V2 adds, on top of that single site:
 *   - a per-session 30s merge window for `reply` completions (a 5-step
 *     workflow run must produce ONE banner, not five);
 *   - immediate (unmerged) dispatch for urgent kinds: ask_user pending and
 *     turn failure — waiting-on-human and broken runs should never be batched;
 *   - gate helpers for per-kind switches and the do-not-disturb window.
 *
 * Merge logic is a standalone class with injected clock/timer so the unit
 * tests (notifyHub.test.ts) run without DOM/JS timers.
 */

import {
  inDndWindow,
  minutesOfDay,
  notifyPrefs,
  osGateOpen,
  soundFor,
  type NotifyKind,
} from "./notifyPrefs";

// soundFor / minutesOfDay / inDndWindow 现在住在 notifyPrefs（闸门与偏好同源）。
// 这里再导出一次，保住既有 import 路径不动。
export { inDndWindow, minutesOfDay, soundFor };

/** Body text after the privacy gate: `titleOnly` strips the content.
 *  @deprecated 隐私裁剪已下沉进 desktop.ts 的 notifyNative（唯一出口），调用点
 *  不需要自己裁。保留此别名只为兼容既有 import，新代码不要再调。 */
export function privacyBody(np: ReturnType<typeof notifyPrefs>, body: string): string {
  return np.privacy === "titleOnly" ? "" : body;
}

/** 用户是否正盯着**这个应用**。
 *
 *  单一判据（P1-4）：修复前 7 个发通知的站点各写各的——6 处只看
 *  `document.visibilityState`，只有 NoticeHost 的失焦兜底额外看了
 *  `document.hasFocus()`。口径不一意味着同一个「用户在看吗」的问题在不同站点
 *  有不同答案。P1 先统一成只看 visibilityState（保守方向），P2 经实测后把
 *  hasFocus 补回来。
 *
 *  **为什么必须两个条件合取**（实测见 scripts/probe-wkwebview-attention.README.md，
 *  macOS 15.6 / WKWebView，2026-11-15）：
 *   - `visibilityState` **不追踪窗口焦点**：窗口可见但焦点在别的 App（或同 App
 *     的另一个窗口）时它仍报 `visible`。只用它 → 用户 ⌘Tab 切走而 Ginno 窗口
 *     还在屏上时通知被抑制，即「该弹不弹」。这是最常见的场景。
 *   - `hasFocus()` **在焦点位于页面内 iframe 时仍为 true**（activeElement 已是
 *     IFRAME），所以纳入它不会把「用户正在操作 MCP app 卡 / 代码面板 viewer」
 *     误判成没在看——这是纳入的主要风险，实测排除。
 *   - 单独用 `hasFocus()` 也不行：App 隐藏时它曾滞后报 true，而 visibilityState
 *     已正确翻成 hidden。
 *
 *  `hasFocus` 不存在时（node/SSR、老环境）退回只看 visibilityState，即 P1 的
 *  保守行为，不会因为拿不到信息就判成「没在看」而狂弹。 */
export function isAppAttentive(): boolean {
  if (typeof document === "undefined") return false;
  if (document.visibilityState !== "visible") return false;
  return typeof document.hasFocus === "function" ? document.hasFocus() : true;
}

/** 用户是否正盯着**这个会话**：应用可见 ∧ 在工作区路由 ∧ 它就是当前会话。
 *
 *  socket 回调闭包会过期，所以只读 ref / 即时值。 */
export function isSessionAttentive(activeSid: string | null, sid: string): boolean {
  if (!isAppAttentive()) return false;
  if (typeof window === "undefined") return false;
  return window.location.pathname === "/" && activeSid === sid;
}

/**
 * The gate: master switch ∧ per-kind switch ∧ not watching ∧ outside the DnD
 * window. 偏好那三层已抽到 notifyPrefs.osGateOpen，与 desktop.ts 的
 * notifyNative 出口共用同一实现——调用点先判一次省掉无谓的正文构造，出口再判
 * 一次兜住漏网的站点（幂等重叠，有意为之）。
 *
 * DnD suppresses entirely for now — provisional silent-to-Center banners need
 * a Rust-side change (see docs/notifications-audit-2026-11-14.md §3 P2).
 */
export function shouldNotify(kind: NotifyKind, watching: boolean): boolean {
  return !watching && osGateOpen(kind);
}


export interface MergeItem {
  title: string;
  body: string;
}

export interface MergeWindowOpts<T extends MergeItem = MergeItem> {
  /** Merge window in ms. Default 30_000. */
  windowMs?: number;
  /** Injected clock (ms) for tests. */
  now?: () => number;
  /** Injected scheduler for tests (default setTimeout, cleared on flush). */
  schedule?: (fn: () => void, ms: number) => () => void;
  /** Called with the accumulated items when the window closes. */
  onFlush: (key: string, items: T[]) => void;
}

/**
 * Per-key merge window: first push starts the timer, subsequent pushes inside
 * the window accumulate, the flush hands all items to `onFlush`. Keys are
 * session ids — merges never mix sessions. Generic over the item type so
 * callers can attach closures (e.g. lazy i18n formatters).
 */
export class ReplyMergeWindow<T extends MergeItem = MergeItem> {
  private readonly windowMs: number;
  private readonly now: () => number;
  private readonly schedule: (fn: () => void, ms: number) => () => void;
  private readonly onFlush: (key: string, items: T[]) => void;
  private readonly buckets = new Map<string, { items: T[]; cancel: () => void }>();

  constructor(opts: MergeWindowOpts<T>) {
    this.windowMs = opts.windowMs ?? 30_000;
    this.now = opts.now ?? Date.now;
    this.schedule =
      opts.schedule ?? ((fn, ms) => { const h = setTimeout(fn, ms); return () => clearTimeout(h); });
    this.onFlush = opts.onFlush;
  }

  push(key: string, item: T): void {
    const existing = this.buckets.get(key);
    if (existing) {
      existing.items.push(item);
      return;
    }
    const cancel = this.schedule(() => {
      const b = this.buckets.get(key);
      this.buckets.delete(key);
      if (b) this.onFlush(key, b.items);
    }, this.windowMs);
    this.buckets.set(key, { items: [item], cancel });
  }

  /** Items waiting in the window for `key` (0 = nothing pending). */
  pending(key: string): number {
    return this.buckets.get(key)?.items.length ?? 0;
  }

  /** Drop a bucket without flushing (e.g. user switched to the session). */
  cancel(key: string): void {
    const b = this.buckets.get(key);
    if (b) {
      b.cancel();
      this.buckets.delete(key);
    }
  }
}

/** Per-kind OS 通知冷却门（2026-11-10：跨会话 reply 刷屏治理）。
 *
 * ReplyMergeWindow 只按「同一 session」合并；多 worker/多 watch 并行时
 * 每个会话的 turn 完成各弹一条，通知中心瞬间刷屏。KindRateGate 在
 * kind 维度加冷却窗：窗口内的同类通知被吞并计数，窗口结束后第一条
 * fire 时带上 `suppressed` 数（调用方拼「+N 条已合并」后缀）。
 * askUser/failure 不走此门（等人/出事绝不节流）——调用方自行选择。 */
export class KindRateGate {
  private last = new Map<string, number>();
  private suppressed = new Map<string, number>();

  constructor(
    private readonly windowMs: number,
    private readonly now: () => number = () => Date.now(),
  ) {}

  /** 尝试放行一次 kind 通知；返回 {ok, suppressed}——suppressed 是本窗
   *  口内被吞掉的数量（仅 ok=true 时非零，随后清零）。 */
  hit(kind: string): { ok: boolean; suppressed: number } {
    const t = this.now();
    // 首次命中（无记录）必须放行：用 -Infinity 而非 0，t=0 起点不误吞。
    const last = this.last.get(kind) ?? Number.NEGATIVE_INFINITY;
    if (t - last < this.windowMs) {
      const n = (this.suppressed.get(kind) ?? 0) + 1;
      this.suppressed.set(kind, n);
      return { ok: false, suppressed: n };
    }
    this.last.set(kind, t);
    const sup = this.suppressed.get(kind) ?? 0;
    this.suppressed.set(kind, 0);
    return { ok: true, suppressed: sup };
  }
}

/** reply 类全局冷却门：60s 内跨会话只弹一条，后续合并计数。 */
export const replyRateGate = new KindRateGate(60_000);
