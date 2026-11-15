/**
 * api/instincts.ts（Instincts 本能层 API 客户端）单元测试。
 *
 * 打桩策略：桩 globalThis.fetch，**不 mock ./core 的 json**——真实走
 * URLSearchParams 过滤（undefined/空串跳过、0 值保留）与 locale 头合并。
 * i18n provider 桩成固定 locale（zh-CN）。
 *
 * 重点分支：listInstincts 参数过滤（含 min_confidence=0 必须发送）、
 * deleteInstinct 的 hard 缺省 false、runEvolve 只发三字段（count 不发）、
 * freezeConflict 空数组、encodeURIComponent 路径段。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../i18n/provider", () => ({ currentLocale: () => "zh-CN" }));

import { BASE } from "./core";
import {
  analyzeInstincts,
  createInstinct,
  deleteInstinct,
  exportInstincts,
  freezeConflict,
  getObserverState,
  importInstincts,
  listConflicts,
  listEvolveClusters,
  listInstincts,
  listObservations,
  resolveConflict,
  runEvolve,
  updateInstinct,
  type EvolveCluster,
  type Instinct,
  type InstinctBody,
} from "./instincts";

const API = "http://127.0.0.1:8787/api";
const LOCALE_HEADER = { "X-Ginno-Language": "zh-CN" };
const WRITE_HEADERS = { ...LOCALE_HEADER, "Content-Type": "application/json" };

/** 满足 Instinct 必填字段的最小 fixture。 */
const INSTINCT: Instinct = {
  id: "i1",
  trigger: "用户要求中文回复",
  behavior: "始终使用简体中文",
  confidence: 0.9,
  domain: "language",
  scope: "global",
  project_slug: "",
  source: "template",
  status: "active",
  evidence: { observed: 5, applied: 4, uncorrected: 4, corrected: 0, last_seen: "2026-11-15T00:00:00Z" },
  created: "2026-11-01T00:00:00Z",
  updated: "2026-11-15T00:00:00Z",
  evolved_to: null,
};

const BODY: InstinctBody = {
  id: "i1",
  trigger: "用户要求中文回复",
  behavior: "始终使用简体中文",
  confidence: 0.9,
  domain: "language",
  scope: "global",
  project_slug: "",
  source: "manual",
  status: "active",
  evidence: null,
};

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function reply(body: unknown) {
  fetchMock.mockResolvedValue({ json: async () => body });
}

function onlyCall() {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  return fetchMock.mock.calls[0] as [string, RequestInit];
}

/** 断言唯一一次 GET：精确 URL + locale 头 + 无 method/body。 */
function expectGet(url: string) {
  const [got, init] = onlyCall();
  expect(got).toBe(url);
  expect(init.method ?? "GET").toBe("GET");
  expect(init.body).toBeUndefined();
  expect(init.headers).toEqual(LOCALE_HEADER);
}

/** GET + query 精确参数集深比较。 */
function expectGetQuery(url: string, params: Record<string, string>) {
  expectGet(url);
  expect(Object.fromEntries(new URL(url).searchParams)).toEqual(params);
}

/** 断言唯一一次写请求：URL、method、JSON body 深比较、双头。 */
function expectWrite(method: "POST" | "PUT" | "DELETE", path: string, body: unknown) {
  const [url, init] = onlyCall();
  expect(url).toBe(`${API}${path}`);
  expect(init.method).toBe(method);
  expect(typeof init.body).toBe("string");
  expect(JSON.parse(init.body as string)).toEqual(body);
  expect(init.headers).toEqual(WRITE_HEADERS);
}

describe("BASE 回落（node 无 window）", () => {
  it("BASE 为 127.0.0.1:8787/api", () => {
    expect(BASE).toBe(API);
  });
});

