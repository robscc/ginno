/**
 * The hook runtime: one invocation at a time, the runner deep-freezes the
 * event payload, builds the invocation-bound `$` and `next`, runs the hook,
 * and reports the answer to the broker. The broker owns the chain (ordering,
 * matchers, budgets, the single-call semantics of `next`); the runner keeps
 * only what has to live next to the mod's closures: the press-callback and
 * timer-callback tables, and a local cache so a second `next()` reuses the
 * first round-trip instead of sending another frame.
 * @module
 */

import { createModsApi } from './api.ts'
import type { TimerHost } from './api.ts'
import { BudgetClock } from './clock.ts'
import { serializeTree, treeProblem } from './elements.ts'
import type { UiNode } from './elements.ts'
import type { FrameConnection, Frame } from './frames.ts'
import { messageOf, deepFreeze } from './values.ts'
import type { LoadedMod, RegisteredHook } from './module.ts'
import type { AnyHook, HookFailure, HookNext, HookOrigin, ModTimer } from './types.ts'

/** The events whose answer is a drawable tree and gets serialized with press markers. */
const TREE_EVENTS: ReadonlySet<string> = new Set(['ui.render'])

/** How a hook's answer failed, as the `event-result` frame words it. */
interface HookAnswer {
  code: 'hook-failed' | 'no-result' | 'tree-problem' | 'timeout' | 'beneath-failed' | 'catch-failed' | 'catch-timeout'
  message: string
}

interface InvocationContext {
  readonly invocation: string
  readonly event: string
  readonly hook: RegisteredHook
  /** The frozen payload copy the hook (and its `.catch`) receives as `e`. */
  readonly frozen: unknown
  readonly clock: BudgetClock
  readonly signal: AbortSignal
  readonly deadlineMs: number
  /** Flags the hook and catch paths share; closures read their current values. */
  readonly state: {
    abandoned: boolean
    called: boolean
    beneath: Promise<unknown> | undefined
    beneathFailed: boolean
  }
  failed: boolean
  settled: boolean
  deadlineTimer: NodeJS.Timeout | undefined
}

export interface HookRuntimeOptions {
  readonly connection: FrameConnection
  readonly loaded: LoadedMod
  readonly hooks: readonly RegisteredHook[]
  /** Wall-clock backstop when an event frame carries no deadlineMs. */
  readonly defaultDeadlineMs?: number
}

/**
 * Broker-owned timers: `after`/`every` frame a `clock.*` op whose result
 * names the timer; the broker hands each firing back as a `timer-callback`
 * frame. Cancellation frames a `clock.cancel` op fire-and-forget.
 */
class BrokerTimers implements TimerHost {
  private readonly callbacks = new Map<string, () => unknown>()

  constructor(
    private readonly modName: string,
    private readonly invoke: (op: string, input: unknown) => Promise<unknown>,
    private readonly report: (line: string) => void,
    private readonly onCallbackFailed: (timerId: string, message: string) => void,
  ) {}

  after(ms: number, fn: () => unknown): ModTimer {
    return this.register('after', ms, fn)
  }

  every(ms: number, fn: () => unknown): ModTimer {
    return this.register('every', ms, fn)
  }

  private register(method: 'after' | 'every', ms: number, fn: () => unknown): ModTimer {
    let timerId: string | undefined
    let cancelled = false
    const cancelNow = (): void => {
      if (timerId === undefined) return
      this.callbacks.delete(timerId)
      this.invoke('clock.cancel', { timerId }).catch(() => {})
    }
    this.invoke(`clock.${method}`, { ms }).then(result => {
      const id = record_get(result, 'timerId')
      if (typeof id !== 'string' && typeof id !== 'number') {
        this.report(`${this.modName}: $.clock.${method} failed: the broker named no timer id`)
        return
      }
      const key = String(id)
      if (cancelled) {
        this.invoke('clock.cancel', { timerId: key }).catch(() => {})
        return
      }
      timerId = key
      this.callbacks.set(key, fn)
    }).catch((error: unknown) => {
      this.report(`${this.modName}: $.clock.${method} failed: ${messageOf(error)}`)
    })
    return {
      cancel(): void {
        cancelled = true
        cancelNow()
      },
    }
  }

