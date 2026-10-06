/**
 * Mod registration: run a mod's `register(on, options)` and keep every
 * `on(...)` registration in one ordered list the runner reports to the
 * broker. Selection and ordering across mods live in the broker; the runner
 * holds the validations Claude Code does at register time.
 * Adapted from deepseek-harness (MIT), Copyright (c) 2026 DeepSeek. @license MIT
 * @module
 */

import { messageOf } from './values.ts'
import { describeMatcher, isEventPattern } from './matcher.ts'
import type { AnyHook, HookMatcher, ModDefinition, ModOn, HookRegistration, PluginOptions } from './types.ts'

/** Plugin names Claude Code accepts: letters, digits, `_` and `-`. */
const PLUGIN_NAME = /^[A-Za-z0-9_-]{1,64}$/u

/** One loaded mod: its plugin identity and the `register` options it received. */
export interface LoadedMod {
  readonly name: string
  readonly version: string | undefined
  /** Absolute directory the mod ships in, as `$.plugin.root` reports it. */
  readonly root: string
  readonly options: PluginOptions
  /** Load order; a single runner hosts one mod, so the order is always 0. */
  readonly order: number
}

/** One `on(...)` registration. */
export interface RegisteredHook {
  readonly mod: LoadedMod
  readonly event: string
  readonly matcher: HookMatcher | undefined
  readonly hook: AnyHook
  catchHandler: AnyHook | undefined
  /** Failure kinds already reported for this hook; one line per kind until the mod reloads. */
  readonly reported: Set<string>
}

function isMatcher(value: unknown): value is HookMatcher {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * Build the `on` function one `register` call receives and collect its
 * registrations. Validation follows Claude Code: the event name must be a
 * known name or glob, and one event may be registered without a matcher only
 * once.
 * @param mod - the mod registering.
 * @returns `on` and the list it appends to.
 */
export function createOn(mod: LoadedMod): { on: ModOn; hooks: RegisteredHook[] } {
  const hooks: RegisteredHook[] = []
  const unmatched = new Set<string>()
  const on = ((event: unknown, matcherOrHook: unknown, maybeHook?: unknown): HookRegistration => {
    if (typeof event !== 'string') throw new TypeError(`${mod.name}: the event name passed to on() is not a string literal`)
    if (!isEventPattern(event)) throw new Error(`${mod.name}: "${event}" is not an event`)
    const hook = maybeHook ?? matcherOrHook
    const matcher = maybeHook === undefined ? undefined : matcherOrHook
    if (typeof hook !== 'function') throw new TypeError(`${mod.name}: on("${event}") needs a hook function`)
    if (matcher !== undefined && !isMatcher(matcher)) throw new TypeError(`${mod.name}: on("${event}") matcher must be an object`)
    if (matcher === undefined) {
      if (unmatched.has(event)) throw new Error(`${mod.name}: on("${event}") is registered twice without a matcher`)
      unmatched.add(event)
    }
    const registered: RegisteredHook = {
      mod, event, matcher, hook: hook as AnyHook, catchHandler: undefined, reported: new Set(),
    }
    hooks.push(registered)
    return {
      catch(handler: AnyHook): void {
        if (typeof handler !== 'function') throw new TypeError(`${mod.name}: on("${event}").catch needs a handler function`)
        registered.catchHandler = handler
      },
    }
  }) as ModOn
  return { on, hooks }
}

/**
 * Run one mod's `register` and collect its hooks. A `register` that throws
 * fails the mod with Claude Code's wording; the caller decides whether the
 * session continues without it.
 * @param definition - the mod as its plugin defined it.
 * @param order - the mod's position in the chain; earlier mods run outside later ones.
 * @returns the loaded mod and its registrations.
 */
export async function registerMod(definition: ModDefinition, order: number): Promise<{ mod: LoadedMod; hooks: RegisteredHook[] }> {
  if (!PLUGIN_NAME.test(definition.name)) {
    throw new Error(`mod "${definition.name}" not loaded: a plugin name uses letters, digits, _ and - only`)
  }
  const mod: LoadedMod = Object.freeze({
    name: definition.name,
    version: definition.version,
    root: definition.root ?? process.cwd(),
    options: Object.freeze({ ...definition.options }),
    order,
  })
  const { on, hooks } = createOn(mod)
  try {
    await definition.register(on, mod.options)
  } catch (error: unknown) {
    throw new Error(`${definition.name}: hooks module did not load: register threw ${messageOf(error)}`, { cause: error })
  }
  return { mod, hooks }
}

/**
 * The `claude plugin validate`-style line for a loaded mod's registrations,
 * for the runner's load log on stderr.
 * @param mod - the loaded mod.
 * @param hooks - its registrations in `on` order.
 * @returns `events: turn.start, tool.call{tool=/^Write/}`, or `events: (none)`.
 */
export function describeRegistrations(mod: LoadedMod, hooks: readonly RegisteredHook[]): string {
  const events = hooks.map(hook => `${hook.event}${describeMatcher(hook.matcher)}`).join(', ')
  return `hooks module ${mod.name} loaded (tier user); events: ${events.length > 0 ? events : '(none)'}`
}
