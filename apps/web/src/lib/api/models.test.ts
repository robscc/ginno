/**
 * models API 客户端单测（providers / model configs / agents / subagent types）。
 *
 * 打桩策略：桩 globalThis.fetch，走真实 core.json() 请求构造，不 mock ./core。
 * node 环境无 window → BASE 回落 http://127.0.0.1:8787/api。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentConfig, ModelConfig } from "../types";
import {
  createAgent,
  deleteAgent,
  getProviders,
  listAgents,
  listModelConfigs,
  listModelsForConfig,
  listSubagentTypes,
  putModelConfigs,
  reorderAgents,
  searchProbeProvider,
  updateAgent,
  updateSubagentType,
  verifyModelConfig,
  verifyProvider,
} from "./models";

const BASE = "http://127.0.0.1:8787/api";

/** 满足 ModelConfig 必填字段的最小 fixture。 */
const CFG: ModelConfig = {
  id: "prov_1",
  name: "Test Provider",
  enabled: true,
  protocol: "openai-compatible",
  base_url: "https://api.example.com/v1",
  api_key: "sk-test",
  models: ["m1", "m2"],
  default_model: "m1",
  max_tokens: 4096,
  temperature: 0.7,
  timeout_s: 60,
};

/** 满足 AgentConfig 必填字段的最小 fixture。 */
const AGENT: AgentConfig = {
  id: "a1",
  name: "Agent",
  icon: "🤖",
  color: "#888888",
  system_prompt: "you are a test agent",
  provider: "prov_1",
  model: "m1",
  tools_allow: [],
  memory_scope: "global",
  status: "active",
};

function stubFetch(payload: unknown = { ok: true }) {
  const mock = vi.fn().mockImplementation(async () => ({ json: async () => payload }));
  vi.stubGlobal("fetch", mock);
  return mock;
}

function onlyRequest(mock: ReturnType<typeof vi.fn>) {
  expect(mock).toHaveBeenCalledTimes(1);
  const [url, init] = mock.mock.calls[0] as [string, RequestInit | undefined];
  expect(init?.headers).toMatchObject({ "X-Ginno-Language": "en" });
  return { url, init };
}

