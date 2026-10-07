/**
 * A mock broker for the runner tests: drives {@link runRunner} over
 * PassThrough streams, answers the runner's `call`/`next` requests through a
 * per-test handler, and lets tests await any frame it sent, including
 * backlogged ones.
 * @module
 */

import { PassThrough } from 'node:stream'
import { runRunner } from '../src/index.ts'
import type { RunnerHandle } from '../src/index.ts'
import { messageOf } from '../src/values.ts'
import type { Frame, ResultFrame } from '../src/frames.ts'

/** The handler one test installs for the runner's `call` and `next` requests. */
export type RequestHandler = (frame: Frame) => unknown | Promise<unknown>

/** A named refusal, as a request handler throws it. */
export class BrokerError extends Error {
  constructor(readonly code: string, message: string) {
    super(message)
  }
}

/** The default handler: `next` passes the input through, `call` names the missing implementation. */
const defaultHandler: RequestHandler = frame => {
  if (frame.kind === 'next') return frame['e']
  throw new BrokerError('no-implementation', `no implementation for ${String(frame['ns'])}.${String(frame['method'])}`)
}

export class MockBroker {
  /** Every frame the runner sent, in order. */
  readonly sent: Frame[] = []
  /** The runner's handles; ready once the promise resolves. */
  readonly runner: Promise<RunnerHandle>
  /** Resolved with stdin-EOF's error (always undefined) after the stream ends. */
  readonly exited: Promise<Error | undefined>

  private onRequest: RequestHandler = defaultHandler
  private startOptions: Record<string, unknown> = {}
  private readonly toRunner = new PassThrough()
  private readonly fromRunner = new PassThrough()
  private readonly waiters: { test: (frame: Frame) => boolean; resolve: (frame: Frame) => void }[] = []
  private readonly consumed = new Set<Frame>()
  private buffer = ''
  private lineIndex = 0

  constructor(readonly modDir: string, pingIntervalMs = 20) {
    let signalExit!: (error: Error | undefined) => void
    this.exited = new Promise(resolve => { signalExit = resolve })
    this.runner = runRunner(modDir, this.toRunner, this.fromRunner, {
      onExit: signalExit,
      pingIntervalMs,
    })
    this.fromRunner.on('data', (chunk: Buffer | string) => {
      this.buffer += typeof chunk === 'string' ? chunk : chunk.toString('utf8')
      let newline = this.buffer.indexOf('\n', this.lineIndex)
      while (newline !== -1) {
        const line = this.buffer.slice(this.lineIndex, newline)
        this.lineIndex = newline + 1
        if (line.trim().length > 0) this.receive(JSON.parse(line) as Frame)
        newline = this.buffer.indexOf('\n', this.lineIndex)
      }
    })
  }

  /** Install the handler for the runner's `call`/`next` requests. */
  replies(handler: RequestHandler): this {
    this.onRequest = handler
    return this
  }

  /** Send one frame to the runner (`event`, `catch-call`, a `clock.fire`/`ui.press` call, `shutdown`). */
  send(frame: Frame): void {
    this.toRunner.write(`${JSON.stringify({ v: 1, ...frame })}\n`)
  }

  /** The handshake's config side: what the runner's `loaded` call gets back. */
  start(options: Record<string, unknown> = {}): void {
    this.startOptions = options
  }

  /** The next unsent-yet frame matching `test`, backlog included; fails the test after 2 s. */
  take(test: (frame: Frame) => boolean, timeoutMs = 2_000): Promise<Frame> {
    const hit = this.scan(test)
    if (hit !== undefined) return Promise.resolve(hit)
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const kinds = this.sent.map(frame => `${frame.kind}:${String(frame['event'] ?? frame['method'] ?? frame['ns'] ?? '')}`)
        reject(new Error(`no matching frame within ${String(timeoutMs)} ms; saw ${JSON.stringify(kinds)}`))
      }, timeoutMs).unref()
      this.waiters.push({
        test,
        resolve: frame => {
          clearTimeout(timer)
          resolve(frame)
        },
      })
    })
  }

  /** End stdin, as a dead broker does; resolves once the runner reports its exit. */
  async close(): Promise<Error | undefined> {
    this.toRunner.end()
    return this.exited
  }

  private scan(test: (frame: Frame) => boolean): Frame | undefined {
    for (const frame of this.sent) {
      if (!this.consumed.has(frame) && test(frame)) {
        this.consumed.add(frame)
        return frame
      }
    }
    return undefined
  }

  private receive(frame: Frame): void {
    this.sent.push(frame)
    for (const [index, waiter] of this.waiters.entries()) {
      const hit = this.scan(waiter.test)
      if (hit !== undefined) {
        this.waiters.splice(index, 1)
        waiter.resolve(hit)
        break
      }
    }
    if (frame.kind === 'call' || frame.kind === 'next') this.answer(frame)
  }

  private answer(frame: Frame): void {
    // The meta calls the real broker serves: the config side of the handshake, and the `start` ack.
    if (frame.kind === 'call' && frame['ns'] === 'runner' && frame['method'] === 'loaded') {
      this.write({ kind: 'result', id: requestId(frame), ok: true, value: { config: this.startOptions } })
      return
    }
    if (frame.kind === 'call' && frame['ns'] === 'runner' && frame['method'] === 'hooks-registered') {
      this.write({ kind: 'result', id: requestId(frame), ok: true, value: 'start' })
      return
    }
    Promise.resolve().then(() => this.onRequest(frame)).then(
      value => {
        // A handler may signal failure by returning an Error as well as by throwing one.
        if (value instanceof Error) return this.write({
          kind: 'result',
          id: requestId(frame),
          ok: false,
          code: value instanceof BrokerError ? value.code : 'error',
          message: messageOf(value),
        })
        this.write({ kind: 'result', id: requestId(frame), ok: true, value: value === undefined ? null : value })
      },
      (error: unknown) => this.write({
        kind: 'result',
        id: requestId(frame),
        ok: false,
        code: error instanceof BrokerError ? error.code : 'error',
        message: messageOf(error),
      }),
    )
  }

  private write(frame: Frame): void {
    this.toRunner.write(`${JSON.stringify({ v: 1, ...frame })}\n`)
  }
}

function requestId(frame: Frame): number {
  return typeof frame['id'] === 'number' ? frame['id'] : -1
}

/** A narrowed `result` view of a taken frame, for assertions. */
export function asResult(frame: Frame): ResultFrame {
  return frame as ResultFrame
}
