/**
 * The runner's entry point: load one mod (`--mod <dir>`), register its hooks
 * with the options the broker's `start` frame carries, report the
 * registrations, and then serve frames until stdin ends or the broker says
 * shutdown. The runner pings every 5 s so the broker can judge it dead.
 * @module
 */

import { pathToFileURL } from 'node:url'
import { FrameConnection } from './frames.ts'
import type { Frame } from './frames.ts'
import { importHooksModule, readManifest } from './loader.ts'
import { describeRegistrations, registerMod } from './module.ts'
import type { LoadedMod } from './module.ts'
import { ENGINE_EVENTS, serializeMatcher } from './matcher.ts'
import { HookRuntime } from './hook-runtime.ts'
import { messageOf } from './values.ts'
import type { ModRegister, PluginOptions } from './types.ts'

/** How often the runner pings the broker (the broker judges death at 15 s). */
const PING_INTERVAL_MS = 5_000
/** How long the runner waits for the `loaded` call's config before registering with defaults. */
const LOADED_TIMEOUT_MS = 2_000

/** The engine events this host raises (design §5.3, P0/P1 rows); anything else registers but never runs. */
export const SERVED_EVENTS: ReadonlySet<string> = new Set([
  'session.start', 'session.end', 'turn.start', 'turn.complete', 'prompt.submit',
  'tool.call', 'tool.check', 'command.run', 'agent.spawn', 'session.compact',
  'ui.render', 'ui.press', 'ui.input',
])

/** What {@link runRunner} leaves with the caller. */
export interface RunnerHandle {
  readonly connection: FrameConnection
  readonly runtime: HookRuntime
  /** The loaded mod's identity, as the manifests named it (`--test` reports it). */
  readonly mod: LoadedMod
  /** The engine events this host actually raises; registrations outside it never run. */
  readonly servedEvents: ReadonlySet<string>
  /** Stops the ping interval; the frames already written stay in flight. */
  stop(): void
}

/** Tuning the real runner leaves at its defaults and tests shorten. */
export interface RunnerOptions {
  /** Called once stdin ends or a `shutdown` frame arrives; the real runner exits there. */
  readonly onExit?: (error: Error | undefined) => void
  /** Ping cadence; the production value is {@link PING_INTERVAL_MS}. */
  readonly pingIntervalMs?: number
}

/**
 * Load, register, and serve one mod over the given streams.
 * @param modDir - the mod directory (the `--mod` argument).
 * @param input - the frame source (the broker side of stdio).
 * @param output - the frame sink.
 * @param options - exit hook and ping cadence.
 * @returns the running runner's handles.
 */
export async function runRunner(
  modDir: string,
  input: NodeJS.ReadableStream,
  output: NodeJS.WritableStream,
  options: RunnerOptions = {},
): Promise<RunnerHandle> {
  const connection = new FrameConnection(input, output)
  // The manifests and the hooks module load before the handshake: a mod that
  // fails to load dies here, and the broker words the spawn failure.
  const manifest = readManifest(modDir)
  const imported = await importHooksModule(manifest)
  connection.start()

  // The deployment config arrives as the `loaded` call's result (the mod's
  // userConfig defaults ride the args); a broker that predates the call leaves
  // the defaults alone once the wait times out.
  const loaded = await Promise.race([
    connection.request({
      kind: 'call',
      ns: 'runner',
      method: 'loaded',
      args: { name: manifest.name, version: manifest.version, userConfig: imported.userConfig },
    }),
    new Promise<undefined>(resolve => { setTimeout(() => resolve(undefined), LOADED_TIMEOUT_MS).unref() }),
  ])
  const pushed = recordOf(loaded?.ok === true ? loaded.value : undefined, 'config')
  const merged = { ...imported.userConfig, ...pushed }

  const { mod, hooks } = await registerMod(
    {
      name: manifest.name,
      version: manifest.version,
      root: manifest.root,
      options: merged as PluginOptions,
      register: imported.register as ModRegister,
    },
    0,
  )
  const runtime = new HookRuntime({ connection, loaded: mod, hooks })
  connection.frameHandler = frame => { dispatch(runtime, frame, options.onExit ?? (() => {})) }
  connection.closeHandler = () => (options.onExit ?? (() => {}))(undefined)

  // The handshake: report the registrations and wait for the broker's `start`.
  const handshake = await connection.request({
    kind: 'call',
    ns: 'runner',
    method: 'hooks-registered',
    args: {
      hooks: hooks.map(hook => ({
        event: hook.event,
        ...(hook.matcher === undefined ? {} : { matcher: serializeMatcher(hook.matcher) }),
      })),
    },
  })
  if (!handshake.ok) console.error(`mod-runner: hooks-registered failed: ${handshake.message ?? 'unknown error'}`)

  console.error(describeRegistrations(mod, hooks))
  for (const event of unservedEvents(hooks.map(hook => hook.event))) {
    console.error(`mod not served: "${event}" is registered, but this host never raises that event`)
  }

  const pinger = setInterval(() => connection.notify('ping'), options.pingIntervalMs ?? PING_INTERVAL_MS)
  pinger.unref()
  return {
    connection,
    runtime,
    mod,
    servedEvents: SERVED_EVENTS,
    stop(): void {
      clearInterval(pinger)
    },
  }
}