  /** Run the callback a `timer-callback` frame names; one-shot timers drop it. */
  fire(timerId: string): void {
    const callback = this.callbacks.get(timerId)
    if (callback === undefined) return
    Promise.resolve().then(callback).catch((error: unknown) => {
      const message = messageOf(error)
      this.report(`${this.modName}: timer callback failed: ${message}`)
      this.onCallbackFailed(timerId, message)
    })
  }
}

function record_get(value: unknown, field: string): unknown {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>)[field] : undefined
}

/**
 * Runs one mod's hooks against frames from the broker.
 */
export class HookRuntime {
  private readonly invocations = new Map<string, InvocationContext>()
  private readonly pressCallbacks = new Map<number, () => unknown>()
  private nextPressIndex = 0
  private readonly timers: BrokerTimers
  private readonly defaultDeadlineMs: number

  constructor(private readonly options: HookRuntimeOptions) {
    this.defaultDeadlineMs = options.defaultDeadlineMs ?? 10_000
    // Timers live outside any invocation: a `clock.after` a hook scheduled fires after the hook settled.
    this.timers = new BrokerTimers(options.loaded.name, this.invokeUnbound.bind(this), this.report.bind(this), (timerId, message) => {
      options.connection.notify('timer-callback-failed', { timerId, message })
    })
  }

  /** A `$` with no invocation bound: what press and timer callbacks capture. */
  private invokeUnbound(op: string, input: unknown): Promise<unknown> {
    return this.invokeOp(op, input, undefined)
  }

  private invokeOp(op: string, input: unknown, invocation: string | undefined): Promise<unknown> {
    const dot = op.indexOf('.')
    const ns = dot === -1 ? op : op.slice(0, dot)
    const method = dot === -1 ? '' : op.slice(dot + 1)
    return this.options.connection.request({
      kind: 'call',
      ns,
      method,
      args: input,
      ...(invocation === undefined ? {} : { invocation }),
    }).then(result => {
      if (result.ok) return result.value
      const failure = new Error(result.message ?? `${op} failed`)
      ;(failure as Error & { code?: string }).code = result.code
      throw failure
    })
  }

  private report(line: string): void {
    console.error(line)
    this.options.connection.notify('report', { message: line })
  }

  // ---- Events ----

  /** Run the hook an `event` frame selects. */
  handleEvent(frame: Frame): void {
    const invocation = typeof frame.invocation === 'string' ? frame.invocation : undefined
    const event = typeof frame.event === 'string' ? frame.event : undefined
    if (invocation === undefined || event === undefined) {
      console.error('mod-runner: an event frame without invocation/event arrived; dropped')
      return
    }
    if (this.invocations.has(invocation)) {
      console.error(`mod-runner: invocation ${invocation} is already running; dropped the duplicate frame`)
      return
    }
    // The payload arrived over JSON, so it is already a fresh copy; freezing it keeps the mods API's promise.
    const frozen = deepFreeze(frame.payload)
    const deadlineMs = typeof frame.deadlineMs === 'number' && frame.deadlineMs > 0 ? frame.deadlineMs : this.defaultDeadlineMs
    const hook = this.hookFor(event)
    const clock = new BudgetClock(deadlineMs)
    const controller = new AbortController()
    const context: InvocationContext = {
      invocation,
      event,
      hook,
      frozen,
      clock,
      signal: controller.signal,
      deadlineMs,
      state: { abandoned: false, called: false, beneath: undefined, beneathFailed: false },
      failed: false,
      settled: false,
      deadlineTimer: undefined,
    }
    this.invocations.set(invocation, context)
    context.deadlineTimer = setTimeout(() => {
      // The broker enforces the budget; this wall-clock backstop only covers a hook that never answers.
      this.settleFailure(context, { code: 'timeout', message: `${event} hook ran past its ${deadlineMs} ms limit` })
    }, deadlineMs).unref()

    const next = this.makeNext(context, {})
    const api = createModsApi({
      mod: { name: this.options.loaded.name, root: this.options.loaded.root },
      clock,
      invoke: (op, input) => this.invokeOp(op, input, invocation),
      timers: this.timers,
      report: line => this.report(line),
    })
    clock.start()
    Promise.resolve().then(() => hook.hook(api, frozen, next as HookNext<unknown, unknown>)).then(
      result => this.settleSuccess(context, result),
      error => this.settleFailure(context, { code: 'hook-failed', message: failureLine(error) }),
    )
  }

