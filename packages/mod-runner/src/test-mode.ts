/**
 * `--test <dir>`: a minimal in-process test suite for one mod. The mod loads
 * through the exact production path ({@link runRunner}) against a mock broker
 * made of two PassThrough streams; every registered hook then receives one
 * synthetic event (payload shapes per events.py §5.3/§15.7), its `$` calls are
 * answered with benign values, and a `ui.render` tree's buttons are pressed.
 * The report lists: the hooks that registered, the hooks whose execution
 * threw, and the `$` calls nothing serves. Human-readable on stdout.
 *
 * Exit-code contract (also in README): 0 = every hook ran clean; 1 = the mod
 * failed to load/register, or at least one hook (or button press) threw; 2 =
 * usage error. Unimplemented `$` calls and registered-but-never-raised events
 * are compatibility warnings — reported loudly, but they do not fail the run
 * (a mod may call `$.model.complete` behind a feature flag on purpose).
 * @module
 */

import { PassThrough } from 'node:stream'
import { runRunner } from './index.ts'
import type { RunnerHandle } from './index.ts'
import { messageOf } from './values.ts'
import type { Frame } from './frames.ts'

/** How long one synthetic hook run (or one press) may take before it counts as a failure. */
const RUN_TIMEOUT_MS = 5_000

/** One hook's synthetic run outcome. */
export interface HookOutcome {
  event: string
  matcher: Record<string, unknown> | undefined
  ok: boolean
  ms: number
  /** code + message when the hook threw / never answered. */
  error?: { code: string; message: string }
}

/** One `$` op namespace.method the mod invoked, with its wire outcomes. */
export interface OpStat {
  op: string
  called: number
  /** The mock answered ok — whether the mod liked the answer is its own business. */
  ok: number
}

/** The whole report {@link testMod} produces. */
export interface TestReport {
  mod: { name: string; version: string | undefined; root: string }
  loaded: boolean
  loadError?: string
  hooks: HookOutcome[]
  ops: OpStat[]
  /** Registered events this host never raises (warnings, design §5.3). */
  unservedEvents: string[]
  /** `ns.member` names the `$` shim rejected locally (warnings). */
  unimplementedOps: string[]
  /** Button presses exercised on the last ui.render tree. */
  presses: { tried: number; failed: number }
}

/** Synthetic event payloads on top of `sessionId`; the shapes events.py dispatches (§5.3/§15.7). */
const SYNTHETIC_PAYLOADS: Record<string, Record<string, unknown>> = {
  'session.start': { cwd: process.cwd(), surface: null, isInteractive: false },
  'session.end': { reason: 'other' },
  'turn.start': { turnId: 'test-turn' },
  'turn.complete': { turnId: 'test-turn', answer: '', durationMs: 0, isAborted: false, reason: 'answer', usage: null },
  'prompt.submit': { text: 'hello from mod-runner --test', origin: { kind: 'composer' } },
  'tool.call': { tool: 'Bash', args: {} },
  'tool.check': { tool: 'Bash', args: {} },
  'command.run': { command: 'test', args: '' },
  'agent.spawn': { agent: 'test' },
  'session.compact': {},
  'ui.render': { reason: 'redraw' },
  'ui.press': { actionId: 'test:a0' },
  'ui.input': { actionId: 'test:a0', value: '' },
}

/** A registered hook as the handshake's `hooks-registered` args carry it. */
interface RegisteredHookInfo {
  event: string
  matcher?: Record<string, unknown>
}

interface Waiter {
  test: (frame: Frame) => boolean
  resolve: (frame: Frame) => void
}

/**
 * The mock broker half of the wire: answers the runner's handshake, serves
 * every `$` op with a benign value, passes `next` through, and lets the test
 * await specific frames (each event's result, each press's result).
 */
class TestBroker {
  readonly toRunner = new PassThrough()
  readonly fromRunner = new PassThrough()
  /** Hook registrations as the runner reported them. */
  registered: RegisteredHookInfo[] = []
  /** `ns.method` → call count, for the report's ops section. */
  readonly opCounts = new Map<string, number>()
  /** Fire-and-forget failure lines (`notify report`) the runner sent. */
  readonly reports: string[] = []

  private timerSeq = 0
  private buffer = ''
  private readonly waiters: Waiter[] = []
  private readonly seen: Frame[] = []