/** Route one inbound frame to its handler; `shutdown` ends the process. */
function dispatch(runtime: HookRuntime, frame: Frame, onExit: (error: Error | undefined) => void): void {
  switch (frame.kind) {
    case 'event':
      runtime.handleEvent(frame)
      return
    case 'catch-call':
      runtime.handleCatchCall(frame)
      return
    case 'call': {
      // The only calls that reach a runner are presses and timer firings.
      if (frame['ns'] === 'ui' && frame['method'] === 'press') runtime.handlePressCall(frame)
      else if (frame['ns'] === 'clock' && frame['method'] === 'fire') runtime.handleFireCall(frame)
      else runtime.respondUnknownCall(frame)
      return
    }
    case 'notify':
      if (frame['method'] === 'hook-timeout') runtime.abandonHook((frame['args'] as Record<string, unknown> | undefined)?.['invocation'])
      else if (frame['method'] === 'shutdown') onExit(undefined)
      return
    case 'shutdown':
      onExit(undefined)
      return
    default:
      console.error(`mod-runner: a "${frame.kind}" frame arrived; dropped`)
  }
}

/** `{config}` out of a result value; anything else reads as no config. */
function recordOf(value: unknown, field: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null) return {}
  const inner = (value as Record<string, unknown>)[field]
  return typeof inner === 'object' && inner !== null ? inner as Record<string, unknown> : {}
}

/** The registered engine-event names this host never raises, each once, in registration order. */
function unservedEvents(events: readonly string[]): string[] {
  const names: string[] = []
  for (const event of events) {
    if (!ENGINE_EVENTS.has(event) || SERVED_EVENTS.has(event) || names.includes(event)) continue
    names.push(event)
  }
  return names
}

async function main(): Promise<void> {
  const testIndex = process.argv.indexOf('--test')
  if (testIndex !== -1) {
    const dir = process.argv[testIndex + 1]
    if (dir === undefined) {
      console.error('usage: mod-runner --test <dir>')
      process.exitCode = 2
      return
    }
    // Lazy: the broker-served path never pays for the test suite's module.
    const { exitCodeOf, renderReport, testMod } = await import('./test-mode.ts')
    try {
      const report = await testMod(dir)
      console.log(renderReport(report))
      process.exit(exitCodeOf(report))
    } catch (error) {
      console.error(`mod-runner --test failed: ${messageOf(error)}`)
      process.exit(1)
    }
    return
  }
  const modIndex = process.argv.indexOf('--mod')
  const modDir = modIndex !== -1 ? process.argv[modIndex + 1] : undefined
  if (modDir === undefined) {
    console.error('usage: mod-runner --mod <dir>')
    process.exitCode = 2
    return
  }
  // A supervised runner stays up through stray rejections; the broker judges
  // its health by the ping and restarts it when it dies.
  process.on('unhandledRejection', reason => {
    console.error('mod-runner: an unhandled rejection was dropped:', messageOf(reason))
  })
  runRunner(modDir, process.stdin, process.stdout, {
    onExit: error => {
      if (error !== undefined) console.error('mod-runner:', error.message)
      process.exit(error === undefined ? 0 : 1)
    },
  }).catch(error => {
    console.error(`mod-runner: mod not loaded: ${messageOf(error)}`)
    process.exit(1)
  })
}

// Run only when executed directly, not when the test suite imports this module.
if (process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href) main()
