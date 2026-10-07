/**
 * Small value helpers shared by the runner: reading a mod's untyped call
 * input, wording a thrown value, and JSON encoding as typed by what it does.
 * Adapted from deepseek-harness (MIT), Copyright (c) 2026 DeepSeek. @license MIT
 * @module
 */

/**
 * The message of a thrown value: an Error's `message`, anything else as text.
 * @param error - the thrown value.
 * @returns the message text.
 */
export function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * A mod's call input as a field record; anything that is not an object reads as empty.
 * @param input - the value a mod passed.
 * @returns the fields, or an empty record.
 */
export function record(input: unknown): Record<string, unknown> {
  return typeof input === 'object' && input !== null ? input as Record<string, unknown> : {}
}

/**
 * Require one string field of a mod's call input.
 * @param value - the field value.
 * @param what - the field, named in the error.
 * @returns the string.
 * @throws TypeError when the value is not a string.
 */
export function requireString(value: unknown, what: string): string {
  if (typeof value !== 'string') throw new TypeError(`${what} must be a string`)
  return value
}

/**
 * `JSON.stringify` as typed by what it does: undefined for a value JSON cannot carry.
 * @param value - the value to encode.
 * @returns the JSON text, or undefined.
 */
export function stringify(value: unknown): string | undefined {
  const encoded: string | undefined = JSON.stringify(value)
  return encoded
}

/**
 * Freeze a value in place at every depth, as the mods API promises for event
 * inputs. (DSH reads this from `@deepseek-ai/dsh-util-values`; the runner has
 * no runtime dependencies, so it carries its own.)
 * @param value - the value to freeze.
 * @returns the same value, frozen.
 */
export function deepFreeze<T>(value: T): T {
  if (typeof value === 'object' && value !== null && !Object.isFrozen(value)) {
    Object.freeze(value)
    for (const child of Object.values(value as Record<string, unknown>)) deepFreeze(child)
  }
  return value
}