  /** Run the failed hook's `.catch` handler on a `catch-call` frame from the broker. */
  handleCatchCall(frame: Frame): void {
    const invocation = typeof frame.invocation === 'string' ? frame.invocation : undefined
    const context = invocation === undefined ? undefined : this.invocations.get(invocation)
    if (context === undefined || !context.failed) {
      console.error(`mod-runner: a catch-call for unknown or unsettled invocation ${String(invocation)} arrived; dropped`)
      return
    }
    const handler = context.hook.catchHandler
    if (handler === undefined) {
      this.finish(context, { ok: false, code: 'catch-failed', message: 'no catch handler is attached to this hook' })
      return
    }
    if (context.state.beneathFailed) {
      // The failure is the event's own (the chain beneath threw), not the hook's: a catch does not answer it.
      this.finish(context, { ok: false, code: 'beneath-failed', message: 'the chain beneath failed; the catch handler does not run' })
      return
    }
    const failure = recordFailure(frame.failure)
    const clock = new BudgetClock(1000) // The catch budget the broker enforces; this backs it up.
    const next = this.makeNext(context, { isCatch: true, failure })
    const api = createModsApi({
      mod: { name: this.options.loaded.name, root: this.options.loaded.root },
      clock,
      invoke: (op, input) => this.invokeOp(op, input, context.invocation),
      timers: this.timers,
      report: line => this.report(line),
    })
    let abandoned = false
    const timer = setTimeout(() => {
      abandoned = true
      this.finish(context, { ok: false, code: 'catch-timeout', message: 'the .catch handler ran past its 1000 ms limit' })
    }, 1000).unref()
    clock.start()
    Promise.resolve().then(() => handler(api, context.frozen, next as HookNext<unknown, unknown>)).then(
      result => {
        if (abandoned) return
        clearTimeout(timer)
        clock.stop()
        if (typeof result === 'object') this.finish(context, { ok: true, value: this.finalValue(context.event, result) })
        else this.finish(context, { ok: false, code: 'catch-failed', message: 'the .catch handler returned no result' })
      },
      error => {
        if (abandoned) return
        clearTimeout(timer)
        clock.stop()
        this.finish(context, { ok: false, code: 'catch-failed', message: failureLine(error) })
      },
    )
  }

  /** Run a `press` frame's button callback from the latest ui.render registrations. */
  handlePress(frame: Frame): void {
    const id = typeof frame.id === 'number' ? frame.id : -1
    // The marker the runner serializes into the tree comes back either as `callbackIndex` or, in broker
    // dialects that only know one field, as the numeric `actionId`.
    const raw = frame.callbackIndex ?? frame.actionId
    const index = typeof raw === 'number' ? raw : typeof raw === 'string' ? Number(raw) : Number.NaN
    const callback = Number.isInteger(index) ? this.pressCallbacks.get(index) : undefined
    if (callback === undefined) {
      this.options.connection.respond(id, false, { code: 'not-found', message: `no press callback is registered for ${String(raw)}` })
      return
    }
    Promise.resolve().then(callback).then(
      value => this.options.connection.respond(id, true, { value: value === undefined ? null : value }),
      error => this.options.connection.respond(id, false, { code: 'press-failed', message: failureLine(error) }),
    )
  }