describe("listInstincts 参数过滤", () => {
  it("不传 params：URL 不带 '?'", async () => {
    reply([INSTINCT]);
    await expect(listInstincts()).resolves.toEqual([INSTINCT]);
    expectGet(`${API}/instincts`);
  });

  it("空对象等价于不传", async () => {
    reply([]);
    await expect(listInstincts({})).resolves.toEqual([]);
    expectGet(`${API}/instincts`);
  });

  it("全参数：五个过滤项都进 query", async () => {
    reply([]);
    await listInstincts({
      scope: "project",
      project_slug: "ginno",
      status: "active",
      domain: "language",
      min_confidence: 0.7,
    });
    expectGetQuery(`${API}/instincts?scope=project&project_slug=ginno&status=active&domain=language&min_confidence=0.7`, {
      scope: "project",
      project_slug: "ginno",
      status: "active",
      domain: "language",
      min_confidence: "0.7",
    });
  });

  it("undefined 值被跳过", async () => {
    reply([]);
    await listInstincts({ scope: "global", status: undefined, domain: undefined });
    expectGetQuery(`${API}/instincts?scope=global`, { scope: "global" });
  });

  it("空串值被跳过（v !== '' 守卫）", async () => {
    reply([]);
    await listInstincts({ scope: "", project_slug: "", status: "pending" });
    expectGetQuery(`${API}/instincts?status=pending`, { status: "pending" });
  });

  it("min_confidence=0 必须发送（0 不是 undefined/空串）", async () => {
    reply([]);
    await listInstincts({ min_confidence: 0 });
    expectGetQuery(`${API}/instincts?min_confidence=0`, { min_confidence: "0" });
  });

  it("project_slug 特殊字符经 URLSearchParams 编码", async () => {
    reply([]);
    await listInstincts({ project_slug: "a b&c" });
    const [url] = onlyCall();
    expect(Object.fromEntries(new URL(url).searchParams)).toEqual({ project_slug: "a b&c" });
  });
});

describe("observations / observer", () => {
  it("listObservations：缺省 limit=50", async () => {
    reply([]);
    await expect(listObservations()).resolves.toEqual([]);
    expectGetQuery(`${API}/instincts/observations?limit=50`, { limit: "50" });
  });

  it("listObservations：自定义 limit", async () => {
    const rows = [{ ts: 1, kind: "correction" }];
    reply(rows);
    await expect(listObservations(10)).resolves.toEqual(rows);
    expectGetQuery(`${API}/instincts/observations?limit=10`, { limit: "10" });
  });

  it("getObserverState：GET /instincts/observer-state，状态透传", async () => {
    const state = {
      enabled: true,
      offset: 10,
      total: 20,
      fresh: 3,
      last_analyze_ts: 1_700_000_000,
      last_error: "",
      observer_model: "glm",
      resolved_provider: "glm",
      min_observations: 3,
      correction_min_observations: 1,
      analyze_gap_min: 30,
    };
    reply(state);
    await expect(getObserverState()).resolves.toEqual(state);
    expectGet(`${API}/instincts/observer-state`);
  });

  it("analyzeInstincts：POST /instincts/analyze，无 body 但带 Content-Type 头", async () => {
    const result = { ok: true, fresh: 3, created: 1, reinforced: 2, processed: 3 };
    reply(result);
    await expect(analyzeInstincts()).resolves.toEqual(result);
    const [url, init] = onlyCall();
    expect(url).toBe(`${API}/instincts/analyze`);
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
    expect(init.headers).toEqual(WRITE_HEADERS);
  });

  it("analyzeInstincts：skipped 节流响应透传", async () => {
    const result = { ok: true, skipped: "throttled" };
    reply(result);
    await expect(analyzeInstincts()).resolves.toEqual(result);
  });
});

describe("instinct CRUD", () => {
  it("createInstinct：POST /instincts，body 原样深比较", async () => {
    reply(INSTINCT);
    await expect(createInstinct(BODY)).resolves.toEqual(INSTINCT);
    expectWrite("POST", "/instincts", BODY);
  });

  it("updateInstinct：PUT /instincts/{id}，body 原样", async () => {
    const updated = { ...INSTINCT, behavior: "新行为", updated: "2026-11-15T01:00:00Z" };
    reply(updated);
    await expect(updateInstinct("i1", BODY)).resolves.toEqual(updated);
    expectWrite("PUT", "/instincts/i1", BODY);
  });

  it("updateInstinct：id 含特殊字符须编码", async () => {
    reply(INSTINCT);
    await updateInstinct("i/1", BODY);
    const [url] = onlyCall();
    expect(url).toBe(`${API}/instincts/i%2F1`);
  });

  it("deleteInstinct：缺省 hard=false 进 query，DELETE 带双头无 body", async () => {
    reply({ ok: true });
    await expect(deleteInstinct("i1", "project", "ginno")).resolves.toEqual({ ok: true });
    const [url, init] = onlyCall();
    expect(url).toBe(`${API}/instincts/i1?scope=project&project_slug=ginno&hard=false`);
    expect(Object.fromEntries(new URL(url).searchParams)).toEqual({
      scope: "project",
      project_slug: "ginno",
      hard: "false",
    });
    expect(init.method).toBe("DELETE");
    expect(init.body).toBeUndefined();
    expect(init.headers).toEqual(WRITE_HEADERS);
  });

  it("deleteInstinct：hard=true 显式覆盖", async () => {
    reply({ ok: true });
    await deleteInstinct("i1", "global", "", true);
    const [url, init] = onlyCall();
    expect(url).toBe(`${API}/instincts/i1?scope=global&project_slug=&hard=true`);
    expect(Object.fromEntries(new URL(url).searchParams)).toEqual({
      scope: "global",
      project_slug: "",
      hard: "true",
    });
    expect(init.method).toBe("DELETE");
  });

  it("deleteInstinct：project_slug 编码、id 编码", async () => {
    reply({ ok: true });
    await deleteInstinct("i/1", "project", "a b&c");
    const [url] = onlyCall();
    expect(url.startsWith(`${API}/instincts/i%2F1?`)).toBe(true);
    expect(Object.fromEntries(new URL(url).searchParams)).toEqual({
      scope: "project",
      project_slug: "a b&c",
      hard: "false",
    });
  });

  it("deleteInstinct：ok=false 响应透传", async () => {
    reply({ ok: false });
    await expect(deleteInstinct("i1", "global", "")).resolves.toEqual({ ok: false });
  });
});

