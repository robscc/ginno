// The `$` shim: served methods frame their op, unserved members reject by
// name, `$` is read-only, fire-and-forget calls report failures, and the
// clock pauses around calls except `$.clock.sleep`.

import { describe, expect, test } from 'vitest'
import { createModsApi } from '../src/api.ts'
import type { ApiBinding } from '../src/api.ts'
import { BudgetClock } from '../src/clock.ts'

function makeBinding(overrides: Partial<ApiBinding> = {}): { binding: ApiBinding; calls: { op: string; input: unknown }[] } {
  const calls: { op: string; input: unknown }[] = []
  const base: ApiBinding = {
    mod: { name: 'sample', root: '/mods/sample' },
    clock: undefined,
    invoke: (op, input) => {
      calls.push({ op, input })
      return op === 'session.id' ? Promise.resolve('s-1') : Promise.resolve({})
    },
    timers: {
      after: () => ({ cancel: () => {} }),
      every: () => ({ cancel: () => {} }),
    },
    report: () => {},
    ...overrides,
  }
  return { binding: base, calls }
}

describe('served methods', () => {
  test('each method frames the op of the same name', async () => {
    const { binding, calls } = makeBinding()
    const $ = createModsApi(binding)
    expect($.plugin).toEqual({ name: 'sample', root: '/mods/sample' })
    await $.session.id()
    await $.state.set({ plugin: 'sample', key: 'k' }, 1)
    await $.fs.read('./x.py')
    await $.prompt.submit({ text: 'hi', asUser: true })
    expect(calls).toEqual([
      { op: 'session.id', input: {} },
      { op: 'state.set', input: { plugin: 'sample', key: 'k', value: 1 } },
      { op: 'fs.read', input: { path: './x.py', as: 'text' } },
      { op: 'prompt.submit', input: { text: 'hi', asUser: true } },
    ])
  })

  test('ui.log/toast/status are fire-and-forget and report failures', async () => {
    const reported: string[] = []
    const { binding } = makeBinding({
      invoke: () => Promise.reject(new Error('denied')),
      report: (line: string) => reported.push(line),
    })
    const $ = createModsApi(binding)
    $.ui.log('hello')
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(reported).toEqual(['sample: $.ui.log failed: denied'])
  })

  test('a failed awaited call rejects with the code attached', async () => {
    const failure = Object.assign(new Error('fs.write is not granted'), { code: 'denied' })
    const { binding } = makeBinding({ invoke: () => Promise.reject(failure) })
    const $ = createModsApi(binding)
    await expect($.fs.write('./x', 'y')).rejects.toMatchObject({ code: 'denied' })
  })

  test('state.get reads a missing slot as {value: undefined}, keeping DSH default destructuring', async () => {
    // The broker answers DSH's opCore shape `{value: stored|null}`; over JSON a missing slot's
    // inner is null (JSON has no undefined), and the shim hands back `{value: undefined}` so
    // `const { value = [] } = await $.state.get(ref)` works.
    const stored: Record<string, unknown> = { readings: [1, 2] }
    const { binding } = makeBinding({
      invoke: (op, input) => {
        if (op !== 'state.get') return Promise.resolve({})
        const key = (input as { key: string }).key
        return Promise.resolve({ value: stored[key] ?? null })
      },
    })
    const $ = createModsApi(binding)
    const { value = [] } = await $.state.get({ plugin: 'sample', key: 'absent' })
    expect(value).toEqual([])
    await expect($.state.get({ plugin: 'sample', key: 'absent' })).resolves.toEqual({ value: undefined })
    // A stored array passes through with its inner shape intact.
    await expect($.state.get({ plugin: 'sample', key: 'readings' })).resolves.toEqual({ value: [1, 2] })
  })
})

describe('unserved members', () => {
  test('an unimplemented member is a function that rejects naming the gap', async () => {
    const { binding } = makeBinding()
    const $ = createModsApi(binding)
    const namespaces = $ as unknown as { telemetry: { mark: () => Promise<unknown> }; settings: { read: () => Promise<unknown> } }
    await expect(namespaces.telemetry.mark()).rejects.toThrow('no implementation for telemetry.mark')
    await expect(namespaces.settings.read()).rejects.toThrow('no implementation for settings.read')
  })

  test('`$` is read-only', () => {
    const { binding } = makeBinding()
    const $ = createModsApi(binding)
    const target = $ as unknown as Record<string, unknown>
    expect(Reflect.set(target, 'fs', {})).toBe(false)
    expect(Reflect.defineProperty(target, 'fs', { value: {} })).toBe(false)
    expect(Reflect.deleteProperty(target, 'fs')).toBe(false)
    expect(target['fs']).not.toEqual({})
  })
})

describe('the clock binding', () => {
  test('an awaited call pauses the hook clock; $.clock.sleep does not', async () => {
    const clock = new BudgetClock(1_000)
    const { binding } = makeBinding({ clock })
    const $ = createModsApi(binding)
    clock.start()
    await $.session.id()
    const afterCall = clock.remainingMs
    await $.clock.sleep(50)
    const afterSleep = clock.remainingMs
    expect(afterSleep).toBeLessThan(afterCall)
    clock.stop()
  })
})
