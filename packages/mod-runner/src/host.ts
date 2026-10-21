/**
 * The host process (2026-10-21 方案 B: one Node process for ALL mods, each in
 * a worker_threads isolate): `node mod-runner.mjs --host`.
 *
 * The broker speaks the SAME newline-JSON protocol over this process's stdio,
 * multiplexed per mod:
 *   broker → host  `load-mod` / `unload-mod` control frames; every event /
 *     catch-call / call / result frame carries the target `mod` name.
 *   host → broker  worker frames forwarded with `mod` stamped; worker-
 *     initiated `call` / `next` ids are offset by a per-worker base
 *     ((seq+1)·2⁴⁰) so ids from different workers never collide on the wire,
 *     and a broker `result` for base+id routes back with the base stripped.
 *
 * Isolation preserved from the per-process design: a crashing or spinning
 * mod dies in its own worker (own event loop, own globals — the JSX pragma
 * injection included); the host watches each worker's 5 s ping and terminates
 * it after 15 s of silence, then notifies `mod.exited`, so the broker's
 * existing per-mod restart / three-strike logic applies unchanged.
 * @module
 */

import { Worker } from 'node:worker_threads'

/** Per-worker id offset; ids stay < 2⁵³ so JSON round-trips exactly. */
const ID_BASE_STRIDE = 2 ** 40
/** A worker this quiet is terminated (its own event loop is stuck). */
const WORKER_SILENCE_MS = 15_000
const WATCHDOG_INTERVAL_MS = 5_000

interface HostedWorker {
  readonly name: string
  readonly worker: Worker
  readonly base: number
  lastSeen: number
  /** True while a deliberate unload/terminate is in flight: silent exit. */
  stopping: boolean
}

export interface HostOptions {
  /** Test seams; production runs on stdio. */
  input?: NodeJS.ReadableStream
  output?: NodeJS.WritableStream
  /** Which file to spawn workers of (defaults to this bundle). */
  workerEntry?: string
}

/**
 * Serve the host loop until stdin ends. Never resolves in production.
 * @param options - streams and the worker entry path.
 */
