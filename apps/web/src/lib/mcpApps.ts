/**
 * MCP Apps（docs/mcp-apps-design.md §2 前端）：tools-with-ui 清单的类型与
 * 缓存 store。零依赖纯逻辑（fetcher 注入）——node 环境可直接 vitest；
 * 真实 fetch 封装与默认实例在 lib/api/mcpApps.ts。
 *
 * 「工具块要不要渲染 app 卡」的判定数据源：runtime GET /api/mcp/tools-with-ui
 * 给 {graphName: {server, tool, resourceUri, csp, permissions}}，前端启动拉
 * 一次 + 过期后台补拉（决策记录在设计文档 §2 末尾：不动 WS 帧 schema）。
 */

export type McpToolUiInfo = {
  /** mcp.json 配置名（/api/mcp/call 的 server 参数） */
  server: string;
  /** graph 面向全名（mcp_<prefix>_<tool>，ASCII slug） */
  tool: string;
  /** ui:// resource 地址 */
  resourceUri: string;
  csp?: string | null;
  permissions?: string[] | null;
};

export type ToolsWithUiMap = Record<string, McpToolUiInfo>;

export type ToolsWithUiFetcher = () => Promise<ToolsWithUiMap>;

export interface ToolsWithUiStore {
  /** 首次拉取（单飞去重）；缓存新鲜时直接回缓存。 */
  get(): Promise<ToolsWithUiMap>;
  /** 强制重拉（同刻多次调用共享同一个 in-flight promise）。 */
  refresh(): Promise<ToolsWithUiMap>;
  /** 同步读缓存（未拉取过为 null）——渲染路径不想 await。 */
  peek(): ToolsWithUiMap | null;
  /** 同步查一个 graph 全名（未命中/未拉取为 null）。 */
  lookup(graphName: string): McpToolUiInfo | null;
}

export interface ToolsWithUiStoreOptions {
  /** 缓存新鲜期（ms）；过期后 get() 重新拉取。默认 5 分钟。 */
  staleMs?: number;
  now?: () => number;
}

export function createToolsWithUiStore(
  fetcher: ToolsWithUiFetcher,
  opts: ToolsWithUiStoreOptions = {},
): ToolsWithUiStore {
  const staleMs = opts.staleMs ?? 5 * 60_000;
  const now = opts.now ?? (() => Date.now());
  let cache: ToolsWithUiMap | null = null;
  let fetchedAt = 0;
  let inflight: Promise<ToolsWithUiMap> | null = null;

  async function refresh(): Promise<ToolsWithUiMap> {
    // 单飞：挂载了 N 张卡的会话也只发一次请求
    if (inflight) return inflight;
    inflight = (async () => {
      try {
        const map = await fetcher();
        // 失败不落缓存（throw 在下面传播），成功才替换
        cache = map && typeof map === "object" ? map : {};
        fetchedAt = now();
        return cache;
      } finally {
        inflight = null;
      }
    })();
    return inflight;
  }

  function get(): Promise<ToolsWithUiMap> {
    if (cache && now() - fetchedAt < staleMs) return Promise.resolve(cache);
    return refresh();
  }

  function peek(): ToolsWithUiMap | null {
    return cache;
  }

  function lookup(graphName: string): McpToolUiInfo | null {
    return cache?.[graphName] ?? null;
  }

  return { get, refresh, peek, lookup };
}
