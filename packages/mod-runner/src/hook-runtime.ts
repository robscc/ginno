/**
 * The hook runtime: one invocation at a time, the runner deep-freezes the
 * event payload, builds the invocation-bound `$` and `next`, runs the hook,
 * and reports the answer to the broker as the `result` frame of the event
 * frame's id. The broker owns the chain (ordering, matchers, budgets, the
 * single-call semantics of `next`); the runner keeps only what has to live
 * next to the mod's closures: the press-callback and timer-callback tables,
 * and a local cache so a second `next()` reuses the first round-trip instead
 * of sending another frame.
 * @module
 */

import { createModsApi } from './api.ts'
import type { TimerHost } from './api.ts'
import { BudgetClock } from './clock.ts'
import { isUiElement, serializeTree, treeProblem } from './elements.ts'
import type { UiNode } from './elements.ts'
import type { FrameConnection, Frame } from './frames.ts'
import { messageOf, deepFreeze } from './values.ts'
import type { LoadedMod, RegisteredHook } from './module.ts'
import type { AnyHook, HookFailure, HookNext, HookOrigin, ModTimer } from './types.ts'

/** The events whose answer is a drawable tree and gets serialized with action ids. */
const TREE_EVENTS: ReadonlySet<string> = new Set(['ui.render'])

/** How long a failed hook's context stays around for the broker's catch-call. */
const CATCH_GRACE_MS = 30_000

/** How a hook's answer failed, as the event's `result` frame words it. */
interface HookAnswer {
  code: 'no-result' | 'no-hook' | 'hook-throw' | 'no-catch' | 'catch-throw' | 'catch-timeout'
  message: string
}

interface InvocationContext {
  /** The id of the `event` frame; the answer `result` frame echoes it. */
  readonly frameId: number
  readonly invocation: string
  readonly event: string
  /** The session the event belongs to, echoed on the invocation's `$` calls. */
  readonly session: string | undefined
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
  catchGraceTimer: NodeJS.Timeout | undefined
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
 * names the timer (`{timer}`, or `{timer:""}` for a dead handle once the
 * set closed); the broker hands each firing back as a `clock.fire` call and
 * reads its result as the callback's completion. Cancellation frames a
 * `clock.cancel` op fire-and-forget.
 */
class BrokerTimers implements TimerHost {
  private readonly callbacks = new Map<string, () => unknown>()
  // No parameter properties: the dev mode runs this source under Node's type stripping.
  private readonly modName: string
  private readonly invoke: (op: string, input: unknown) => Promise<unknown>
  private readonly report: (line: string) => void

