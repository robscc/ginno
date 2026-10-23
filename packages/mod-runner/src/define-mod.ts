/**
 * `defineMod`: wrap a mod's `register(on, options)` as the default-export
 * object the runner's loader reads (`userConfig` defaults, plugin identity).
 * The Cordis mounting DSH adds is host-side work here — the broker mounts the
 * mod from its manifests — so this wraps the spec and nothing more.
 * Adapted from deepseek-harness (MIT), Copyright (c) 2026 DeepSeek. @license MIT
 * @module
 */

import type { ModRegister, PluginOptions } from './types.ts'

/**
 * One property of a mod's `configSchema`: the JSON-Schema subset the settings
 * editor renders, plus the `x-ginno-*` extensions it reads for grouping,
 * ordering, and secret masking.
 */
export interface ConfigSchemaProperty {
  readonly type?: 'string' | 'number' | 'integer' | 'boolean' | 'array' | 'object'
  readonly title?: string
  readonly description?: string
  /** The value the editor offers as "reset to default"; mirrors `userConfig`. */
  readonly default?: unknown
  readonly enum?: readonly unknown[]
  /** Array items (one level is enough for the editor's tags/number-list controls). */
  readonly items?: ConfigSchemaProperty
  /** Nested object fields. */
  readonly properties?: Readonly<Record<string, ConfigSchemaProperty>>
  readonly required?: readonly string[]
  readonly 'x-ginno-group'?: string
  readonly 'x-ginno-order'?: number
  readonly 'x-ginno-secret'?: boolean
  readonly [extension: `x-ginno-${string}`]: unknown
}

/**
 * A mod's optional config descriptor: the JSON-Schema subset `defineMod` may
 * declare so the host can render a form without reverse-engineering defaults.
 * Unknown keys pass through untouched (hosts ignore what they do not know).
 */
export interface ConfigSchema {
  readonly type?: 'object'
  readonly title?: string
  readonly description?: string
  readonly properties?: Readonly<Record<string, ConfigSchemaProperty>>
  readonly required?: readonly string[]
  readonly [extension: `x-ginno-${string}`]: unknown
}

/** What a mod's hooks module declares: the plugin identity and the `register`. */
export interface ModSpec {
  /** Plugin name, as `.claude-plugin/plugin.json` names it: letters, digits, `_` and `-`. */
  readonly name: string
  readonly version?: string
  /** Absolute directory the mod ships in, reported by `$.plugin.root`; defaults to the process cwd. */
  readonly root?: string
  /** `userConfig` defaults: the `options` `register` receives before the deployment overlays its config. */
  readonly userConfig?: PluginOptions
  /** Optional config descriptor the host reports alongside the defaults. */
  readonly configSchema?: ConfigSchema
  readonly register: ModRegister
}

/** What `defineMod` produces: the default export a hooks module may ship. */
export interface DefinedMod {
  readonly name: string
  readonly version: string | undefined
  readonly root: string | undefined
  readonly userConfig: PluginOptions
  readonly configSchema: ConfigSchema | undefined
  readonly register: ModRegister
}

/**
 * Wrap one mod's spec as the loader expects it.
 * @param spec - the mod's identity, `userConfig` defaults, optional config schema, and `register`.
 * @returns the frozen default-export object.
 */
export function defineMod(spec: ModSpec): DefinedMod {
  return Object.freeze({
    name: spec.name,
    version: spec.version,
    root: spec.root,
    userConfig: Object.freeze({ ...spec.userConfig }),
    configSchema: spec.configSchema,
    register: spec.register,
  })
}
