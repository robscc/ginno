/**
 * Accounts a hook's own running time: the clock runs while the hook is busy
 * and pauses while it awaits `next` or a mods API call. The runner uses it
 * only to report `next.budget.remainingMs` and to pause around its own
 * round-trips; the *deadline* is enforced by the broker (one budget
 * implementation, no drift). Only the wall-clock backstop in the hook
 * runtime bounds a hook that never answers.
 * Adapted from deepseek-harness (MIT), Copyright (c) 2026 DeepSeek. @license MIT — the
 * deadline arming (`expired`) is carried over unchanged but nothing in the
 * runner awaits it.
 * @module
 */

/** The `next.error.kind === 'timeout'` failure, as the broker words it. */
export class HookTimeoutError extends Error {
  constructor(budgetMs: number) {
    super(`ran past its ${budgetMs} ms limit`)
    this.name = 'HookTimeoutError'
  }
}

export class BudgetClock {
  readonly ms: number
  private spent = 0
  private busySince: number | undefined
  private pauses = 0
  private stopped = false
  private timer: ReturnType<typeof setTimeout> | undefined
  private readonly deadline = Promise.withResolvers<never>()
  // No parameter properties: the dev mode runs this source under Node's type stripping.
  private readonly now: () => number

  /**
   * Hooked by the hook runtime to suspend/resume its wall-clock backstop in
   * step with the budget: while a `next`/mods-API call (a pending `$.ui.ask`
   * included) has the clock paused, the backstop must not fire — user think
   * time is not the mod's running time (design §3.2).
   */
  onPause: (() => void) | undefined
  onResume: (() => void) | undefined

  /**
   * @param ms - the running-time limit in milliseconds.
   * @param now - monotonic clock in milliseconds.
   */
  constructor(ms: number, now: () => number = () => performance.now()) {
    this.ms = ms
    this.now = now
    // The deadline only matters while a race awaits it; an unobserved
    // rejection after the hook settled must not surface.
    this.deadline.promise.catch(() => {})
  }

  /** Rejects with {@link HookTimeoutError} when the busy time reaches the limit. */
  get expired(): Promise<never> {
    return this.deadline.promise
  }

  /** Milliseconds left before the deadline fires. */
  get remainingMs(): number {
    const busy = this.busySince === undefined ? 0 : this.now() - this.busySince
    return Math.max(0, this.ms - this.spent - busy)
  }

  /** Start counting; the hook is busy from now on. */
  start(): void {
    if (this.stopped) return
    this.busySince = this.now()
    this.arm()
  }

  /** Stop counting while the hook awaits `next` or a mods API call. Nested pauses are counted. */
  pause(): void {
    if (this.stopped) return
    this.pauses += 1
    if (this.busySince !== undefined) {
      this.spent += this.now() - this.busySince
      this.busySince = undefined
    }
    this.disarm()
    this.onPause?.()
  }

  /** Resume counting after the awaited call settled. */
  resume(): void {
    if (this.stopped || this.pauses === 0) return
    this.pauses -= 1
    if (this.pauses === 0) {
      this.busySince = this.now()
      this.arm()
    }
    this.onResume?.()
  }

  /** The hook settled: no deadline can fire from now on. */
  stop(): void {
    if (this.busySince !== undefined) this.spent += this.now() - this.busySince
    this.stopped = true
    this.busySince = undefined
    this.disarm()
  }

  private arm(): void {
    this.disarm()
    const remaining = this.remainingMs
    this.timer = setTimeout(() => {
      this.stopped = true
      this.busySince = undefined
      this.spent = this.ms
      this.deadline.reject(new HookTimeoutError(this.ms))
    }, remaining)
    this.timer.unref()
  }

  private disarm(): void {
    if (this.timer !== undefined) clearTimeout(this.timer)
    this.timer = undefined
  }
}
