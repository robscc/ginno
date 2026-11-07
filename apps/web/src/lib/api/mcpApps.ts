/**
 * MCP Apps 的 sidecar HTTP 封装（docs/mcp-apps-design.md §2 ③）：
 * GET /api/mcp/tools-with-ui、GET /api/mcp/ui、POST /api/mcp/call。
 * 默认清单 store 也在这里组装（fetcher = getMcpToolsWithUi）。
 */

import { currentLocale } from "../../i18n/provider";
import { createToolsWithUiStore, type ToolsWithUiMap } from "../mcpApps";
import { BASE, H, json } from "./core";

export type { McpToolUiInfo, ToolsWithUiMap, ToolsWithUiStore } from "../mcpApps";

/** GET /api/mcp/ui 的 200 载荷（404 时 getMcpToolUi 返回 null）。 */
export type McpToolUiPayload = {
  html: string;
  csp?: string | null;
  permissions?: string[] | null;
  server: string;
  tool: string;
};

/** POST /api/mcp/call 的 200 载荷。 */
export type McpAppCallPayload = {
  ok: boolean;
  content: string[];
  structured?: unknown;
};

/** 403：被权限策略拒绝（P0 收敛：deny 与 ask 同 403）。 */
export class McpAppPermissionError extends Error {
  decision?: string;
  constructor(message: string, decision?: string) {
    super(message);
    this.name = "McpAppPermissionError";
    this.decision = decision;
  }
}

export async function getMcpToolsWithUi(): Promise<ToolsWithUiMap> {
  const r = await json<{ tools?: ToolsWithUiMap }>(`${BASE}/mcp/tools-with-ui`);
  return r.tools ?? {};
}

export async function getMcpToolUi(server: string, tool: string): Promise<McpToolUiPayload | null> {
  const q = new URLSearchParams({ server, tool });
  const r = await fetch(`${BASE}/mcp/ui?${q.toString()}`, {
    headers: { "X-Ginno-Language": currentLocale() },
  });
  if (r.status === 404) return null; // 无 UI：调用方回退普通渲染
  if (!r.ok) throw new Error(`mcp ui fetch failed (${r.status})`);
  return (await r.json()) as McpToolUiPayload;
}

export async function mcpAppCall(
  server: string,
  tool: string,
  args: Record<string, unknown>,
): Promise<McpAppCallPayload> {
  const r = await fetch(`${BASE}/mcp/call`, {
    method: "POST",
    headers: { ...H, "X-Ginno-Language": currentLocale() },
    body: JSON.stringify({ server, tool, arguments: args, origin: "mcp-app" }),
  });
  const data = (await r.json().catch(() => null)) as
    | (McpAppCallPayload & { error?: string; decision?: string })
    | null;
  if (r.status === 403) {
    throw new McpAppPermissionError(
      data?.error ?? "denied by permission policy",
      data?.decision,
    );
  }
  if (!r.ok || !data?.ok) {
    throw new Error(data?.error ?? `mcp call failed (${r.status})`);
  }
  return data;
}

/** 默认清单 store：组件层经 @/lib/runtime 引用。 */
export const toolsWithUi = createToolsWithUiStore(getMcpToolsWithUi);
