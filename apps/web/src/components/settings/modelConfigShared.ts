// Shared helpers for the model-config list page (multi-provider-model-config.md §2).
import type { ModelConfig, ModelConfigReference, ModelProtocol } from "@/lib/types";

export const PROTOCOLS: ModelProtocol[] = ["anthropic", "openai-compatible", "openai-responses"];

export const PROTOCOL_LABEL: Record<ModelProtocol, string> = {
  anthropic: "Anthropic",
  "openai-compatible": "OpenAI Compatible",
  "openai-responses": "OpenAI Responses",
};

// Migrated `openai` slots show as plain "OpenAI" when pointed at the official
// host (design §3.2) — same wire protocol, friendlier badge.
// 协议徽标均为品牌/协议名，不入 catalog。asr 卡（kind="asr"）用青色
// 「ASR」徽标 + AudioLines 图标（ModelConfigCard），与对话卡一眼可分。
export function protocolBadge(cfg: {
  protocol: ModelProtocol;
  base_url?: string;
  kind?: "chat" | "asr";
}): {
  label: string;
  color: string;
} {
  if (cfg.kind === "asr") return { label: "ASR", color: "#14b8a6" };
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
// why 文案在 catalog settings.model.recommendWhy.<whyKey>（模块级无法用 hook）。
export const DOMAIN_RECOMMEND: Array<{
  host: string;
  flag: "enable_search" | "enable_thinking";
  whyKey: "qwenSearch" | "deepseekThinking";
}> = [
  { host: "dashscope.aliyuncs.com", flag: "enable_search", whyKey: "qwenSearch" },
  { host: "deepseek.com", flag: "enable_thinking", whyKey: "deepseekThinking" },
];

export function domainRecommendations(base_url: string) {
  const host = hostOf(base_url);
  if (!host) return [];
  return DOMAIN_RECOMMEND.filter((r) => host === r.host || host.endsWith(`.${r.host}`) || host.endsWith(r.host));
}

/** 把守卫的 `references` 摊成黄条要的结构化片段(每个被挡下的配置一条)。
 * 后端只在该字段里给;前端原来读的扁平 `refs` 后端从未发过。 */
export function describeBlockedConfigs(
  references: Record<string, ModelConfigReference> | undefined,
  nameOf: (id: string) => string,
): {
  id: string;
  name: string;
  agents: { name: string; subagents: string[] }[];
  isDefault: boolean;
}[] {
  if (!references || typeof references !== "object") return [];
  return Object.entries(references).map(([id, r]) => ({
    id,
    name: nameOf(id) || id,
    agents: (r?.agents ?? [])
      .map((a) => ({
        name: (a?.name || a?.id || "").trim(),
        subagents: (a?.subagents ?? []).map((s) => String(s ?? "").trim()).filter(Boolean),
      }))
      .filter((a) => a.name),
    isDefault: Boolean(r?.is_default),
  }));
}


export function blankConfig(kind: "chat" | "asr" = "chat"): ModelConfig {
  return {
    id: "",
    name: "",
    protocol: "openai-compatible", // asr 也走 OpenAI 兼容端点（chat/completions + input_audio）
    kind,
    base_url: "",
    api_key: "",
    models: [],
    default_model: "",
    max_tokens: 8192,
    temperature: 0.7,
    timeout_s: 60,
    enabled: true,
    server_web_search: false,
    server_web_search_max_uses: 5,
  };
}

// Tolerate sparse rows from the migration path (e.g. missing models[]).
export function normalizeConfig(raw: ModelConfig): ModelConfig {
  return { ...raw, models: Array.isArray(raw.models) ? raw.models : [] };
}

export type RelTimePart =
  | { key: "justNow"; count?: undefined }
  | { key: "minAgo" | "hoursAgo" | "daysAgo"; count: number };

// 相对时间返回结构化片段，由组件经 settings.model.<key> 翻译。
export function relTimeParts(ts: number): RelTimePart {
  const d = Math.max(0, Date.now() / 1000 - ts);
  if (d < 90) return { key: "justNow" };
  if (d < 3600) return { key: "minAgo", count: Math.round(d / 60) };
  if (d < 86400) return { key: "hoursAgo", count: Math.round(d / 3600) };
  return { key: "daysAgo", count: Math.round(d / 86400) };
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

// Merge ids picked from the 「从 API 拉取」 panel into the manual list:
// existing order preserved, new ids appended sorted, duplicates dropped.
export function mergeModelIds(existing: string[], picked: string[]): string[] {
  const have = new Set(existing);
  const fresh = [...new Set(picked)].filter((id) => !have.has(id)).sort();
  return [...existing, ...fresh];
}