  constructor(modName: string, invoke: (op: string, input: unknown) => Promise<unknown>, report: (line: string) => void) {
    this.modName = modName
    this.invoke = invoke
    this.report = report
  }

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
      this.invoke('clock.cancel', { timer: timerId }).catch(() => {})
    }
    this.invoke(`clock.${method}`, { ms }).then(result => {
      const id = record_get(result, 'timer')
      if (typeof id !== 'string') {
        this.report(`${this.modName}: $.clock.${method} failed: the broker named no timer`)
        return
      }
      if (id === '') return // A dead handle: the timer set closed; cancel is a no-op, as DSH's is.
      if (cancelled) {
        this.invoke('clock.cancel', { timer: id }).catch(() => {})
        return
      }
      timerId = id
      this.callbacks.set(id, fn)
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

  /**
   * Run the callback a `clock.fire` call names, resolving when it settles —
   * the broker reads this result as the callback's completion (its close
   * semantics await the callbacks already running).
   */
  async fire(timerId: string): Promise<void> {
    const callback = this.callbacks.get(timerId)
    if (callback === undefined) return
    try {
      await callback()
    } catch (error: unknown) {
      this.report(`${this.modName}: timer callback failed: ${messageOf(error)}`)
      throw error
    }
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
  /** The latest ui.render's press callbacks, keyed `session\0actionId`. */
  private readonly pressCallbacks = new Map<string, () => unknown>()
  private readonly timers: BrokerTimers
  private readonly defaultDeadlineMs: number
  // No parameter properties: the dev mode runs this source under Node's type stripping.
  private readonly options: HookRuntimeOptions

  constructor(options: HookRuntimeOptions) {
    this.options = options
    this.defaultDeadlineMs = options.defaultDeadlineMs ?? 10_000
    // Timers live outside any invocation: a `clock.after` a hook scheduled fires after the hook settled.
    this.timers = new BrokerTimers(options.loaded.name, this.invokeUnbound.bind(this), this.report.bind(this))
  }

  /** A `$` with no invocation bound: what press and timer callbacks capture. */
  private invokeUnbound(op: string, input: unknown): Promise<unknown> {
    return this.invokeOp(op, input, undefined)
  }

  /**
   * The session a press or timer callback runs for. Its `$` calls carry no
   * invocation (the hook that captured the `$` has settled), so the press /
   * clock.fire frame's session rides here instead — without it the broker's
   * runtime-backed ops refuse the call ("session context missing").
   */
  private ambientSession: string | undefined

  private invokeOp(op: string, input: unknown, invocation: string | undefined): Promise<unknown> {
    const dot = op.indexOf('.')
    const ns = dot === -1 ? op : op.slice(0, dot)
    const method = dot === -1 ? '' : op.slice(dot + 1)
    const session =
      (invocation !== undefined ? this.invocations.get(invocation)?.session : undefined) ??
      this.ambientSession
    return this.options.connection.request({
      kind: 'call',
      ns,
      method,
      args: input,
      mod: this.options.loaded.name,
      ...(session === undefined ? {} : { session }),
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

  /** Run the hook an `event` frame selects, and answer its `result` frame. */
  handleEvent(frame: Frame): void {
    const invocation = typeof frame.invocation === 'string' ? frame.invocation : undefined
    const event = typeof frame.event === 'string' ? frame.event : undefined
    const frameId = typeof frame.id === 'number' ? frame.id : -1
    if (invocation === undefined || event === undefined) {
      console.error('mod-runner: an event frame without invocation/event arrived; dropped')
      this.options.connection.respond(frameId, false, { code: 'hook-throw', message: 'an event frame without invocation/event arrived' })
      return
    }
    if (this.invocations.has(invocation)) {
      console.error(`mod-runner: invocation ${invocation} is already running; dropped the duplicate frame`)
      return
    }
    const session = typeof frame.session === 'string' ? frame.session : undefined
    // The payload arrived over JSON, so it is already a fresh copy; freezing it keeps the mods API's promise.
    const frozen = deepFreeze(frame.payload)
    const deadlineMs = typeof frame.deadlineMs === 'number' && frame.deadlineMs > 0 ? frame.deadlineMs : this.defaultDeadlineMs
    let hook: RegisteredHook
    try {
      hook = this.hookFor(event, frame.hook)
    } catch (error: unknown) {
      // The broker dispatches from its own registry, so this is a protocol slip, not a hook failure; answer and move on.
      const message = messageOf(error)
      console.error(`mod-runner: ${message}`)
      this.options.connection.respond(frameId, false, { code: 'no-hook', message })
      return
    }
    const clock = new BudgetClock(deadlineMs)
    const controller = new AbortController()
    const context: InvocationContext = {
      frameId,
      invocation,
      event,
      session,
      hook,
      frozen,
      clock,
      signal: controller.signal,
      deadlineMs,
      state: { abandoned: false, called: false, beneath: undefined, beneathFailed: false },
      failed: false,
      settled: false,
      deadlineTimer: undefined,
      catchGraceTimer: undefined,
    }
    this.invocations.set(invocation, context)
    context.deadlineTimer = setTimeout(() => {
      // The broker enforces the budget; this wall-clock backstop only covers a hook that never answers.
      this.settleFailure(context, { code: 'hook-throw', message: `ran past its ${deadlineMs} ms limit` })
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
      error => this.settleThrown(context, error),
    )
  }

  /** The broker gave up on this hook (`hook-timeout`): stop waiting; a late `next` answers undefined. */
  abandonHook(invocation: unknown): void {
    const context = typeof invocation === 'string' ? this.invocations.get(invocation) : undefined
    if (context === undefined || context.settled) return
    this.clearDeadline(context)
    context.state.abandoned = true
    context.settled = true
    this.options.connection.respond(context.frameId, false, {
      code: 'hook-throw',
      message: `ran past its ${context.deadlineMs} ms limit`,
    })
    this.invocations.delete(context.invocation)
  }

  /** Run the failed hook's `.catch` handler on the broker's `catch-call` frame. */
  handleCatchCall(frame: Frame): void {
    const id = typeof frame.id === 'number' ? frame.id : -1
    const invocation = typeof frame.invocation === 'string' ? frame.invocation : undefined
    const context = invocation === undefined ? undefined : this.invocations.get(invocation)
    if (context === undefined || !context.failed) {
      // The broker skips silently on `no-catch` (its wording for a handler that is not there).
      this.options.connection.respond(id, false, { code: 'no-catch', message: `no failed hook is waiting under ${String(invocation)}` })
      return
    }
    // The catch's answer rides on the catch-call frame's id, not the event's.
    const finish = (answer: { ok: boolean; value?: unknown; code?: string; message?: string }): void => {
      this.clearDeadline(context)
      context.settled = true
      this.clearCatchGrace(context)
      this.invocations.delete(context.invocation)
      this.options.connection.respond(id, answer.ok, {
        ...(answer.ok ? { value: answer.value } : { code: answer.code, message: answer.message }),
      })
    }
    const handler = context.hook.catchHandler
    if (handler === undefined) {
      // `no-catch` is the broker's silent skip: it does not know which hooks attached a handler.
      finish({ ok: false, code: 'no-catch', message: 'no catch handler is attached to this hook' })
      return
    }
    if (context.state.beneathFailed) {
      // The failure is the event's own (the chain beneath threw), not the hook's: a catch does not answer it.
      finish({ ok: false, code: 'no-catch', message: 'the chain beneath failed; the catch handler does not run' })
      return
    }
    const failure = recordFailure(frame.failure)
    const catchDeadlineMs = typeof frame.deadlineMs === 'number' && frame.deadlineMs > 0 ? frame.deadlineMs : 1000
    const clock = new BudgetClock(catchDeadlineMs)
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
      finish({ ok: false, code: 'catch-timeout', message: `the .catch handler ran past its ${catchDeadlineMs} ms limit` })
    }, catchDeadlineMs).unref()
    clock.start()
    Promise.resolve().then(() => handler(api, context.frozen, next as HookNext<unknown, unknown>)).then(
      result => {
        if (abandoned) return
        clearTimeout(timer)
        clock.stop()
        if (typeof result === 'object') finish({ ok: true, value: this.finalValue(context, result) })
        else finish({ ok: false, code: 'catch-throw', message: 'the .catch handler returned no result' })
      },
      error => {
        if (abandoned) return
        clearTimeout(timer)
        clock.stop()
        finish({ ok: false, code: 'catch-throw', message: failureLine(error) })
      },
    )
  }

  /** Answer the broker's `ui.press` call by running the named button callback. */
  handlePressCall(frame: Frame): void {
    const id = typeof frame.id === 'number' ? frame.id : -1
    const args = record_get(frame, 'args')
    const actionId = record_get(args, 'actionId')
    const session = typeof frame.session === 'string' ? frame.session : ''
    const callback = typeof actionId === 'string' ? this.pressCallbacks.get(`${session}\u0000${actionId}`) : undefined
    if (callback === undefined) {
      this.options.connection.respond(id, false, { code: 'not-found', message: `no press callback is registered for ${String(actionId)}` })
      return
    }
    // The callback's `$` calls ride this session (see ambientSession).
    this.ambientSession = session || undefined
    Promise.resolve().then(callback).then(
      value => {
        this.ambientSession = undefined
        this.options.connection.respond(id, true, { value: value === undefined ? null : value })
      },
      error => {
        this.ambientSession = undefined
        this.options.connection.respond(id, false, { code: 'error', message: failureLine(error) })
      },
    )
  }

  /** Answer the broker's `clock.fire` call by running the timer callback to completion. */
  handleFireCall(frame: Frame): void {
    const id = typeof frame.id === 'number' ? frame.id : -1
    const timer = record_get(record_get(frame, 'args'), 'timer')
    if (typeof timer !== 'string') {
      this.options.connection.respond(id, false, { code: 'not-found', message: 'a clock.fire call without a timer name arrived' })
      return
    }
    // Same ambient-session rule as a press: the timer's callback `$` calls
    // carry the frame's session when one rode along.
    const fireSession = typeof frame.session === 'string' ? frame.session : ''
    this.ambientSession = fireSession || undefined
    this.timers.fire(timer).then(
      () => {
        this.ambientSession = undefined
        this.options.connection.respond(id, true, { value: null })
      },
      error => {
        this.ambientSession = undefined
        this.options.connection.respond(id, false, { code: 'error', message: failureLine(error) })
      },
    )
  }

  /** A call this runner does not serve; the broker words the gap by the code. */
  respondUnknownCall(frame: Frame): void {
    const id = typeof frame.id === 'number' ? frame.id : -1
    this.options.connection.respond(id, false, {
      code: 'no-implementation',
      message: `no implementation for ${String(frame['ns'])}.${String(frame['method'])}`,
    })
  }

  // ---- Internals ----

  /**
   * The hook an event dispatches: the frame's `hook` field names the
   * registration (its position in this mod's `hooks-registered` list); the
   * event pattern is the fallback for brokers that omit it.
   */
  private hookFor(event: string, index: unknown): RegisteredHook {
    if (typeof index === 'number' && Number.isInteger(index) && index >= 0 && index < this.options.hooks.length) {
      return this.options.hooks[index] as RegisteredHook
    }
    const hook = this.options.hooks.find(candidate => candidate.event === event || (candidate.event.endsWith('.*') && event.startsWith(candidate.event.slice(0, -1))) || candidate.event === '*')
    if (hook === undefined) throw new Error(`no hook of this mod selects ${event}`)
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
        ...(opts.isCatch === true ? { phase: 'catch' } : {}),
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
    // A hook that answers after the deadline backstop fired (or after it failed) has no audience left.
    if (context.failed || context.settled) return
    // `null` is an answer (a surface drawn empty); only a hook that settles with no object at all is skipped.
    if (typeof result !== 'object') {
      this.settleFailure(context, { code: 'no-result', message: `${context.event} hook returned no result` })
      return
    }
    let value: unknown
    try {
      value = this.finalValue(context, result)
    } catch (error: unknown) {
      // An invalid tree is the hook's failure; word it as one.
      this.settleFailure(context, { code: 'hook-throw', message: messageOf(error) })
      return
    }
    // The hook answered; whatever it started beneath still runs to its end before the event settles.
    const finish = (): void => this.respondValue(context, value)
    if (context.state.beneath !== undefined) {
      context.state.beneath.catch((error: unknown) => {
        this.report(`${this.options.loaded.name}: ${context.event}: the chain beneath failed after the hook answered: ${messageOf(error)}`)
      }).then(finish, finish)
      return
    }
    finish()
  }

  /**
   * A hook that threw. A protocol error from `next()` that the hook let
   * through (or rethrew) rides verbatim — the broker matches beneath failures
   * by code+message, the distributed stand-in for DSH's error identity check.
   */
  private settleThrown(context: InvocationContext, error: unknown): void {
    const code = record_get(error, 'code')
    if (context.state.beneathFailed && typeof code === 'string') {
      this.settleFailure(context, { code: 'hook-throw', message: messageOf(error), wireCode: code })
      return
    }
    this.settleFailure(context, { code: 'hook-throw', message: failureLine(error) })
  }

  private settleFailure(context: InvocationContext, answer: HookAnswer & { wireCode?: string }): void {
    this.clearDeadline(context)
    context.state.abandoned = true
    // One answer per invocation: a late throw after the deadline backstop fired changes nothing.
    if (context.failed || context.settled) return
    const line = `${this.options.loaded.name}: ${context.event} hook skipped: ${answer.message}`
    if (!context.hook.reported.has(answer.code)) {
      context.hook.reported.add(answer.code)
      this.report(line)
    }
    context.failed = true
    this.options.connection.respond(context.frameId, false, {
      code: answer.wireCode ?? answer.code,
      message: answer.message,
    })
    // The broker may still want the `.catch` to answer; hold the context for it, then sweep.
    context.catchGraceTimer = setTimeout(() => {
      this.invocations.delete(context.invocation)
    }, CATCH_GRACE_MS).unref()
  }

  /** Send the invocation's successful answer and drop its state. */
  private respondValue(context: InvocationContext, value: unknown): void {
    this.clearDeadline(context)
    context.settled = true
    this.clearCatchGrace(context)
    this.invocations.delete(context.invocation)
    this.options.connection.respond(context.frameId, true, { value })
  }

  private clearDeadline(context: InvocationContext): void {
    if (context.deadlineTimer !== undefined) clearTimeout(context.deadlineTimer)
    context.deadlineTimer = undefined
  }

  private clearCatchGrace(context: InvocationContext): void {
    if (context.catchGraceTimer !== undefined) clearTimeout(context.catchGraceTimer)
    context.catchGraceTimer = undefined
  }

  /**
   * The value an answer frame carries: for a tree event, the hook's elements
   * serialized with each `onPress` replaced by a runner-minted action id
   * (`<mod>:a<N>`, positional within the drawing, so an unchanged tree stays
   * JSON-equal across redraws and keeps its generation); anything else passes
   * as JSON as-is.
   */
  private finalValue(context: InvocationContext, result: unknown): unknown {
    const event = context.event
    if (!TREE_EVENTS.has(event)) return result === undefined ? null : result
    if (result === null || result === undefined) return null
    if (looksSerialized(result)) return result // A tree `next` handed up from a later mod: already serialized.
    const problem = treeProblem(result as UiNode)
    if (problem !== undefined) throw new Error(problem)
    // The drawing replaces the session's previous callbacks; ids restart at a0 so the
    // same tree serializes byte-identically every time.
    const prefix = `${context.session ?? ''}\u0000`
    for (const key of [...this.pressCallbacks.keys()]) {
      if (key.startsWith(prefix)) this.pressCallbacks.delete(key)
    }
    let index = 0
    return serializeTree(result as UiNode, callback => {
      const actionId = `${this.options.loaded.name}:a${index}`
      index += 1
      this.pressCallbacks.set(`${prefix}${actionId}`, callback)
      return actionId
    })
  }
}

/** Whether a value already reads as a serialized tree (what `next` hands up from a later mod). */
function looksSerialized(value: unknown): boolean {
  const nodes = Array.isArray(value) ? value : [value]
  return nodes.every(node => typeof node === 'string' || (typeof node === 'object' && node !== null
    && !isUiElement(node) // a branded element is a live tree, however much it resembles the JSON
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
