/**
 * Shared HTTP foundation for the sidecar client: same-origin base URL,
 * the JSON fetch helper (locale header), the WS base, and `health`.
 *
 * Every other ./api module imports its helpers from here; this module must
 * not import any of them back (no cycles).
 *
 * Split out of lib/runtime.ts, which re-exports every name unchanged.
 */
import { currentLocale } from "../../i18n/provider";

// The sidecar serves the static pages AND the JSON API from ONE origin (the API
// is namespaced under /api). So the client must call back to whatever origin
// served the page — never a baked-in port — otherwise a page opened on a
// non-default port (e.g. a dev sidecar on 8797) would fetch the wrong sidecar.
// NEXT_PUBLIC_RUNTIME_PORT remains an opt-in override for the rare split-origin
// dev setup (web :3000 talking to a sidecar elsewhere).
//
// Reference the env var DIRECTLY, no `typeof process` guard: Next statically
// inlines NEXT_PUBLIC_* into a string literal at compile time, and a runtime
// guard can silently discard that literal when a chunk executes without the
// process shim — exactly the trap that had split-origin dev 404ing to
// same-origin despite the port being correctly inlined (2026-09-25).
export const OVERRIDE_PORT = process.env.NEXT_PUBLIC_RUNTIME_PORT;

function sameOriginBase(): string {
  if (typeof window !== "undefined") {
    return OVERRIDE_PORT
      ? `${window.location.protocol}//${window.location.hostname}:${OVERRIDE_PORT}/api`
      : `${window.location.origin}/api`;
  }
  return `http://127.0.0.1:${OVERRIDE_PORT ?? 8787}/api`; // SSR/build fallback
}

export const BASE = sameOriginBase();

export function wsBase(): string {
  if (typeof window !== "undefined") {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = OVERRIDE_PORT
      ? `${window.location.hostname}:${OVERRIDE_PORT}`
      : window.location.host;
    return `${proto}//${host}/api/ws/sessions`;
  }
  return `ws://127.0.0.1:${OVERRIDE_PORT ?? 8787}/api/ws/sessions`;
}

// 每个请求下发已解析 locale（X-Ginno-Language）：runtime 的
// RequestLocaleMiddleware 读头绑定 contextvar，非法/缺失回落 settings 解析。
export const H = { "Content-Type": "application/json" };

export async function json<T>(input: string | URL | Request, init?: RequestInit): Promise<T> {
  const r = await fetch(input, { ...init, headers: { "X-Ginno-Language": currentLocale(), ...(init?.headers ?? {}) } });
  return (await r.json()) as T;
}

export async function health() {
  return json<{ ok: boolean; version: string }>(`${BASE}/health`);
}
