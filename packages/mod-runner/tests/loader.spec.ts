// Manifest reading, module import, and the userConfig extraction.

import { describe, expect, test } from 'vitest'
import { fileURLToPath } from 'node:url'
import { extractUserConfig, importHooksModule, readManifest } from '../src/loader.ts'

const SAMPLE = fileURLToPath(new URL('./fixtures/sample-mod', import.meta.url))

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
