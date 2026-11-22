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
  const body = (await r.json()) as T;
  // 损坏降级闸门（settings_gate.py）的 503 体带 settings_corrupt_degraded
  // 标记。绝不把它当业务文档返回——否则 AppShell 会对错误体 .map 白屏，
  // 恢复横幅永远渲染不出来。抛错后各加载器走与「runtime 不可达」相同的
  // catch/降级路径，壳存活、横幅可见（test_corrupt_recovery_banner_ui）。
  if (
    body &&
    typeof body === "object" &&
    (body as { error?: unknown }).error === "settings_corrupt_degraded"
  ) {
    const err = new Error("settings corrupt: runtime is in recovery-only mode");
    (err as Error & { ginnoStatus?: number }).ginnoStatus = 503;
    throw err;
  }
  return body;
}

// 严格版：非 2xx 直接抛错，绝不把错误体（如 FastAPI 的 {"detail": ...}）当业务文档返回。
// 读改写型调用方（设置面板等）必须用这个，否则服务端故障窗口内会把错误体整份写回。
// 抛出的 Error 挂 ginnoStatus（HTTP 状态码）供调用方细分结局——如 settings.json
// 损坏时后端 409，通知面板据此显示「保存被拒：文件损坏」而非笼统的保存失败。
export async function jsonStrict<T>(input: string | URL | Request, init?: RequestInit): Promise<T> {
  const r = await fetch(input, { ...init, headers: { "X-Ginno-Language": currentLocale(), ...(init?.headers ?? {}) } });
  // 测试桩可能不带 status；真实 fetch 恒有。仅在明确非 2xx 时抛错。
  if (r.status !== undefined && !r.ok) {
    let detail = "";
    try {
      detail = ((await r.json()) as { detail?: string })?.detail ?? "";
    } catch {
      /* 非 JSON 错误体 */
    }
    const err = new Error(detail || `HTTP ${r.status}`);
    (err as Error & { ginnoStatus?: number }).ginnoStatus = r.status;
    throw err;
  }
  return (await r.json()) as T;
}

export async function health() {
  return json<{ ok: boolean; version: string }>(`${BASE}/health`);
}
