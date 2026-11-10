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
  FAILURE_SOUND,
  inDndWindow,
  notifyPrefs,
  type NotifyKind,
} from "./notifyPrefs";

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

/** Minutes since local midnight — input for inDndWindow. */
function minutesOfDay(d = new Date()): number {
  return d.getHours() * 60 + d.getMinutes();
}

/**
 * The V2 gate: master switch ∧ per-kind switch ∧ not watching ∧ outside the
 * DnD window (DnD suppresses entirely for now — provisional
 * silent-to-Center banners need a Rust-side change, tracked in
 * docs/os-notifications-v2-design.md).
 */
export function shouldNotify(kind: NotifyKind, watching: boolean): boolean {
  const np = notifyPrefs();
  return (
    np.enabled &&
    np.kinds[kind] &&
    !watching &&
    !inDndWindow(np.dnd, minutesOfDay())
  );
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

/** Body text after the privacy gate: `titleOnly` strips the content. */
export function privacyBody(np: ReturnType<typeof notifyPrefs>, body: string): string {
  return np.privacy === "titleOnly" ? "" : body;
}

/** Sound for a kind: failures always get Basso; others follow prefs. */
export function soundFor(kind: NotifyKind): string | undefined {
  const np = notifyPrefs();
  if (!np.sound) return undefined;
  return kind === "failure" ? FAILURE_SOUND : np.soundName;
}
