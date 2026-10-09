// MCP 设置页共享类型与纯函数（无 React 依赖）。 transport 归一化、配置解析、
// graph 命名、token 估算都集中在这里，卡片/向导/详情三处共用同一份逻辑。

/** mcpServers[name] 的单条配置（registry.py 契约：transport|type、url 或 command/args/env）。 */
export type McpServerEntry = Record<string, unknown>;

/** 三种规范化 transport；其它拼写归一化后落 streamable-http / 原样透传。 */
export type McpTransport = "streamable-http" | "stdio" | "sse";

/** 每工具约 130 token/轮（原型 v2 的估算口径）。 */
export const TOKENS_PER_TOOL = 130;

/** graph 侧工具全名 — 与 runtime registry._full_tool_name 保持一致。 */
export function fullToolName(server: string, tool: string): string {
  return `mcp_${server}_${tool}`;
}

/** 由 graph 全名还原 tool 短名（前缀不匹配时原样返回）。 */
export function shortToolName(server: string, full: string): string {
  const prefix = `mcp_${server}_`;
  return full.startsWith(prefix) ? full.slice(prefix.length) : full;
}

/** registry.py 的归一化规则：trim + 小写 + 下划线转连字符。 */
export function normalizeTransport(v: unknown): string {
  return String(v ?? "")
    .trim()
    .toLowerCase()
    .replace(/_/g, "-");
}

/** 从配置条目推导 transport：transport → type →（有 command 视为 stdio，否则 streamable-http）。 */
export function transportOf(entry: McpServerEntry): McpTransport {
  const raw = normalizeTransport(entry.transport ?? entry.type);
  if (raw === "stdio") return "stdio";
  if (raw === "sse") return "sse";
  if (raw) return "streamable-http";
  return "command" in entry ? "stdio" : "streamable-http";
}

/** transport 徽章配色（原型 v2：http 蓝 / stdio 紫 / sse 黄）。 */
export function transportBadgeClass(t: string): string {
  if (t === "stdio") return "border-violet-500/40 bg-violet/10 text-violet-400";
  if (t === "sse") return "border-yellow-500/40 bg-yellow/10 text-yellow-400";
  return "border-blue-500/40 bg-blue/10 text-blue-400";
}

/** 卡片/详情头部的单行摘要：stdio → command + args；远程 → url。 */
export function serverLine(entry: McpServerEntry): string {
  if (transportOf(entry) === "stdio") {
    const args = Array.isArray(entry.args) ? (entry.args as unknown[]).map(String) : [];
    return [String(entry.command ?? ""), ...args].join(" ").trim();
  }
  return String(entry.url ?? "");
}

/** token 数格式化：>=1000 显示 1.2k。 */
export function fmtTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

/** 时长人性化（connectedAt 的 "connected for X"）：3d / 2h / 5m / <1m。 */
export function fmtDuration(from: number): string {
  const s = Math.max(0, (Date.now() - from) / 1000);
  if (s < 60) return "<1m";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

/** HH:MM（lastErrorAt 的 "failed at 09:58"）。 */
export function fmtClock(ts: number): string {
  return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** 卡片/详情头的 meta 片段：在线 → connected for X；失败 → failed at HH:MM。
 *  connectedAt 是 epoch 秒（后端契约，未连为 null）；lastErrorAt 缺失（后端未供）
 *  时对应分支自然不渲染。 */
export function statusMeta(s: { connected: boolean; connectedAt?: number | null; lastErrorAt?: number }):
  | { kind: "up" | "failed"; value: string }
  | null {
  if (s.connected && typeof s.connectedAt === "number")
    return { kind: "up", value: fmtDuration(s.connectedAt * 1000) };
  if (!s.connected && typeof s.lastErrorAt === "number") return { kind: "failed", value: fmtClock(s.lastErrorAt) };
  return null;
}

/** 用于 id 的 slug（仅前端测试 key 用）。 */
export function slugify(name: string): string {
  return (
    name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "server"
  );
}

/** 粘贴 JSON 的解析产物：一个 server 名 + 归一化后的配置条目。 */
export type ParsedServer = { name: string; entry: McpServerEntry };

/**
 * 兼容三种粘贴形态：
 *  1. {"mcpServers": {...}}          — Claude Desktop / .mcp.json
 *  2. {"name": {...}, ...}           — 裸的 name→entry 字典
 *  3. {"command": ...} / {"url":...} — 单个 entry 本身
 * 客户端做 type/transport 拼写归一化预览（registry.py 在保存后也会再归一化一次）。
 */
export function parseMcpServersJson(text: string): ParsedServer[] {
  const j = JSON.parse(text) as Record<string, unknown>;
  if (!j || typeof j !== "object" || Array.isArray(j)) throw new Error("not an object");
  let dict: Record<string, unknown> | null = null;
  if (j.mcpServers && typeof j.mcpServers === "object" && !Array.isArray(j.mcpServers)) {
    dict = j.mcpServers as Record<string, unknown>;
  } else if ("command" in j || "url" in j) {
    dict = { server: j }; // 单 entry 兜底名
  } else {
    dict = j;
  }
  const out: ParsedServer[] = [];
  for (const [name, raw] of Object.entries(dict)) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) continue;
    const src = raw as Record<string, unknown>;
    // 拼写归一化：type/transport 二选一 → 统一写成 transport（registry 两者都认）
    const t = normalizeTransport(src.transport ?? src.type);
    const entry: McpServerEntry = { ...src };
    delete entry.type;
    if (t) entry.transport = t;
    else if ("command" in entry) entry.transport = "stdio";
    else if ("url" in entry) entry.transport = "streamable-http";
    out.push({ name, entry });
  }
  if (!out.length) throw new Error("no servers found");
  return out;
}

/** 原型 v2 的模板 chips（占位 URL 由用户粘贴真实值）。 */
export type McpTemplate = {
  key: string;
  label: string;
  transport: "http" | "stdio";
  name: string;
  url?: string;
  command?: string;
  args?: string;
  env?: string;
};

export const MCP_TEMPLATES: McpTemplate[] = [
  {
    key: "dingtalk-docs",
    label: "钉钉文档",
    transport: "http",
    name: "钉钉文档",
    url: "https://mcp-gw.dingtalk.com/server/...?key=...",
  },
  {
    key: "volc-search",
    label: "火山联网搜索",
    transport: "http",
    name: "火山引擎联网搜索服务",
    url: "http://ai-mcp-gw.sf-express.com/volc-web-search/mcp",
  },
  { key: "sentry", label: "Sentry", transport: "http", name: "sentry", url: "https://mcp.sentry.dev/mcp" },
  {
    key: "github",
    label: "GitHub (stdio)",
    transport: "stdio",
    name: "github",
    command: "npx",
    args: "-y @modelcontextprotocol/server-github",
    env: "GITHUB_TOKEN",
  },
  {
    key: "filesystem",
    label: "Filesystem (stdio)",
    transport: "stdio",
    name: "filesystem",
    command: "uvx",
    args: "mcp-server-filesystem ~/workspace",
  },
];

/** 详情页活动时间线的单条事件（前端观测，来自 5s 轮询的状态差分）。 */
export type McpActivityEvent = {
  ts: number;
  level: "info" | "warn" | "error" | "gray";
  text: string;
};