function bodyOf(init: RequestInit | undefined) {
  expect(init?.headers).toMatchObject({ "Content-Type": "application/json" });
  return JSON.parse(init?.body as string);
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("providers（legacy 通道）", () => {
  it("getProviders：GET /providers，响应透传", async () => {
    const payload = { default_provider: "openai", providers: {} };
    const mock = stubFetch(payload);
    await expect(getProviders()).resolves.toEqual(payload);
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/providers`);
    expect(init?.method ?? "GET").toBe("GET");
  });

  it("verifyProvider：POST /providers/{id}/verify，无 body", async () => {
    const payload = { ok: true, models: ["m1"] };
    const mock = stubFetch(payload);
    await expect(verifyProvider("openai")).resolves.toEqual(payload);
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/providers/openai/verify`);
    expect(init?.method).toBe("POST");
    expect(init?.body).toBeUndefined();
  });

  it("searchProbeProvider：POST search_probe；ok:false 错误原样透传", async () => {
    const payload = { ok: false, error: "probe timeout" };
    const mock = stubFetch(payload);
    await expect(searchProbeProvider("openai")).resolves.toEqual(payload);
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/providers/openai/search_probe`);
    expect(init?.method).toBe("POST");
    expect(init?.body).toBeUndefined();
  });
});

describe("model configs", () => {
  it("listModelConfigs：GET /model_configs，空清单透传", async () => {
    const payload = { ok: true, configs: [], default_config: "" };
    const mock = stubFetch(payload);
    await expect(listModelConfigs()).resolves.toEqual(payload);
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/model_configs`);
  });

  it("putModelConfigs：PUT 全量替换，body={configs, default_config}", async () => {
    const payload = { ok: true, default_config: "prov_1" };
    const mock = stubFetch(payload);
    await expect(putModelConfigs([CFG], "prov_1")).resolves.toEqual(payload);
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/model_configs`);
    expect(init?.method).toBe("PUT");
    expect(bodyOf(init)).toEqual({ configs: [CFG], default_config: "prov_1" });
  });

  it("putModelConfigs：空数组合法；拒绝时 references 错误体原样透传（HTTP 400 约定）", async () => {
    const payload = {
      ok: false,
      error: "still referenced",
      references: { prov_1: { agents: [{ id: "a1", name: "Agent" }], is_default: true } },
    };
    const mock = stubFetch(payload);
    await expect(putModelConfigs([], "")).resolves.toEqual(payload);
    const { init } = onlyRequest(mock);
    expect(bodyOf(init)).toEqual({ configs: [], default_config: "" });
  });

  it("verifyModelConfig：POST draft；通过时返回的 config 即已保存记录", async () => {
    const draft = { name: "draft", base_url: "https://x", api_key: "k", protocol: "openai-compatible" as const };
    const payload = { ok: true, config: CFG, latency_ms: 123 };
    const mock = stubFetch(payload);
    await expect(verifyModelConfig(draft)).resolves.toEqual(payload);
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/model_configs/verify`);
    expect(init?.method).toBe("POST");
    expect(bodyOf(init)).toEqual(draft);
  });

  it("verifyModelConfig：失败是 HTTP 200 {ok:false, error}，不抛错", async () => {
    const payload = { ok: false, error: "401 unauthorized" };
    const mock = stubFetch(payload);
    await expect(verifyModelConfig({ id: "prov_1" })).resolves.toEqual(payload);
    const { init } = onlyRequest(mock);
    expect(bodyOf(init)).toEqual({ id: "prov_1" });
  });

  it("listModelsForConfig：POST draft（无需 id）；成功返回 models 列表", async () => {
    const draft = { base_url: "https://x", api_key: "k" };
    const payload = {
      ok: true,
      models: [{ id: "m1", owned_by: "org" }, { id: "m2", owned_by: null }],
      model_tag_recommendations: { m1: { tag: "codex", max_effort: "ultra" }, m2: { tag: "codex" } },
      latency_ms: 88,
    };
    const mock = stubFetch(payload);
    await expect(listModelsForConfig(draft)).resolves.toEqual(payload);
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/model_configs/list_models`);
    expect(init?.method).toBe("POST");
    expect(bodyOf(init)).toEqual(draft);
  });

  it("listModelsForConfig：ok:false 错误透传", async () => {
    const payload = { ok: false, error: "connection refused" };
    const mock = stubFetch(payload);
    await expect(listModelsForConfig({})).resolves.toEqual(payload);
    const { init } = onlyRequest(mock);
    expect(bodyOf(init)).toEqual({});
  });
});

describe("agents", () => {
  it("listAgents：GET /agents，空数组透传", async () => {
    const mock = stubFetch([]);
    await expect(listAgents()).resolves.toEqual([]);
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/agents`);
  });

  it("createAgent：POST /agents + body", async () => {
    const payload = { ok: true, agent: AGENT };
    const mock = stubFetch(payload);
    await expect(createAgent({ name: "Agent", provider: "prov_1" })).resolves.toEqual(payload);
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/agents`);
    expect(init?.method).toBe("POST");
    expect(bodyOf(init)).toEqual({ name: "Agent", provider: "prov_1" });
  });

  it("updateAgent：PUT /agents/{id} + body", async () => {
    const mock = stubFetch({ ok: true, agent: AGENT });
    await updateAgent("a1", { model: "m2" });
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/agents/a1`);
    expect(init?.method).toBe("PUT");
    expect(bodyOf(init)).toEqual({ model: "m2" });
  });

  it("deleteAgent：DELETE /agents/{id}", async () => {
    const mock = stubFetch({ ok: true });
    await expect(deleteAgent("a1")).resolves.toEqual({ ok: true });
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/agents/a1`);
    expect(init?.method).toBe("DELETE");
  });

  it("reorderAgents：POST /agents/reorder + {order}（空数组也原样发送）", async () => {
    const mock = stubFetch({ ok: true });
    await reorderAgents(["a2", "a1"]);
    let { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/agents/reorder`);
    expect(init?.method).toBe("POST");
    expect(bodyOf(init)).toEqual({ order: ["a2", "a1"] });

    vi.clearAllMocks();
    await reorderAgents([]);
    ({ url, init } = onlyRequest(mock));
    expect(bodyOf(init)).toEqual({ order: [] });
  });

  it("reorderAgents：服务端校验失败 {ok:false, error} 透传", async () => {
    const payload = { ok: false, error: "order must cover all agents" };
    const mock = stubFetch(payload);
    await expect(reorderAgents(["a1"])).resolves.toEqual(payload);
    onlyRequest(mock);
  });
});

describe("subagent types", () => {
  it("listSubagentTypes：GET /subagent_types，注册表透传", async () => {
    const payload = [
      { name: "implementer", description: "写代码", model: "prov_1/m1", tools_allow: ["bash"] },
    ];
    const mock = stubFetch(payload);
    await expect(listSubagentTypes()).resolves.toEqual(payload);
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/subagent_types`);
  });

  it("updateSubagentType：PUT + name 走 encodeURIComponent；tools_allow=[] 保留（=不收紧）", async () => {
    const payload = { ok: true, type: { name: "code reviewer", description: "", model: "" } };
    const mock = stubFetch(payload);
    const data = { description: "审查", tools_allow: [], model: "prov_1/m2" };
    await expect(updateSubagentType("code reviewer/zh", data)).resolves.toEqual(payload);
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/subagent_types/code%20reviewer%2Fzh`);
    expect(init?.method).toBe("PUT");
    expect(bodyOf(init)).toEqual(data);
  });

  it("updateSubagentType：{ok:false, error} 透传", async () => {
    const payload = { ok: false, error: "unknown type" };
    const mock = stubFetch(payload);
    await expect(updateSubagentType("nope", { model: "m" })).resolves.toEqual(payload);
    onlyRequest(mock);
  });
});

describe("错误传播", () => {
  it("fetch 网络异常 → Promise reject", async () => {
    const mock = vi.fn().mockImplementation(async () => {
      throw new Error("sidecar unreachable");
    });
    vi.stubGlobal("fetch", mock);
    await expect(listAgents()).rejects.toThrow("sidecar unreachable");
    expect(mock).toHaveBeenCalledTimes(1);
  });
});
