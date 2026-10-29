/**
 * Providers, model configs and agents.
 *
 * Split out of lib/runtime.ts, which re-exports every name unchanged.
 */
import { BASE, H, json } from "./core";
import type {
  AgentConfig,
  ModelConfig,
  ModelConfigReference,
  Providers,
  VerifyResult,
} from "../types";

// ---- providers ----
export async function getProviders() {
  return json<{ default_provider: string; providers: Providers }>(`${BASE}/providers`);
}

export async function verifyProvider(id: string) {
  return json<VerifyResult>(`${BASE}/providers/${id}/verify`, { method: "POST" });
}

export async function searchProbeProvider(id: string) {
  return json<{ ok: boolean; error?: string; latency_ms?: number; text?: string }>(
    `${BASE}/providers/${id}/search_probe`,
    { method: "POST" },
  );
}

// ---- model configs (multi-provider-model-config.md §2) ----
// The settings tab's Model API section talks ONLY to these endpoints; the
// legacy /providers functions above stay exported for their other consumers
// (store, AgentsSettings, GeneralSettings, ChatStream) until the backend
// swap completes.

export async function listModelConfigs() {
  return json<{ ok: boolean; configs: ModelConfig[]; default_config: string }>(
    `${BASE}/model_configs`,
  );
}

// Full-replacement write. Deletion is a PUT with the entry removed
// client-side (no DELETE endpoint). Refusals (still default / still
// referenced) come back HTTP 400 with `references` keyed by config id
// ({agents:[{id,name}], is_default}) — same shape as DELETE; the UI turns
// that into the "why can't I delete this" yellow bar.
export async function putModelConfigs(configs: ModelConfig[], default_config: string) {
  return json<{
    ok: boolean;
    error?: string;
    references?: Record<string, ModelConfigReference>;
    default_config?: string;
  }>(`${BASE}/model_configs`, {
    method: "PUT",
    headers: H,
    body: JSON.stringify({ configs, default_config }),
  });
}

// Q4: a passing probe is persisted server-side immediately — the returned
// `config` IS the saved record (merge it into the local list; no extra PUT).
// For edits pass the existing id so the backend updates in place; new drafts
// omit it and get a server-generated id back. Failures are HTTP 200
// {ok:false, error, refs?}.
export async function verifyModelConfig(draft: Partial<ModelConfig>) {
  return json<
    | { ok: true; config: ModelConfig; latency_ms: number }
    | { ok: false; error: string }
  >(`${BASE}/model_configs/verify`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(draft),
  });
}

// ---- agents ----
export async function listAgents() {
  return json<AgentConfig[]>(`${BASE}/agents`);
}
/** 子代理类型注册表（~/.ginno/agents/subagents/*.md）——Agents 页
 *  Sub-agent models 区块的数据源（model-assignment-design.md §2.3）。
 *  model 为类型 frontmatter 的默认覆盖原文（provider/model 或裸名）；
 *  tools_allow/body 供 Sub-agent types 编辑区回填（spawn 时按白名单收紧
 *  子代工具集——2026-10-12 子代「用不了 MCP」即 implementer 白名单不含
 *  mcp_*，产品内可改）。 */
export interface SubagentTypeInfo {
  name: string;
  description: string;
  model: string;
  tools_allow?: string[];
  body?: string;
}
export async function listSubagentTypes() {
  return json<SubagentTypeInfo[]>(`${BASE}/subagent_types`);
}
/** 保存一个类型的编辑（原地改写 md 文件；name 不可改）。tools_allow=[]
 *  = 不收紧（继承全量并集工具集，含 MCP）。 */
export async function updateSubagentType(
  name: string,
  data: Partial<Pick<SubagentTypeInfo, "description" | "tools_allow" | "model" | "body">>,
) {
  return json<{ ok: boolean; type?: SubagentTypeInfo; error?: string }>(
    `${BASE}/subagent_types/${encodeURIComponent(name)}`,
    { method: "PUT", headers: H, body: JSON.stringify(data) },
  );
}
export async function createAgent(data: Partial<AgentConfig>) {
  return json<{ ok: boolean; agent?: AgentConfig; error?: string }>(`${BASE}/agents`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(data),
  });
}
export async function updateAgent(id: string, data: Partial<AgentConfig>) {
  return json<{ ok: boolean; agent?: AgentConfig; error?: string }>(`${BASE}/agents/${id}`, {
    method: "PUT",
    headers: H,
    body: JSON.stringify(data),
  });
}
export async function deleteAgent(id: string) {
  return json<{ ok: boolean }>(`${BASE}/agents/${id}`, { method: "DELETE" });
}
// 服务端校验 order 必须恰好覆盖当前 agent 集合（读-改-写 settings.json）。
export async function reorderAgents(order: string[]) {
  return json<{ ok: boolean; error?: string }>(`${BASE}/agents/reorder`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ order }),
  });
}

// 模型列表「从 API 拉取」: probes the provider's list-models endpoint with a
// config DRAFT (nothing is saved — id not required). HTTP 200 always;
// ok:false carries the error, same convention as verifyModelConfig.
export async function listModelsForConfig(draft: Partial<ModelConfig>) {
  return json<
    | { ok: true; models: Array<{ id: string; owned_by: string | null }>; latency_ms: number }
    | { ok: false; error: string }
  >(`${BASE}/model_configs/list_models`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(draft),
  });
}