  /** Run the timer callback a `timer-callback` frame names. */
  handleTimerCallback(frame: Frame): void {
    const id = typeof frame.id === 'number' ? frame.id : -1
    const timerId = typeof frame.timerId === 'string' ? frame.timerId : undefined
    if (timerId === undefined) {
      this.options.connection.respond(id, false, { code: 'not-found', message: 'a timer-callback frame without timerId arrived' })
      return
    }
    this.timers.fire(timerId)
    this.options.connection.respond(id, true, { value: null })
  }

  // ---- Internals ----

  private hookFor(event: string): RegisteredHook {
    // The broker dispatches by its own registry; one runner hosts one mod, and an event
    // only arrives when a hook of this mod selects it.
    const hook = this.options.hooks.find(candidate => candidate.event === event || (candidate.event.endsWith('.*') && event.startsWith(candidate.event.slice(0, -1))) || candidate.event === '*')
    if (hook === undefined) {
      throw new Error(`no hook of this mod selects ${event}`)
    }
    return hook
  }

  /**
   * The `next` one hook (or its `.catch`) receives. First call frames the
   * broker; a second call hands back the first round-trip, so the broker
   * never sees two `next` frames for one hook.
   */
  private makeNext(context: InvocationContext, opts: { isCatch?: boolean; failure?: HookFailure }): HookNext<unknown, unknown> {
    const origin: HookOrigin = Object.freeze({ plugin: 'engine', tier: 'core' })
    const next = ((e: unknown): Promise<unknown> => {
      context.state.called = true
      // One run beneath per hook: a second call hands back the first run.
      if (context.state.beneath !== undefined) return context.state.beneath
      if (context.state.abandoned) return Promise.resolve(undefined)
      context.clock.pause()
      const beneath = this.options.connection.request({
        kind: 'next',
        invocation: context.invocation,
        e: e === undefined ? null : e,
      }).then(result => {
        if (result.ok) return result.value
        context.state.beneathFailed = true
        const failure = new Error(result.message ?? 'the chain beneath failed')
        ;(failure as Error & { code?: string }).code = result.code
        throw failure
      }).finally(() => {
        context.clock.resume()
      })
      context.state.beneath = beneath
      return beneath
    }) as HookNext<unknown, unknown>
    Object.assign(next, {
      signal: context.signal,
      origin,
      budget: { ms: context.deadlineMs, get remainingMs(): number { return context.clock.remainingMs } },
      to(): Promise<never> {
        return Promise.reject(new Error(opts.isCatch === true
          ? 'next.to is not available in a .catch handler'
          : 'next.to is only available to mods in managed prependPlugins or appendPlugins'))
      },
      ...(opts.isCatch === true && opts.failure !== undefined ? { error: opts.failure, called: context.state.called } : {}),
    })
    return next
  }

  private settleSuccess(context: InvocationContext, result: unknown): void {
    this.clearDeadline(context)
    // `null` is an answer (a surface drawn empty); only a hook that settles with no object at all is skipped.
    if (typeof result !== 'object') {
      this.settleFailure(context, { code: 'no-result', message: `${context.event} hook returned no result` })
      return
    }
    // The hook answered; whatever it started beneath still runs to its end before the event settles.
    const finish = (): void => this.finish(context, { ok: true, value: this.finalValue(context.event, result) })
    if (context.state.beneath !== undefined) {
      context.state.beneath.catch((error: unknown) => {
        this.report(`${this.options.loaded.name}: ${context.event}: the chain beneath failed after the hook answered: ${messageOf(error)}`)
      }).then(finish, finish)
      return
    }
    finish()
  }