  constructor() {
    this.fromRunner.on('data', (chunk: Buffer | string) => {
      this.buffer += typeof chunk === 'string' ? chunk : chunk.toString('utf8')
      let newline: number
      while ((newline = this.buffer.indexOf('\n')) !== -1) {
        const line = this.buffer.slice(0, newline)
        this.buffer = this.buffer.slice(newline + 1)
        if (line.trim().length === 0) continue
        try {
          this.receive(JSON.parse(line) as Frame)
        } catch {
          /* a torn line never happens over PassThrough; ignore regardless */
        }
      }
    })
  }

  /** Send one frame to the runner. */
  send(frame: Frame): void {
    this.toRunner.write(`${JSON.stringify({ v: 1, ...frame })}\n`)
  }

  /** The next frame matching `test`, backlog included; rejects past `timeoutMs`. */
  take(test: (frame: Frame) => boolean, timeoutMs = RUN_TIMEOUT_MS): Promise<Frame> {
    for (const frame of this.seen) {
      if (test(frame)) return Promise.resolve(frame)
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`no frame within ${String(timeoutMs)} ms`)), timeoutMs).unref()
      this.waiters.push({
        test,
        resolve: frame => {
          clearTimeout(timer)
          resolve(frame)
        },
      })
    })
  }

  private receive(frame: Frame): void {
    this.seen.push(frame)
    if (frame.kind === 'result') {
      for (const [index, waiter] of this.waiters.entries()) {
        if (waiter.test(frame)) {
          this.waiters.splice(index, 1)
          waiter.resolve(frame)
          return
        }
      }
      return
    }
    if (frame.kind === 'notify') {
      if (frame.method === 'report') {
        const message = (frame.args as Record<string, unknown> | undefined)?.message
        if (typeof message === 'string') this.reports.push(message)
      }
      return
    }
    if (frame.kind === 'call' || frame.kind === 'next') this.answer(frame)
  }

  private answer(frame: Frame): void {
    const id = typeof frame.id === 'number' ? frame.id : -1
    if (frame.kind === 'call' && frame.ns === 'runner') {
      // The handshake: config defaults (nothing pushed), then the start ack.
      if (frame.method === 'loaded') this.write({ kind: 'result', id, ok: true, value: { config: {} } })
      else if (frame.method === 'hooks-registered') {
        const rawHooks: unknown = (frame.args as Record<string, unknown> | undefined)?.hooks
        const hooks = Array.isArray(rawHooks) ? rawHooks : []
        this.registered = hooks.map(hook => {
          const record = (hook ?? {}) as Record<string, unknown>
          return {
            event: typeof record.event === 'string' ? record.event : '?',
            matcher: typeof record.matcher === 'object' && record.matcher !== null
              ? record.matcher as Record<string, unknown>
              : undefined,
          }
        })
        this.write({ kind: 'result', id, ok: true, value: 'start' })
      } else {
        this.write({ kind: 'result', id, ok: false, code: 'no-implementation', message: `no implementation for runner.${String(frame.method)}` })
      }
      return
    }
    if (frame.kind === 'next') {
      // The chain beneath passes the event through untouched — the most
      // neutral stand-in for "the engine answered".
      this.write({ kind: 'result', id, ok: true, value: frame.e === undefined ? null : frame.e })
      return
    }
    // A `$` op: count it and answer a benign value. `clock.*` timers must name
    // a timer or the runner reports a failure; `state.get` needs `{value}`;
    // everything else answers `{}` — property reads yield undefined, which is
    // what every mod's destructuring defaults expect.
    const op = `${String(frame.ns)}.${String(frame.method)}`
    this.opCounts.set(op, (this.opCounts.get(op) ?? 0) + 1)
    let value: unknown = {}
    if (op === 'clock.after' || op === 'clock.every') value = { timer: `t${++this.timerSeq}` }
    else if (op === 'state.get') value = { value: null }
    this.write({ kind: 'result', id, ok: true, value })
  }

  private write(frame: Frame): void {
    this.toRunner.write(`${JSON.stringify({ v: 1, ...frame })}\n`)
  }
}

