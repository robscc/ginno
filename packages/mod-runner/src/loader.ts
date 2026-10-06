/**
 * Mod discovery: read the Claude Code plugin manifest (`.claude-plugin/plugin.json`)
 * and hooks manifest (`hooks/hooks.json`, first module), import the hooks
 * module as ESM (`.js`, `.mjs`, or `.ts` via Node's native type stripping),
 * and pull the `userConfig` defaults the module declares.
 * @module
 */

import { readFileSync } from 'node:fs'
import { resolve, dirname, extname } from 'node:path'
import { pathToFileURL } from 'node:url'
import { record } from './values.ts'

/** What the manifests name: the plugin identity and the hooks module to import. */
export interface LoadedManifest {
  /** Plugin name from `plugin.json`; the `plugin` of `$.state` refs and tool prefixes. */
  readonly name: string
  readonly version: string | undefined
  /** Absolute directory the mod ships in. */
  readonly root: string
  /** Absolute path of the hooks module. */
  readonly modulePath: string
}

/** A hooks module as the runner sees it after import: a `register`, plus optional config defaults. */
export interface ImportedHooksModule {
  readonly register: unknown
  readonly userConfig: Record<string, unknown>
}

function readJson(path: string, what: string): Record<string, unknown> {
  let text: string
  try {
    text = readFileSync(path, 'utf8')
  } catch (error: unknown) {
    throw new Error(`mod not loaded: cannot read ${what} at ${path}: ${error instanceof Error ? error.message : String(error)}`)
  }
  try {
    const parsed: unknown = JSON.parse(text)
    return record(parsed)
  } catch (error: unknown) {
    throw new Error(`mod not loaded: ${what} at ${path} is not valid JSON: ${error instanceof Error ? error.message : String(error)}`)
  }
}

/**
 * Read a mod directory's manifests and resolve its hooks module.
 * @param dir - the mod directory (`--mod`).
 * @returns the plugin identity and the hooks module path.
 */
export function readManifest(dir: string): LoadedManifest {
  const root = resolve(dir)
  const pluginJson = readJson(resolve(root, '.claude-plugin', 'plugin.json'), '.claude-plugin/plugin.json')
  const hooksJson = readJson(resolve(root, 'hooks', 'hooks.json'), 'hooks/hooks.json')
  const modules = hooksJson['modules']
  if (!Array.isArray(modules) || typeof modules[0] !== 'string') {
    throw new Error(`mod not loaded: hooks/hooks.json needs a "modules" array whose first entry names the hooks module`)
  }
  // Only the first module loads, as Claude Code reads the list; the rest are a warning, not an error.
  if (modules.length > 1) console.error(`mod not loaded fully: hooks.json lists ${modules.length} modules; this host loads the first only`)
  const name = pluginJson['name']
  if (typeof name !== 'string') throw new Error('mod not loaded: .claude-plugin/plugin.json needs a string "name"')
  const version = typeof pluginJson['version'] === 'string' ? pluginJson['version'] as string : undefined
  const modulePath = resolve(dirname(resolve(root, 'hooks', 'hooks.json')), modules[0] as string)
  const extension = extname(modulePath)
  if (extension !== '.js' && extension !== '.mjs' && extension !== '.ts') {
    throw new Error(`mod not loaded: the hooks module must be .js, .mjs, or .ts; got ${modulePath}`)
  }
  return { name, version, root, modulePath }
}

/**
 * Import the hooks module and check what it exports. TypeScript modules load
 * through Node's native type stripping (Node ≥ 22.18), so a mod ships its
 * `.ts` source directly.
 * @param manifest - what {@link readManifest} read.
 * @returns the module's `register` and its `userConfig` defaults.
 */
export async function importHooksModule(manifest: LoadedManifest): Promise<ImportedHooksModule> {
  const imported: Record<string, unknown> = await import(pathToFileURL(manifest.modulePath).href)
  if (typeof imported['register'] !== 'function') {
    throw new Error(`mod not loaded: the hooks module exports no register(on, options) function`)
  }
  return { register: imported['register'], userConfig: extractUserConfig(imported) }
}

/**
 * The `userConfig` defaults a hooks module declares: the default export's
 * `userConfig` when it has one (a `defineMod` spec), else the options of its
 * wrapped `definition` (a DSH-style plugin object), else nothing.
 * @param imported - the module namespace.
 * @returns the defaults, as a plain record.
 */
export function extractUserConfig(imported: Record<string, unknown>): Record<string, unknown> {
  const spec = record(imported['default'])
  if (Object.keys(record(spec['userConfig'])).length > 0) return { ...record(spec['userConfig']) }
  const definition = record(spec['definition'])
  return { ...record(definition['options']) }
}