export async function runHost(options: HostOptions = {}): Promise<void> {
  const input = options.input ?? process.stdin
  const output = options.output ?? process.stdout
  const entry = options.workerEntry ?? process.argv[1]!
  const workers = new Map<string, HostedWorker>()
  let workerSeq = 0

  const send = (frame: Record<string, unknown>): void => {
    output.write(`${JSON.stringify({ v: 1, ...frame })}\n`)
  }

  const onWorkerExit = (hosted: HostedWorker, code: number): void => {
    if (workers.get(hosted.name) !== hosted) return
    workers.delete(hosted.name)
    if (hosted.stopping) return
    // The broker's on_runner_exit path (restart / three-strike) hangs off this.
    send({ kind: 'notify', method: 'mod.exited', mod: hosted.name, args: { name: hosted.name, code } })
  }

  const spawnWorker = (name: string, dir: string, grants: unknown, config: unknown): void => {
    const existing = workers.get(name)
    if (existing !== undefined) return
    const base = (++workerSeq) * ID_BASE_STRIDE
    const worker = new Worker(entry, {
      workerData: { modDir: dir, grants, config },
      stdout: true,
      stderr: true,
    })
    const hosted: HostedWorker = { name, worker, base, lastSeen: Date.now(), stopping: false }
    workers.set(name, hosted)
    // Runner diagnostics (describeRegistrations, load errors) keep flowing to
    // the shared stderr log, prefixed so lines stay attributable per mod.
    worker.stdout?.on('data', (chunk: Buffer) => process.stderr.write(`mod-${name}: ${String(chunk)}`))
    worker.stderr?.on('data', (chunk: Buffer) => process.stderr.write(`mod-${name}: ${String(chunk)}`))
    worker.on('message', (line: unknown) => {
      if (typeof line !== 'string') return
      hosted.lastSeen = Date.now()
      onWorkerFrame(hosted, line)
    })
    worker.on('exit', code => onWorkerExit(hosted, code))
    worker.on('error', error => {
      process.stderr.write(`mod-${name}: worker error: ${String(error.message)}\n`)
    })
  }

  const onWorkerFrame = (hosted: HostedWorker, line: string): void => {
    let frame: Record<string, unknown>
    try {
      frame = JSON.parse(line) as Record<string, unknown>
    } catch {
      process.stderr.write(`mod-${hosted.name}: host dropped an unparseable worker line\n`)
      return
    }
    if (frame.kind === 'result') {
      // Broker-initiated ids pass as-is (events, presses); worker-initiated
      // ones already carry the base offset. Both need the mod stamp so the
      // host-mode broker routes the reply to THIS mod's virtual conn.
      send({ ...frame, mod: hosted.name })
      return
    }
    if (frame.kind === 'call' || frame.kind === 'next') {
      // Worker-initiated request: offset the id into this worker's range so
      // the broker's result routes home, and stamp the owning mod.
      const id = typeof frame.id === 'number' ? hosted.base + frame.id : frame.id
      send({ ...frame, id, mod: hosted.name })
      return
    }
    // notify (ping, mod.status, ui.*) and anything else: stamp and forward.
    send({ ...frame, mod: frame.mod ?? hosted.name })
  }

  const onBrokerLine = (line: string): void => {
    if (line.trim().length === 0) return
    let frame: Record<string, unknown>
    try {
      frame = JSON.parse(line) as Record<string, unknown>
    } catch {
      process.stderr.write('mod-host: dropped an unparseable broker line\n')
      return
    }
    if (frame.kind === 'load-mod') {
      spawnWorker(
        String(frame.name ?? ''),
        String(frame.dir ?? ''),
        frame.grants,
        frame.config,
      )
      return
    }
    if (frame.kind === 'unload-mod') {
      const hosted = workers.get(String(frame.name ?? ''))
      if (hosted !== undefined) {
        hosted.stopping = true
        void hosted.worker.terminate()
      }
      return
    }
    const name = typeof frame.mod === 'string' ? frame.mod : undefined
    const hosted = name !== undefined ? workers.get(name) : undefined
    if (hosted === undefined) {
      // A frame for a mod that just exited (race with unload): drop it.
      process.stderr.write(`mod-host: frame for unknown mod ${String(name)} dropped\n`)
      return
    }
    let forwarded = frame
    if (frame.kind === 'result' && typeof frame.id === 'number' && frame.id >= hosted.base) {
      // Answer to a worker-initiated request: strip this worker's base.
      forwarded = { ...frame, id: frame.id - hosted.base }
    }
    hosted.worker.postMessage(`${JSON.stringify(forwarded)}\n`)
  }

  let lineBuffer = ''
  input.on('data', (chunk: Buffer | string) => {
    lineBuffer += typeof chunk === 'string' ? chunk : chunk.toString('utf8')
    let newline = lineBuffer.indexOf('\n')
    while (newline !== -1) {
      onBrokerLine(lineBuffer.slice(0, newline))
      lineBuffer = lineBuffer.slice(newline + 1)
      newline = lineBuffer.indexOf('\n')
    }
  })
  const shutdown = (): void => {
    for (const hosted of workers.values()) {
      hosted.stopping = true
      void hosted.worker.terminate()
    }
  }
  input.on('end', shutdown)
  input.on('error', shutdown)

  const watchdog = setInterval(() => {
    const now = Date.now()
    for (const hosted of workers.values()) {
      if (now - hosted.lastSeen > WORKER_SILENCE_MS) {
        // The worker's own event loop is stuck (its 5 s ping stopped); only
        // terminate() reaches it. stopping stays false so the exit handler
        // notifies mod.exited and the broker restarts the mod.
        process.stderr.write(`mod-${hosted.name}: worker silent ${now - hosted.lastSeen} ms; terminating\n`)
        void hosted.worker.terminate()
      }
    }
  }, WATCHDOG_INTERVAL_MS)
  watchdog.unref()
}
