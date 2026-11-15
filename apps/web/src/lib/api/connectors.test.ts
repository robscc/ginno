/**
 * api/connectors.ts（连接器与浏览器 handoff API 客户端）单元测试。
 *
 * 打桩策略：桩 globalThis.fetch，走真实 core.json()，**不 mock ./core**；
 * i18n provider 桩成固定 zh-CN。node 无 window → BASE/ws 回落 127.0.0.1:8787。
 *
 * 重点分支：getConnector/getConnectorDetail 同 URL 契约、connectorAction 的
 * {action, ...extra} 展开顺序（extra 可覆盖 action）、encodeURIComponent、
 * 空数组/空配置透传、wsConnectorsUrl 的 window/https/OVERRIDE_PORT 三态。
 * 类型导出（ConnectorInfo 等）无运行时行为，不在单测范围。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../i18n/provider", () => ({ currentLocale: () => "zh-CN" }));

import {
  connectorAction,
  getConnector,
  getConnectorDetail,
  getHandoffStatus,
  getPushedPage,
  listConnectors,
  patchConnectorConfig,
  wsConnectorsUrl,
} from "./connectors";
import { BASE, OVERRIDE_PORT } from "./core";

const API = "http://127.0.0.1:8787/api";

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

function expectGet(url: string) {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [got, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(got).toBe(url);
  expect(init.method ?? "GET").toBe("GET");
  expect(init.body).toBeUndefined();
  expect(init.headers).toEqual({ "X-Ginno-Language": "zh-CN" });
}

function expectWrite(method: "POST" | "PATCH", url: string, body: unknown) {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [got, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(got).toBe(url);
  expect(init.method).toBe(method);
  expect(typeof init.body).toBe("string");
  expect(JSON.parse(init.body as string)).toEqual(body);
  expect(init.headers).toEqual({
    "X-Ginno-Language": "zh-CN",
    "Content-Type": "application/json",
  });
}

/** 最小 ConnectorInfo fixture（必填字段齐全）。 */
const CONN = {
  id: "browser",
  name: "Browser",
  icon: "globe",
  description: "浏览器连接器",
  kind: "builtin",
  status: "connected",
  statusDetail: "",
  version: "1.0.0",
  capabilities: ["navigate"],
  enabled: true,
};

describe("BASE 回落（node 无 window）", () => {
  it("BASE 为 127.0.0.1:8787/api", () => {
    expect(BASE).toBe(API);
  });
});

describe("listConnectors", () => {
  it("GET /connectors，完整载荷（connectors+aggregateDot+runtime）透传", async () => {
    const payload = {
      connectors: [CONN],
      aggregateDot: "warn",
      runtime: { brokerOk: true, modsEnabled: true, loadedCount: 2, status: "connected" },
    };
    reply(payload);
    await expect(listConnectors()).resolves.toEqual(payload);
    expectGet(`${API}/connectors`);
  });

  it("空清单 + 空 aggregateDot 原样透传", async () => {
    const payload = { connectors: [], aggregateDot: "" };
    reply(payload);
    await expect(listConnectors()).resolves.toEqual(payload);
    expectGet(`${API}/connectors`);
  });
});

describe("getConnector / getConnectorDetail", () => {
  it("getConnector：GET /connectors/{id}", async () => {
    reply(CONN);
    await expect(getConnector("browser")).resolves.toEqual(CONN);
    expectGet(`${API}/connectors/browser`);
  });

  it("getConnector：id encodeURIComponent（斜杠/空格/&）", async () => {
    reply(CONN);
    await getConnector("mod/hello world&x");
    expectGet(`${API}/connectors/mod%2Fhello%20world%26x`);
  });

  it("getConnectorDetail：与 getConnector 同 URL，详情字段透传", async () => {
    const detail = {
      ...CONN,
      config: { token: "secret" },
      defaults: { token: "" },
      effective: { token: "secret" },
      configSchema: { type: "object", properties: { token: { type: "string", "x-ginno-secret": true } } },
      classicGrant: "ask",
      configSummary: { count: 1, modified: 1 },
    };
    reply(detail);
    await expect(getConnectorDetail("browser")).resolves.toEqual(detail);
    expectGet(`${API}/connectors/browser`);
  });
});

describe("patchConnectorConfig", () => {
  it("PATCH /connectors/{id}/config，config 深比较 + 双头", async () => {
    const config = { token: "t-123", retries: 3, nested: { on: true } };
    const result = { ok: true, config, applied: { ok: true, reloaded: true } };
    reply(result);
    await expect(patchConnectorConfig("browser", config)).resolves.toEqual(result);
    expectWrite("PATCH", `${API}/connectors/browser/config`, config);
  });

  it("空配置 {} → body {}（清空覆盖的合法形态）", async () => {
    reply({ ok: true, config: {} });
    await patchConnectorConfig("browser", {});
    expectWrite("PATCH", `${API}/connectors/browser/config`, {});
  });

  it("失败信封 {ok:false, error} 透传不抛错", async () => {
    const result = { ok: false, error: "schema 校验失败" };
    reply(result);
    await expect(patchConnectorConfig("browser", { x: 1 })).resolves.toEqual(result);
  });

  it("id 特殊字符编码", async () => {
    reply({ ok: true });
    await patchConnectorConfig("a b&c", {});
    expectWrite("PATCH", `${API}/connectors/a%20b%26c/config`, {});
  });
});

