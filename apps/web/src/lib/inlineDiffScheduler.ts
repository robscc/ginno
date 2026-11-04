/**
 * 500ms coalescing scheduler for `code.changed` → inline-diff opens
 * (docs/code-panel-diff-design.md §4.1: 同文件连续 code.changed 防抖合并).
 *
 * Per-path timer: repeated events for the same file RESET the 500ms window and
 * keep only the newest payload, so a burst of agent edits fires exactly one
 * diff fetch — carrying the newest version. Events for a different path get
 * their own timer and never delay each other. `cancel(path)` drops a pending
 * entry (tab closed / user edited) and `dispose()` clears everything.
 */

export interface ScheduledChange<T> {
  /** Identity of the changed file (absolute path on the wire). */
  path: string;
  /** Newest observed version token — older ones are discarded by replacement. */
  version: string;
  /** Opaque extra data (op / baseVersion / turnId) carried to the consumer. */
  data: T;
}

export class InlineDiffScheduler<T> {
  private timers = new Map<string, ReturnType<typeof setTimeout>>();
  private pending = new Map<string, ScheduledChange<T>>();

  constructor(
    private readonly delayMs: number,
    private readonly onFire: (change: ScheduledChange<T>) => void,
  ) {}

  /** (Re)arm the window for `path`. The newest payload always wins. */
  schedule(path: string, version: string, data: T): void {
    const existing = this.timers.get(path);
    if (existing !== undefined) clearTimeout(existing);
    this.pending.set(path, { path, version, data });
    this.timers.set(
      path,
      setTimeout(() => {
        this.timers.delete(path);
        const change = this.pending.get(path);
        this.pending.delete(path);
        if (change) this.onFire(change);
      }, this.delayMs),
    );
  }

  /** Drop a pending change (e.g. the tab was closed mid-window). */
  cancel(path: string): void {
    const timer = this.timers.get(path);
    if (timer !== undefined) {
      clearTimeout(timer);
      this.timers.delete(path);
    }
    this.pending.delete(path);
  }

  dispose(): void {
    for (const timer of this.timers.values()) clearTimeout(timer);
    this.timers.clear();
    this.pending.clear();
  }
}