/** Load one mod through the production path and exercise every hook. */
export async function testMod(modDir: string): Promise<TestReport> {
  const broker = new TestBroker()
  const report: TestReport = {
    mod: { name: '', version: undefined, root: modDir },
    loaded: false,
    hooks: [],
    ops: [],
    unservedEvents: [],
    unimplementedOps: [],
    presses: { tried: 0, failed: 0 },
  }
  let handle: RunnerHandle
  try {
    handle = await runRunner(modDir, broker.toRunner, broker.fromRunner, { pingIntervalMs: 60_000 })
  } catch (error: unknown) {
    report.loadError = messageOf(error)
    return report
  }
  report.loaded = true
  report.mod.name = handle.mod.name
  report.mod.version = handle.mod.version
  report.mod.root = handle.mod.root

  // One synthetic event per registered hook, in registration order.
  for (const [index, hook] of broker.registered.entries()) {
    await runSynthetic(broker, report, hook, index)
  }

  handle.stop()
  broker.toRunner.end()

  report.ops = [...broker.opCounts.entries()]
    .map(([op, called]) => ({ op, called, ok: called }))
    .sort((a, b) => a.op.localeCompare(b.op))
  report.unservedEvents = unservedEventNames(handle, broker.registered.map(hook => hook.event))
  report.unimplementedOps = unimplementedOpNames([
    ...broker.reports,
    ...report.hooks.flatMap(hook => (hook.error ? [hook.error.message] : [])),
  ])
  return report
}

/** Dispatch one hook's synthetic event, await its answer, record the outcome. */
async function runSynthetic(broker: TestBroker, report: TestReport, hook: RegisteredHookInfo, index: number): Promise<void> {
  const started = Date.now()
  const frameId = 10_000 + index
  broker.send({
    kind: 'event',
    id: frameId,
    event: hook.event,
    session: 'test-session',
    invocation: `test-${index + 1}`,
    payload: { sessionId: 'test-session', ...(SYNTHETIC_PAYLOADS[hook.event] ?? {}) },
    deadlineMs: RUN_TIMEOUT_MS,
    hook: index,
  })
  let result: Frame
  try {
    result = await broker.take(frame => frame.kind === 'result' && frame.id === frameId)
  } catch {
    report.hooks.push({
      event: hook.event,
      matcher: hook.matcher,
      ok: false,
      ms: Date.now() - started,
      error: { code: 'no-answer', message: `the hook never answered within ${String(RUN_TIMEOUT_MS)} ms (pending timers?)` },
    })
    return
  }
  if (result.ok !== true) {
    report.hooks.push({
      event: hook.event,
      matcher: hook.matcher,
      ok: false,
      ms: Date.now() - started,
      error: {
        code: typeof result.code === 'string' ? result.code : 'error',
        message: typeof result.message === 'string' ? result.message : 'unknown failure',
      },
    })
    return
  }
  report.hooks.push({ event: hook.event, matcher: hook.matcher, ok: true, ms: Date.now() - started })
  // A ui.render answer is a serialized tree; press every button in it.
  if (hook.event === 'ui.render') await pressTree(broker, report, result.value)
}

/** Press every button of a serialized ui.render tree, recording failures as hook outcomes. */
async function pressTree(broker: TestBroker, report: TestReport, value: unknown): Promise<void> {
  for (const actionId of collectActionIds(value)) {
    report.presses.tried += 1
    const id = 20_000 + report.presses.tried
    broker.send({
      kind: 'call',
      ns: 'ui',
      method: 'press',
      id,
      mod: report.mod.name,
      session: 'test-session',
      args: { actionId, generation: 1 },
    })
    let result: Frame
    try {
      result = await broker.take(frame => frame.kind === 'result' && frame.id === id)
    } catch {
      report.presses.failed += 1
      report.hooks.push({
        event: `ui.press{${actionId}}`,
        matcher: undefined,
        ok: false,
        ms: RUN_TIMEOUT_MS,
        error: { code: 'no-answer', message: `the press callback never completed within ${String(RUN_TIMEOUT_MS)} ms` },
      })
      continue
    }
    if (result.ok === true) continue
    report.presses.failed += 1
    report.hooks.push({
      event: `ui.press{${actionId}}`,
      matcher: undefined,
      ok: false,
      ms: 0,
      error: {
        code: typeof result.code === 'string' ? result.code : 'error',
        message: typeof result.message === 'string' ? result.message : 'the press callback failed',
      },
    })
  }
}