describe("connectorAction", () => {
  it("缺省 extra={} → body 仅 {action}", async () => {
    reply({ ok: true });
    await expect(connectorAction("browser", "connect")).resolves.toEqual({ ok: true });
    expectWrite("POST", `${API}/connectors/browser/action`, { action: "connect" });
  });

  it("extra 合并进 body（action 在前，extra 字段随后）", async () => {
    reply({ ok: true, tabId: "t-1" });
    await connectorAction("browser", "handoff", { tabId: "t-1", force: true });
    expectWrite("POST", `${API}/connectors/browser/action`, {
      action: "handoff",
      tabId: "t-1",
      force: true,
    });
  });

  it("展开顺序契约：extra 里的同名 action 覆盖第一参数（{action, ...extra}）", async () => {
    reply({ ok: true });
    await connectorAction("browser", "connect", { action: "disconnect" });
    expectWrite("POST", `${API}/connectors/browser/action`, { action: "disconnect" });
  });

  it("id 特殊字符编码", async () => {
    reply({ ok: true });
    await connectorAction("mod/x y", "install");
    expectWrite("POST", `${API}/connectors/mod%2Fx%20y/action`, { action: "install" });
  });
});

describe("浏览器 handoff / pushed-page", () => {
  it("getHandoffStatus：GET /connectors/browser/handoff，active 数组透传", async () => {
    const payload = { active: [{ tabId: "tab-1" }, { tabId: "tab-2" }] };
    reply(payload);
    await expect(getHandoffStatus()).resolves.toEqual(payload);
    expectGet(`${API}/connectors/browser/handoff`);
  });

  it("getHandoffStatus：空 active 数组", async () => {
    reply({ active: [] });
    await expect(getHandoffStatus()).resolves.toEqual({ active: [] });
  });

  it("getPushedPage：GET /connectors/browser/pushed-page，page 对象透传", async () => {
    const page = { url: "https://example.com", title: "Example", favIconUrl: "https://example.com/f.ico", pushedAt: 1_700_000_000 };
    reply({ page });
    await expect(getPushedPage()).resolves.toEqual({ page });
    expectGet(`${API}/connectors/browser/pushed-page`);
  });

  it("getPushedPage：page=null（无推送页）", async () => {
    reply({ page: null });
    await expect(getPushedPage()).resolves.toEqual({ page: null });
    expectGet(`${API}/connectors/browser/pushed-page`);
  });
});

describe("wsConnectorsUrl 三态", () => {
  it("node 无 window → ws://127.0.0.1:8787/api/ws/connectors", () => {
    expect(wsConnectorsUrl()).toBe(`ws://127.0.0.1:${OVERRIDE_PORT ?? 8787}/api/ws/connectors`);
  });

  it("window http → ws://location.host", () => {
    vi.stubGlobal("window", {
      location: { protocol: "http:", hostname: "desk.local", host: "desk.local:3000" },
    });
    const host = OVERRIDE_PORT ? `desk.local:${OVERRIDE_PORT}` : "desk.local:3000";
    expect(wsConnectorsUrl()).toBe(`ws://${host}/api/ws/connectors`);
  });

  it("window https → wss://", () => {
    vi.stubGlobal("window", {
      location: { protocol: "https:", hostname: "app.example.com", host: "app.example.com" },
    });
    const host = OVERRIDE_PORT ? `app.example.com:${OVERRIDE_PORT}` : "app.example.com";
    expect(wsConnectorsUrl()).toBe(`wss://${host}/api/ws/connectors`);
  });

  it("OVERRIDE_PORT 显式覆盖宿主端口（resetModules 重读 env）", async () => {
    const prev = process.env.NEXT_PUBLIC_RUNTIME_PORT;
    process.env.NEXT_PUBLIC_RUNTIME_PORT = "9999";
    vi.resetModules();
    try {
      const mod = await import("./connectors");
      vi.stubGlobal("window", {
        location: { protocol: "http:", hostname: "desk.local", host: "desk.local:3000" },
      });
      expect(mod.wsConnectorsUrl()).toBe("ws://desk.local:9999/api/ws/connectors");
    } finally {
      if (prev === undefined) delete process.env.NEXT_PUBLIC_RUNTIME_PORT;
      else process.env.NEXT_PUBLIC_RUNTIME_PORT = prev;
      vi.resetModules();
    }
  });
});

describe("错误传播", () => {
  it("fetch 网络异常 → Promise reject", async () => {
    fetchMock.mockRejectedValue(new Error("sidecar unreachable"));
    await expect(listConnectors()).rejects.toThrow("sidecar unreachable");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("响应 body 非法 JSON → r.json() 异常传播", async () => {
    fetchMock.mockResolvedValue({
      json: async () => {
        throw new SyntaxError("Unexpected end of JSON input");
      },
    });
    await expect(getConnector("browser")).rejects.toThrow("Unexpected end of JSON input");
  });
});
