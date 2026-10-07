// Event tables and the cross-process matcher serialization (design §3.2.1).

import { describe, expect, test } from 'vitest'
import { describeMatcher, isEventPattern, serializeMatcher, serializeMatcherValue } from '../src/matcher.ts'
import type { MatcherValue } from '../src/types.ts'

describe('isEventPattern', () => {
  test('exact names, `*`, and namespace globs pass', () => {
    expect(isEventPattern('tool.call')).toBe(true)
    expect(isEventPattern('*')).toBe(true)
    expect(isEventPattern('tool.*')).toBe(true)
    expect(isEventPattern('session.*')).toBe(true)
  })

  test('unknown names and empty globs fail', () => {
    expect(isEventPattern('tool.calll')).toBe(false)
    expect(isEventPattern('.*')).toBe(false)
    expect(isEventPattern('telemetry.*')).toBe(true)
  })

  test('classic.* hooks are refused', () => {
    expect(isEventPattern('classic.PreToolUse')).toBe(false)
    expect(isEventPattern('classic.*')).toBe(false)
  })
})

describe('serializeMatcherValue', () => {
  test('strings and numbers pass as-is', () => {
    expect(serializeMatcherValue('Bash')).toBe('Bash')
    expect(serializeMatcherValue(3)).toBe(3)
  })

  test('arrays keep members, RegExp members become {regexp, flags}', () => {
    expect(serializeMatcherValue(['Read', 'Write'])).toEqual(['Read', 'Write'])
    // A RegExp inside an array serializes like a top-level one; the declared member type is narrower than the wire format.
    const mixed = [/^Write/, 'Edit'] as unknown as MatcherValue
    expect(serializeMatcherValue(mixed)).toEqual([{ regexp: '^Write', flags: '' }, 'Edit'])
  })

  test('a RegExp becomes {regexp: source, flags: flags}', () => {
    expect(serializeMatcherValue(/^Bash.*$/gi)).toEqual({ regexp: '^Bash.*$', flags: 'gi' })
  })
})

describe('serializeMatcher', () => {
  test('no matcher serializes to undefined', () => {
    expect(serializeMatcher(undefined)).toBeUndefined()
  })

  test('each field serializes in place', () => {
    expect(serializeMatcher({ tool: /Bash/, kind: 'x' })).toEqual({ tool: { regexp: 'Bash', flags: '' }, kind: 'x' })
  })
})

describe('describeMatcher', () => {
  test('validate-style rendering', () => {
    expect(describeMatcher(undefined)).toBe('')
    expect(describeMatcher({ tool: /Write/ })).toBe('{tool=/Write/}')
    expect(describeMatcher({ tool: ['Read', 'Write'] })).toBe('{tool=Read|Write}')
    expect(describeMatcher({ tool: 'Read' })).toBe('{tool=Read}')
  })
})
