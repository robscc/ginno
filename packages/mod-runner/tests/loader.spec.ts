// Manifest reading, module import, and the userConfig extraction.

import { describe, expect, test } from 'vitest'
import { fileURLToPath } from 'node:url'
import { extractConfigSchema, extractUserConfig, importHooksModule, readManifest } from '../src/loader.ts'
import { defineMod } from '../src/define-mod.ts'

const SAMPLE = fileURLToPath(new URL('./fixtures/sample-mod', import.meta.url))
const SCHEMA_MOD = fileURLToPath(new URL('./fixtures/schema-mod', import.meta.url))

describe('readManifest', () => {
  test('reads the plugin and hooks manifests, resolving the first module', () => {
    const manifest = readManifest(SAMPLE)
    expect(manifest.name).toBe('sample-mod')
    expect(manifest.version).toBe('0.1.0')
    expect(manifest.root).toBe(SAMPLE)
    expect(manifest.modulePath).toContain('hooks/sample.ts')
  })

  test('a missing plugin.json fails with its path named', () => {
    expect(() => readManifest('/definitely/not/there')).toThrow(/plugin\.json/)
  })
})

describe('importHooksModule', () => {
  test('imports the .ts hooks module via type stripping', async () => {
    const manifest = readManifest(SAMPLE)
    const imported = await importHooksModule(manifest)
    expect(imported.register).toBeTypeOf('function')
    expect(imported.userConfig).toEqual({})
  })
})

describe('extractUserConfig', () => {
  test('reads a defineMod default export', () => {
    expect(extractUserConfig({ default: { userConfig: { units: 'metric', limit: 3 } } })).toEqual({ units: 'metric', limit: 3 })
  })

  test('falls back to a DSH-style definition\'s options', () => {
    expect(extractUserConfig({ default: { definition: { options: { x: 'y' } } } })).toEqual({ x: 'y' })
  })

  test('a plain module yields nothing', () => {
    expect(extractUserConfig({})).toEqual({})
  })
})

describe('configSchema', () => {
  const schema = {
    type: 'object' as const,
    properties: {
      units: {
        type: 'string' as const,
        enum: ['metric', 'imperial'],
        default: 'metric',
        'x-ginno-group': 'Display',
      },
    },
  }

  test('defineMod stores configSchema on the spec', () => {
    const defined = defineMod({ name: 'schema-mod', configSchema: schema, register: () => {} })
    expect(defined.configSchema).toEqual(schema)
  })

  test('the loader exposes it, and importHooksModule reports it', async () => {
    expect(extractConfigSchema({ default: { configSchema: schema } })).toEqual(schema)
    const imported = await importHooksModule(readManifest(SCHEMA_MOD))
    expect(imported.configSchema).toEqual(schemaWithDefaults())
    expect(imported.userConfig).toEqual({ units: 'metric', limit: 3 })
  })

  test('a plain module (or a non-object value) yields none', () => {
    expect(extractConfigSchema({})).toBeUndefined()
    expect(extractConfigSchema({ default: { configSchema: 'nope' } })).toBeUndefined()
  })
})

/** The fixture's full schema, with the fields the compact test schema omits. */
function schemaWithDefaults(): unknown {
  return {
    type: 'object',
    properties: {
      units: {
        type: 'string',
        enum: ['metric', 'imperial'],
        title: 'Units',
        description: 'Display units',
        default: 'metric',
        'x-ginno-group': 'Display',
        'x-ginno-order': 10,
      },
      apiKey: { type: 'string', default: '', 'x-ginno-secret': true },
      tags: { type: 'array', items: { type: 'string' }, default: [] },
    },
  }
}
