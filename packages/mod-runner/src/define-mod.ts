/**
 * `defineMod`: wrap a mod's `register(on, options)` as the default-export
 * object the runner's loader reads (`userConfig` defaults, plugin identity).
 * The Cordis mounting DSH adds is host-side work here — the broker mounts the
 * mod from its manifests — so this wraps the spec and nothing more.
 * Adapted from deepseek-harness (MIT), Copyright (c) 2026 DeepSeek. @license MIT
 * @module
 */

import type { ModRegister, PluginOptions } from './types.ts'

/** What a mod's hooks module declares: the plugin identity and the `register`. */
export interface ModSpec {
  /** Plugin name, as `.claude-plugin/plugin.json` names it: letters, digits, `_` and `-`. */
  readonly name: string
  readonly version?: string
  /** Absolute directory the mod ships in, reported by `$.plugin.root`; defaults to the process cwd. */
  readonly root?: string
  /** `userConfig` defaults: the `options` `register` receives before the deployment overlays its config. */
  readonly userConfig?: PluginOptions
  readonly register: ModRegister
}

/** What `defineMod` produces: the default export a hooks module may ship. */
export interface DefinedMod {
  readonly name: string
  readonly version: string | undefined
  readonly root: string | undefined
  readonly userConfig: PluginOptions
  readonly register: ModRegister
}

/**
 * Wrap one mod's spec as the loader expects it.
 * @param spec - the mod's identity, `userConfig` defaults, and `register`.
 * @returns the frozen default-export object.
 */
export function defineMod(spec: ModSpec): DefinedMod {
  return Object.freeze({
    name: spec.name,
    version: spec.version,
    root: spec.root,
    userConfig: Object.freeze({ ...spec.userConfig }),
    register: spec.register,
  })
}
