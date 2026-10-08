/**
 * Event-name patterns: which event names `on` accepts. Event names are Claude
 * Code's; a hook on a name this host never raises registers fine and never
 * runs. Matcher *evaluation* lives in the broker (the runner holds no policy,
 * and one matcher implementation avoids drift); only the name table and the
 * validate-style rendering are kept here.
 * Adapted from deepseek-harness (MIT), Copyright (c) 2026 DeepSeek. @license MIT
 * @module
 */

import type { HookMatcher, MatcherValue } from './types.ts'

/** The events the engine raises on its own, as opposed to the `<namespace>.<method>` events a mods API call raises. */
export const ENGINE_EVENTS: ReadonlySet<string> = new Set([
  'tool.call', 'tool.check', 'tool.describe',
  'prompt.submit', 'prompt.fill', 'prompt.suggest', 'prompt.edit', 'prompt.compose', 'prompt.section',
  'prompt.context', 'prompt.attachment', 'skill.prompt', 'attribution.text',
  'command.run', 'command.describe', 'config.set', 'config.describe',
  'turn.start', 'turn.step', 'turn.complete',
  'session.start', 'session.end', 'session.compact', 'session.receive', 'session.send', 'session.append',
  'session.attach', 'session.detach', 'session.measure',
  'agent.offer', 'agent.spawn',
  'ui.render', 'ui.resolve', 'ui.press', 'ui.input', 'ui.select', 'ui.focus', 'ui.scroll', 'ui.close', 'ui.message',
  'plugin.register', 'engine.create',
  'telemetry.log', 'telemetry.mark',
])

/**
 * Every event name a mod may hook, in Claude Code's reference order: engine
 * events, then every mods API method as `<namespace>.<method>`. `on` refuses a
 * name outside this list with Claude Code's own wording, so a misspelling
 * fails at load rather than registering a hook that never runs.
 */
export const KNOWN_EVENTS: ReadonlySet<string> = new Set([
  ...ENGINE_EVENTS,
  // mods API calls
  'ui.log', 'ui.toast', 'ui.status', 'ui.notice', 'ui.invalidate', 'ui.open', 'ui.panes', 'ui.blit', 'ui.ask', 'ui.copy',
  'command.register', 'command.list',
  'tool.register', 'tool.list',
  'agent.register', 'agent.list',
  'model.complete', 'model.fork', 'model.classify',
  'prompt.read',
  'turn.abort',
  'session.messages', 'session.cwd', 'session.root', 'session.model', 'session.turns', 'session.id', 'session.repo',
  'session.surface', 'session.surfaces', 'session.usage', 'session.version', 'session.authorize',
  'config.list',
  'settings.read',
  'env.get', 'env.set',
  'fs.read', 'fs.write', 'fs.list', 'fs.exists', 'fs.stat', 'fs.ancestors',
  'store.get', 'store.set', 'store.delete', 'store.keys',
  'state.get', 'state.set',
  'clock.now', 'clock.sleep', 'clock.after', 'clock.every',
  'http.fetch',
  'process.run', 'process.spawn',
  'mcp.call', 'mcp.connect',
  'audio.play', 'audio.speak',
])

/**
 * Whether a name passed to `on` is an exact event name, `*`, or a `<namespace>.*` glob.
 * @param pattern - the name or glob.
 * @returns true when `on` accepts it.
 */
export function isEventPattern(pattern: string): boolean {
  if (pattern === '*') return true
  if (pattern.endsWith('.*')) {
    const namespace = pattern.slice(0, -2)
    return namespace.length > 0 && [...KNOWN_EVENTS].some(event => event.startsWith(`${namespace}.`))
  }
  return KNOWN_EVENTS.has(pattern)
}

/** Cross-process matcher value: a RegExp becomes `{"regexp", "flags"}`; scalars and arrays pass as-is. */
export function serializeMatcherValue(value: MatcherValue): unknown {
  if (value instanceof RegExp) return { regexp: value.source, flags: value.flags }
  if (Array.isArray(value)) return value.map(candidate => serializeMatcherValue(candidate))
  return value
}

/**
 * A matcher as the `hooks-registered` frame carries it; the broker rebuilds
 * and evaluates it on its side of the wire.
 * @param matcher - the matcher passed to `on`, or undefined.
 * @returns the serialized fields, or undefined for a hook without one.
 */
export function serializeMatcher(matcher: HookMatcher | undefined): Record<string, unknown> | undefined {
  if (matcher === undefined) return undefined
  const fields: Record<string, unknown> = {}
  for (const [field, value] of Object.entries(matcher)) fields[field] = serializeMatcherValue(value)
  return fields
}

/**
 * Render a matcher the way `claude plugin validate` prints it.
 * @param matcher - the matcher, or undefined for a hook without one.
 * @returns `{field=value,...}`, or an empty string.
 */
export function describeMatcher(matcher: HookMatcher | undefined): string {
  if (matcher === undefined) return ''
  const fields = Object.entries(matcher).map(([field, value]) => {
    const rendered = value instanceof RegExp ? String(value) : Array.isArray(value) ? value.join('|') : String(value)
    return `${field}=${rendered}`
  })
  return `{${fields.join(',')}}`
}
