/**
 * WebSocket entry points (session socket + run socket) and external
 * agent delegation.
 *
 * Split out of lib/runtime.ts, which re-exports every name unchanged.
 */
import { BASE, json, OVERRIDE_PORT, wsBase } from "./core";
// 非 hook 层取已解析 locale（模块级镜像，provider 渲染期同步赋值）：随每个
// API 请求/WS 连接下发给 runtime（i18n-design.md §1/§5）。
import { currentLocale } from "../../i18n/provider";

// 浏览器 WebSocket API 不能发自定义头——WS 侧经 ?lang=<resolved> 下发 locale，
// runtime 中间件在无 X-Ginno-Language 头时回退读 query_string。所有 new
// WebSocket 构造统一走此 helper，别在调用点裸建（会漏 lang）。
export function openSocket(url: string): WebSocket {
  const sep = url.includes("?") ? "&" : "?";
  return new WebSocket(`${url}${sep}lang=${encodeURIComponent(currentLocale())}`);
}

export function openSessionSocket(session_id: string): WebSocket {
  return openSocket(`${wsBase()}/${session_id}`);
}
/** Run-scoped live channel (design B P2): snapshot on connect, then
 *  run.event (payload.seq) / run.status / run.snapshot frames. */
export function openRunSocket(run_id: string): WebSocket {
  const path = `/api/ws/runs/${run_id}`;
  if (typeof window !== "undefined") {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = OVERRIDE_PORT
      ? `${window.location.hostname}:${OVERRIDE_PORT}`
      : window.location.host;
    return openSocket(`${proto}//${host}${path}`);
  }
  return openSocket(`ws://127.0.0.1:${OVERRIDE_PORT ?? 8787}${path}`);
}
export async function stopDelegation(id: string) {
  return json(`${BASE}/delegations/${id}/stop`, { method: "POST" });
}

export async function getExternalAgents(): Promise<
  { name: string; installed: boolean; path: string }[]
> {
  return json(`${BASE}/external-agents`);
}
