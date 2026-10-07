// The `on` validations Claude Code does at register time.

import { describe, expect, test } from 'vitest'
import { describeRegistrations, registerMod } from '../src/module.ts'
import type { ModDefinition } from '../src/types.ts'

const definition = (name = 'sample'): ModDefinition => ({
  name,
  root: '/mods/sample',
  options: { units: 'metric' },
  register: () => {},
})

describe('createOn', () => {
  test('a known event with matcher or without registers', async () => {
    const { mod, hooks } = await registerMod({
      ...definition(),
      register: on => {
        on('tool.call', () => {})
        on('tool.call', { tool: 'Bash' }, () => {})
      },
    }, 0)
    expect(mod.name).toBe('sample')
    expect(hooks.map(hook => hook.event)).toEqual(['tool.call', 'tool.call'])
  })

  test('an unknown event name fails with Claude Code\'s wording', async () => {
    await expect(registerMod({
      ...definition(),
      register: on => on('tool.calll', () => {}),
    }, 0)).rejects.toThrow('"tool.calll" is not an event')
  })

  test('classic.* names are refused at register time', async () => {
    await expect(registerMod({
      ...definition(),
      register: on => on('classic.PreToolUse', () => {}),
    }, 0)).rejects.toThrow('"classic.PreToolUse" is not an event')
  })

  test('a bare registration of one event happens once only', async () => {
    await expect(registerMod({
      ...definition(),
      register: on => {
        on('session.start', () => {})
        on('session.start', () => {})
      },
    }, 0)).rejects.toThrow('registered twice without a matcher')
  })

  test('a non-function hook and a non-object matcher fail, wrapped as load errors', async () => {
    await expect(registerMod({
      ...definition(),
      register: on => on('session.start', 'nope' as unknown as never),
    }, 0)).rejects.toThrow('needs a hook function')
    await expect(registerMod({
      ...definition(),
      register: on => on('session.start', 4 as unknown as never, () => {}),
    }, 0)).rejects.toThrow('matcher must be an object')
  })

  test('.catch attaches to the registration', async () => {
    const { hooks } = await registerMod({
      ...definition(),
      register: on => {
        const hook = on('tool.call', () => {
          throw new Error('x')
        })
        hook.catch(() => ({}))
      },
    }, 0)
    expect(hooks[0]?.catchHandler).toBeTypeOf('function')
  })
})

describe('registerMod', () => {
  test('a bad plugin name fails', async () => {
    await expect(registerMod({ ...definition('bad name!'), register: () => {} }, 0))
      .rejects.toThrow('letters, digits, _ and - only')
  })

  test('a throwing register is wrapped in Claude Code\'s wording', async () => {
    await expect(registerMod({
      ...definition(),
      register: () => {
        throw new Error('cannot read config')
      },
    }, 0)).rejects.toThrow('hooks module did not load: register threw cannot read config')
  })

  test('register receives frozen options', async () => {
    let seen: unknown
    await registerMod({
      ...definition(),
      register: (on, options) => { seen = options },
    }, 0)
    expect(seen).toEqual({ units: 'metric' })
  })

  test('describeRegistrations renders the validate line', async () => {
    const { mod, hooks } = await registerMod({
      ...definition(),
      register: on => on('tool.call', { tool: /^Write/ }, () => {}),
    }, 0)
    expect(describeRegistrations(mod, hooks)).toBe('hooks module sample loaded (tier user); events: tool.call{tool=/^Write/}')
  })
})