describe("conflicts（冲突裁决）", () => {
  it("listConflicts：GET /instincts/conflicts，空数组透传", async () => {
    reply([]);
    await expect(listConflicts()).resolves.toEqual([]);
    expectGet(`${API}/instincts/conflicts`);
  });

  it("listConflicts：冲突对透传", async () => {
    const pairs = [{ a: INSTINCT, b: { ...INSTINCT, id: "i2" }, trigger_overlap: 0.8 }];
    reply(pairs);
    await expect(listConflicts()).resolves.toEqual(pairs);
  });

  it("freezeConflict：POST body {ids}", async () => {
    reply({ ok: true, frozen: 2 });
    await expect(freezeConflict(["i1", "i2"])).resolves.toEqual({ ok: true, frozen: 2 });
    expectWrite("POST", "/instincts/conflicts/freeze", { ids: ["i1", "i2"] });
  });

  it("freezeConflict：空数组原样发送", async () => {
    reply({ ok: true, frozen: 0 });
    await freezeConflict([]);
    expectWrite("POST", "/instincts/conflicts/freeze", { ids: [] });
  });

  it("resolveConflict：POST body {keep_id, drop_id}", async () => {
    reply({ ok: true });
    await resolveConflict("i1", "i2");
    expectWrite("POST", "/instincts/conflicts/resolve", { keep_id: "i1", drop_id: "i2" });
  });
});

describe("evolve（蒸馏成 skill）", () => {
  it("listEvolveClusters：GET /instincts/evolve/clusters", async () => {
    const clusters: EvolveCluster[] = [{ scope: "project", project_slug: "ginno", domain: "git", count: 4 }];
    reply(clusters);
    await expect(listEvolveClusters()).resolves.toEqual(clusters);
    expectGet(`${API}/instincts/evolve/clusters`);
  });

  it("runEvolve：body 只含 scope/project_slug/domain（count 不发）", async () => {
    reply({ ok: true, skill: "git-discipline" });
    const cluster: EvolveCluster = { scope: "project", project_slug: "ginno", domain: "git", count: 4 };
    await expect(runEvolve(cluster)).resolves.toEqual({ ok: true, skill: "git-discipline" });
    expectWrite("POST", "/instincts/evolve", { scope: "project", project_slug: "ginno", domain: "git" });
  });
});

describe("export / import", () => {
  it("exportInstincts：GET /instincts/export，dump 透传", async () => {
    const dump = { version: 1, instincts: [INSTINCT] };
    reply(dump);
    await expect(exportInstincts()).resolves.toEqual(dump);
    expectGet(`${API}/instincts/export`);
  });

  it("importInstincts：POST /instincts/import，dump 原样作 body", async () => {
    const dump = { instincts: [INSTINCT] };
    const result = { ok: true, added: 1, updated: 0, skipped: 0 };
    reply(result);
    await expect(importInstincts(dump)).resolves.toEqual(result);
    expectWrite("POST", "/instincts/import", dump);
  });

  it("importInstincts：空清单原样发送", async () => {
    reply({ ok: true, added: 0, updated: 0, skipped: 0 });
    await importInstincts({ instincts: [] });
    expectWrite("POST", "/instincts/import", { instincts: [] });
  });
});

describe("错误传播", () => {
  it("fetch 网络异常 → Promise reject", async () => {
    fetchMock.mockRejectedValue(new Error("sidecar unreachable"));
    await expect(listInstincts()).rejects.toThrow("sidecar unreachable");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("响应 body 非法 JSON → r.json() 异常传播", async () => {
    fetchMock.mockResolvedValue({
      json: async () => {
        throw new SyntaxError("Unexpected end of JSON input");
      },
    });
    await expect(getObserverState()).rejects.toThrow("Unexpected end of JSON input");
  });
});
