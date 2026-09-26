// Shared helpers for the model-config list page (multi-provider-model-config.md §2).
import type { ModelConfig, ModelConfigRefs, ModelProtocol } from "@/lib/types";

export const PROTOCOLS: ModelProtocol[] = ["anthropic", "openai-compatible", "openai-responses"];

export const PROTOCOL_LABEL: Record<ModelProtocol, string> = {
  anthropic: "Anthropic",
  "openai-compatible": "OpenAI Compatible",
  "openai-responses": "OpenAI Responses",
};

// Migrated `openai` slots show as plain "OpenAI" when pointed at the official
// host (design §3.2) — same wire protocol, friendlier badge.
export function protocolBadge(cfg: { protocol: ModelProtocol; base_url?: string }): {
  label: string;
  color: string;
} {
  if (cfg.protocol === "anthropic") return { label: "Anthropic", color: "#f97316" };
  if (cfg.protocol === "openai-responses") return { label: "OpenAI Responses", color: "#3b82f6" };
  if (hostOf(cfg.base_url) === "api.openai.com") return { label: "OpenAI", color: "#22c55e" };
  return { label: "OpenAI Compatible", color: "#8b5cf6" };
}

export function hostOf(base_url?: string): string {
  if (!base_url) return "";
  try {
    return new URL(base_url).hostname;
  } catch {
    return "";
  }
}

// Q5: suggest compatible-endpoint private switches by base_url domain.
// Recommend-only — never auto-checked; the UI offers a one-click apply.
export const DOMAIN_RECOMMEND: Array<{
  host: string;
  flag: "enable_search" | "enable_thinking";
  why: string;
}> = [
  { host: "dashscope.aliyuncs.com", flag: "enable_search", why: "通义千问 compatible-mode 支持服务端联网搜索" },
  { host: "deepseek.com", flag: "enable_thinking", why: "DeepSeek 支持思考模式输出推理过程" },
];

export function domainRecommendations(base_url: string) {
  const host = hostOf(base_url);
  if (!host) return [];
  return DOMAIN_RECOMMEND.filter((r) => host === r.host || host.endsWith(`.${r.host}`) || host.endsWith(r.host));
}

// Defensive ref-summary for the Q8 yellow bar. The backend may send counts or
// id lists; unknown keys pass through as-is.
export function describeRefs(refs?: ModelConfigRefs): string {
  if (!refs) return "";
  const LABEL: Record<string, string> = { agents: "Agent", sessions: "会话", workflows: "Workflow" };
  const parts: string[] = [];
  for (const [key, value] of Object.entries(refs)) {
    const n = Array.isArray(value) ? value.length : typeof value === "number" ? value : 0;
    if (!n) continue;
    const label = LABEL[key] ?? key;
    // Space only reads well before an ASCII label ("3 个 Agent" vs "12 个会话").
    parts.push(/[\x00-\x7f]/.test(label[0]) ? `${n} 个 ${label}` : `${n} 个${label}`);
  }
  return parts.join("、");
}

export function blankConfig(): ModelConfig {
  return {
    id: "",
    name: "",
    protocol: "openai-compatible",
    base_url: "",
    api_key: "",
    models: [],
    default_model: "",
    max_tokens: 8192,
    temperature: 0.7,
    timeout_s: 60,
    enabled: true,
  };
}

// Tolerate sparse rows from the migration path (e.g. missing models[]).
export function normalizeConfig(raw: ModelConfig): ModelConfig {
  return { ...raw, models: Array.isArray(raw.models) ? raw.models : [] };
}

export function relTime(ts: number): string {
  const d = Math.max(0, Date.now() / 1000 - ts);
  if (d < 90) return "刚刚";
  if (d < 3600) return `${Math.round(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.round(d / 3600)} 小时前`;
  return `${Math.round(d / 86400)} 天前`;
}

// Parse the free-form models textarea: newline- or comma-separated ids,
// trimmed, de-duplicated, order preserved.
export function parseModelsText(text: string): string[] {
  const seen = new Set<string>();
  for (const part of text.split(/[\n,]/)) {
    const id = part.trim();
    if (id) seen.add(id);
  }
  return [...seen];
}