/** Every `actionId` in a serialized tree, in draw order. */
function collectActionIds(value: unknown, found: string[] = []): string[] {
  if (Array.isArray(value)) {
    for (const item of value) collectActionIds(item, found)
    return found
  }
  if (typeof value !== 'object' || value === null) return found
  const record = value as Record<string, unknown>
  if (typeof record.actionId === 'string') found.push(record.actionId)
  if (Array.isArray(record.children)) collectActionIds(record.children, found)
  return found
}

/** `no implementation for ns.member` mentions, deduped, in first-seen order. */
function unimplementedOpNames(messages: readonly string[]): string[] {
  const names: string[] = []
  for (const message of messages) {
    const name = /no implementation for ([A-Za-z0-9_.]+)/.exec(message)?.[1]
    if (name !== undefined && !names.includes(name)) names.push(name)
  }
  return names
}

/** Registered events this host never raises, deduped, registration order. */
function unservedEventNames(handle: RunnerHandle, events: readonly string[]): string[] {
  const names: string[] = []
  for (const event of events) {
    if (!handle.servedEvents.has(event) && !names.includes(event)) names.push(event)
  }
  return names
}

// ---- rendering -----------------------------------------------------------------

/** The matcher as `validate` prints it (`{tool=/^Write/}`), for report lines. */
function matcherText(matcher: Record<string, unknown> | undefined): string {
  if (matcher === undefined) return ''
  return `{${Object.entries(matcher).map(([field, value]) => `${field}=${renderMatcherValue(value)}`).join(',')}}`
}

function renderMatcherValue(value: unknown): string {
  if (typeof value === 'object' && value !== null && typeof (value as Record<string, unknown>).regexp === 'string') {
    const source = (value as Record<string, unknown>).regexp as string
    const flags = typeof (value as Record<string, unknown>).flags === 'string' ? (value as Record<string, unknown>).flags as string : ''
    return `/${source}/${flags}`
  }
  if (Array.isArray(value)) return value.map(renderMatcherValue).join('|')
  return String(value)
}

/**
 * The human-readable report. `✓` hooks ran clean, `✗` threw; the ops section
 * shows every `$` call the mod made, then the two compatibility warnings.
 */
export function renderReport(report: TestReport): string {
  const lines: string[] = []
  const title = `mod test report — ${report.mod.name}${report.mod.version ? ` v${report.mod.version}` : ''}`
  lines.push(title)
  lines.push(`root: ${report.mod.root}`)
  lines.push('')
  if (!report.loaded) {
    lines.push(`✗ mod not loaded: ${report.loadError ?? 'unknown error'}`)
    lines.push('')
    lines.push('result: FAIL (mod not loaded)')
    return lines.join('\n')
  }
  lines.push('hooks')
  for (const hook of report.hooks) {
    const shape = `${hook.event}${matcherText(hook.matcher)}`
    if (hook.ok) lines.push(`  ✓ ${shape} (${String(hook.ms)} ms)`)
    else lines.push(`  ✗ ${shape} — ${hook.error?.code ?? 'error'}: ${hook.error?.message ?? ''}`)
  }
  lines.push('')
  if (report.ops.length > 0) {
    lines.push('$ ops exercised')
    for (const op of report.ops) lines.push(`  · ${op.op} ×${String(op.called)}`)
    lines.push('')
  }
  if (report.unimplementedOps.length > 0) {
    lines.push('warning: $ calls with no implementation in this host (rejected at runtime):')
    for (const name of report.unimplementedOps) lines.push(`  · ${name}`)
    lines.push('')
  }
  if (report.unservedEvents.length > 0) {
    lines.push('warning: events registered but never raised by this host (the hooks never run):')
    for (const event of report.unservedEvents) lines.push(`  · ${event}`)
    lines.push('')
  }
  const failed = report.hooks.filter(hook => !hook.ok).length
  lines.push(`result: ${failed === 0 ? 'OK' : `FAIL (${String(failed)} hook failure${failed === 1 ? '' : 's'})`}`)
  return lines.join('\n')
}

/** The exit code `--test` ends with, per the module-doc contract. */
export function exitCodeOf(report: TestReport): number {
  if (!report.loaded) return 1
  return report.hooks.some(hook => !hook.ok) ? 1 : 0
}
