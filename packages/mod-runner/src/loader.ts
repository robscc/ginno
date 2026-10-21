/**
 * Mod discovery: read the Claude Code plugin manifest (`.claude-plugin/plugin.json`)
 * and hooks manifest (`hooks/hooks.json`, first module), import the hooks
 * module as ESM (`.js`, `.mjs`, or `.ts` via Node's native type stripping),
 * and pull the `userConfig` defaults the module declares.
 * @module
 */

import { readFileSync } from 'node:fs'
import { resolve, dirname, extname } from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'
import * as nodeModule from 'node:module'
import { transform as sucraseTransform } from 'sucrase'
import { record } from './values.ts'
import { element as makeUiElement } from './elements.ts'

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

/** Hooks-module extensions this runner hosts: native (.ts strips in Node) and
 * the JSX set, which a sucrase-backed in-process loader hook transpiles. */
const PLAIN_MODULE_EXTS = new Set(['.js', '.mjs', '.ts'])
const JSX_MODULE_EXTS = new Set(['.tsx', '.jsx'])

type RegisterHooksFn = (hooks: {
  load?: (url: string, context: unknown, nextLoad: (url: string, context: unknown) => unknown) => unknown
}) => void

let jsxLoaderInstalled = false

/** Sniff the file's `@jsx X` / `@jsxFrag Y` pragma comments. Sucrase does not
 * read them, so the loader must pass them as options — and inject the same
 * names as globals (the host's job: Claude Code provides them to hooks
 * modules; next-steps declares `h` + `Fragment` and imports neither). */
function jsxPragmasOf(source: string): { pragma: string; fragment: string } {
  const pragma = /(?:^|\n)\s*\/\*\s*@jsx\s+([\w.$]+)\s*\*\//.exec(source)?.[1] ?? 'React'
  const fragment = /(?:^|\n)\s*\/\*\s*@jsxFrag\s+([\w.$]+)\s*\*\//.exec(source)?.[1] ?? 'Fragment'
  return { pragma, fragment }
}

/** Classic createElement over this runner's element system: a function type
 * (what `$.ui.resolve(e)` hands a hook — `Box`, `Text`, `Button`) is CALLED,
 * so its branded/frozen element comes back; a string type rides the same
 * factory. Children fold into `props.children` exactly as the constructors
 * expect (single child unwrapped, none omitted). */
function installJsxGlobals(pragma: string, fragment: string): void {
  const g = globalThis as Record<string, unknown>
  const createElement = (type: unknown, config: Record<string, unknown> | null, ...children: unknown[]) => {
    const props = {
      ...(config ?? {}),
      ...(children.length > 0
        ? { children: children.length === 1 ? children[0] : children }
        : {}),
    }
    return typeof type === 'function'
      ? (type as (props: Record<string, unknown>) => unknown)(props)
      : makeUiElement(type as never, props as never)
  }
  const Fragment = 'Fragment'
  if (pragma === 'React') {
    g.React ??= { createElement, Fragment }
  } else {
    g[pragma] ??= createElement
  }
  if (fragment !== 'React') g[fragment] ??= Fragment
}

/**
 * Transpile this mod's .tsx/.jsx hooks modules on import (sucrase:
 * typescript + jsx, classic createElement — the JSX function comes from the
 * file's own `@jsx` pragma comment, e.g. next-steps' local `h`). Scoped to
 * the mod's root: anything outside it (host, node_modules) is untouched, and
 * an out-of-scope .tsx keeps Node's native "unknown extension" error.
 *
 * One runner process hosts one mod, so a single root scope per install is
 * exact. Uses Node's synchronous `module.registerHooks` (≥22.15; the engines
 * field pins ≥22.18), so this works from the single-file esbuild bundle —
 * no second hooks file to ship. 2026-10-21: claude-plugins-community ships
 * .tsx hooks modules (next-steps).
 */
export function ensureJsxLoader(modRoot: string): void {
  if (jsxLoaderInstalled) return
  const registerHooks = (nodeModule as unknown as { registerHooks?: RegisterHooksFn }).registerHooks
  if (typeof registerHooks !== 'function') {
    throw new Error(
      'mod not loaded: .tsx/.jsx hooks modules need module.registerHooks (Node ≥22.15); ' +
        `this Node lacks it (${process.version})`,
    )
  }
  const rootHref = pathToFileURL(resolve(modRoot)).href
  registerHooks({
    load(url, context, nextLoad) {
      let path = url
      try {
        path = fileURLToPath(url)
      } catch {
        // non-file URL (data:, node:) — not ours to touch
      }
      if (!url.startsWith(rootHref) || !JSX_MODULE_EXTS.has(extname(path))) {
        return nextLoad(url, context)
      }
      const source = readFileSync(path, 'utf8')
      const { pragma, fragment } = jsxPragmasOf(source)
      const { code } = sucraseTransform(source, {
        transforms: ['typescript', 'jsx'],
        filePath: url,
        production: true,
        jsxPragma: pragma,
        jsxFragmentPragma: fragment,
      })
      return { format: 'module', shortCircuit: true, source: code }
    },
  })
  jsxLoaderInstalled = true
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
  if (!PLAIN_MODULE_EXTS.has(extension) && !JSX_MODULE_EXTS.has(extension)) {
    throw new Error(`mod not loaded: the hooks module must be .js, .mjs, .ts, .tsx, or .jsx; got ${modulePath}`)
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
  // The JSX set rides the transpile hook (see ensureJsxLoader); .ts/.js/.mjs
  // import natively (Node type stripping). The pragma globals install before
  // the import: the module's JSX expressions reference them at call time.
  if (JSX_MODULE_EXTS.has(extname(manifest.modulePath))) {
    ensureJsxLoader(manifest.root)
    const { pragma, fragment } = jsxPragmasOf(readFileSync(manifest.modulePath, 'utf8'))
    installJsxGlobals(pragma, fragment)
  }
  const imported: Record<string, unknown> = await import(pathToFileURL(manifest.modulePath).href)
  // A plain module exports `register`; a `defineMod` module default-exports the spec that holds it.
  const spec = record(imported['default'])
  const register = imported['register'] ?? spec['register']
  if (typeof register !== 'function') {
    throw new Error(`mod not loaded: the hooks module exports no register(on, options) function`)
  }
  return { register, userConfig: extractUserConfig(imported) }
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