  private settleFailure(context: InvocationContext, answer: HookAnswer): void {
    this.clearDeadline(context)
    context.state.abandoned = true
    if (context.settled) return
    const line = `${this.options.loaded.name}: ${context.event} hook skipped: ${answer.message}`
    if (!context.hook.reported.has(answer.code)) {
      context.hook.reported.add(answer.code)
      this.report(line)
    }
    context.failed = true
    this.options.connection.notify('event-result', {
      invocation: context.invocation,
      ok: false,
      code: answer.code,
      message: answer.message,
      hasCatch: context.hook.catchHandler !== undefined,
    })
    // No `.catch` to wait for: the invocation is over on this side.
    if (context.hook.catchHandler === undefined) this.invocations.delete(context.invocation)
  }

  /** Send the invocation's final answer and drop its state. */
  private finish(context: InvocationContext, answer: { ok: boolean; value?: unknown; code?: string; message?: string }): void {
    this.clearDeadline(context)
    context.settled = true
    this.invocations.delete(context.invocation)
    this.options.connection.notify('event-result', {
      invocation: context.invocation,
      ok: answer.ok,
      ...(answer.ok ? { value: answer.value } : { code: answer.code, message: answer.message }),
    })
  }

  private clearDeadline(context: InvocationContext): void {
    if (context.deadlineTimer !== undefined) clearTimeout(context.deadlineTimer)
    context.deadlineTimer = undefined
  }

  /**
   * The value an answer frame carries: for a tree event, the hook's elements
   * serialized with each `onPress` replaced by a numeric marker the broker
   * turns into its own action id; anything else passes as JSON as-is.
   */
  private finalValue(event: string, result: unknown): unknown {
    if (!TREE_EVENTS.has(event)) return result === undefined ? null : result
    if (result === null || result === undefined) return null
    if (looksSerialized(result)) return result // A tree `next` handed up from a later mod: already serialized.
    const problem = treeProblem(result as UiNode)
    if (problem !== undefined) {
      // An invalid tree is the hook's failure; the caller words it as an answer frame failure.
      throw new Error(problem)
    }
    const serialized = serializeTree(result as UiNode, callback => {
      const index = this.nextPressIndex
      this.nextPressIndex += 1
      this.pressCallbacks.set(index, callback)
      return index
    })
    // serializeTree names the held callback `actionId` (the host assigns it); this broker learns the
    // callback from the numeric `onPress` marker instead.
    return markPressCallbacks(serialized)
  }
}

/** `serializeTree` output with each held `actionId` renamed to a numeric `onPress` marker. */
function markPressCallbacks(nodes: unknown): unknown {
  if (Array.isArray(nodes)) return nodes.map(markPressCallbacks)
  if (typeof nodes === 'object' && nodes !== null) {
    const record = nodes as Record<string, unknown>
    const rest: Record<string, unknown> = {}
    for (const [field, value] of Object.entries(record)) {
      if (field === 'actionId') continue
      rest[field] = field === 'children' ? markPressCallbacks(value) : value
    }
    if ('actionId' in record) rest['onPress'] = typeof record['actionId'] === 'number' ? record['actionId'] : Number(record['actionId'])
    return rest
  }
  return nodes
}

/** Whether a value already reads as a serialized tree (what `next` hands up from a later mod). */
function looksSerialized(value: unknown): boolean {
  const nodes = Array.isArray(value) ? value : [value]
  return nodes.every(node => typeof node === 'string' || (typeof node === 'object' && node !== null
    && typeof (node as Record<string, unknown>)['type'] === 'string'
    && typeof (node as Record<string, unknown>)['props'] === 'object'))
}

/** A failed hook as the frame words it: an Error's `name: message`, anything else as text. */
function failureLine(error: unknown): string {
  return error instanceof Error ? `${error.name}: ${error.message}` : messageOf(error)
}

function recordFailure(value: unknown): HookFailure {
  const kind = record_get(value, 'kind') === 'timeout' ? 'timeout' : 'throw'
  const message = record_get(value, 'message')
  return { kind, message: typeof message === 'string' ? message : '' }
}

// AnyHook surfaces in the catch-handler signature; keep the type import honest.
export type { AnyHook, Frame }
