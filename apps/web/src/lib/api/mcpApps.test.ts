/**
 * api/mcpApps.ts（MCP Apps sidecar HTTP 封装 + 默认清单 store）单元测试。
 *
 * ⚠️ 本文件测的是 lib/api/mcpApps.ts（80 行 HTTP 封装），不是 lib/mcpApps.ts
 * （纯逻辑 store，已有独立测试）——后者只作为真实依赖被引入，绝不 mock。
 *
 * 打桩策略：桩 globalThis.fetch；getMcpToolsWithUi 走真实 core.json()，
 * getMcpToolUi/mcpAppCall 直发 fetch（桩需带 status/ok/json）。
 *
 * 重点分支：404→null、!ok→throw、403→McpAppPermissionError（含 json 解析
 * 失败兜底）、data.ok=false、r.json().catch(()=>null)、tools 缺省→{}、
 * toolsWithUi 单例 store 的缓存/单飞/失败不落缓存（经真实 fetcher 集成验证）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../i18n/provider", () => ({ currentLocale: () => "zh-CN" }));

import {
  McpAppPermissionError,
  getMcpToolUi,
  getMcpToolsWithUi,
  mcpAppCall,
  toolsWithUi,
} from "./mcpApps";
import { BASE } from "./core";

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

/** 200 + 可解析 body（json() 路径与直发 fetch 路径通用）。 */
function replyJson(body: unknown) {
  fetchMock.mockResolvedValue({ status: 200, ok: true, json: async () => body });
}

/** 指定状态码；jsonThrows 模拟响应体非法 JSON。 */
function replyRaw(status: number, body: unknown, jsonThrows = false) {
  fetchMock.mockResolvedValue({
    status,
    ok: status >= 200 && status < 300,
    json: jsonThrows
      ? async () => {
          throw new SyntaxError("Unexpected end of JSON input");
        }
      : async () => body,
  });
}

function onlyCall() {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  return fetchMock.mock.calls[0] as [string, RequestInit];
}

const UI_PAYLOAD = {
  html: "<div>hi</div>",
  csp: "default-src 'none'",
  permissions: ["clipboard-write"],
  server: "srv",
  tool: "echo",
};

describe("BASE 回落（node 无 window）", () => {
  it("BASE 为 127.0.0.1:8787/api", () => {
    expect(BASE).toBe(API);
  });
});

describe("getMcpToolsWithUi（json() 路径）", () => {
  it("GET /mcp/tools-with-ui，locale 单头，返回 r.tools", async () => {
    const tools = {
      mcp_srv_echo: { server: "srv", tool: "mcp_srv_echo", resourceUri: "ui://srv/echo" },
    };
    replyJson({ tools });
    await expect(getMcpToolsWithUi()).resolves.toEqual(tools);
    const [url, init] = onlyCall();
    expect(url).toBe(`${API}/mcp/tools-with-ui`);
    expect(init.method ?? "GET").toBe("GET");
    expect(init.body).toBeUndefined();
    expect(init.headers).toEqual({ "X-Ginno-Language": "zh-CN" });
  });

  it("响应无 tools 字段 → {}（?? 兜底）", async () => {
    replyJson({});
    await expect(getMcpToolsWithUi()).resolves.toEqual({});
  });

  it("tools=null → {}（?? 兜底，null 不穿透）", async () => {
    replyJson({ tools: null });
    await expect(getMcpToolsWithUi()).resolves.toEqual({});
  });

  it("fetch 网络异常 → Promise reject", async () => {
    fetchMock.mockRejectedValue(new Error("sidecar unreachable"));
    await expect(getMcpToolsWithUi()).rejects.toThrow("sidecar unreachable");
  });
});

describe("getMcpToolUi（直发 fetch，404 语义）", () => {
  it("200 → 载荷透传；server/tool 经 URLSearchParams 编码；locale 单头", async () => {
    replyJson(UI_PAYLOAD);
    await expect(getMcpToolUi("srv one", "t&x")).resolves.toEqual(UI_PAYLOAD);
    const [url, init] = onlyCall();
    expect(url).toBe(`${API}/mcp/ui?server=srv+one&tool=t%26x`);
    expect(Object.fromEntries(new URL(url).searchParams)).toEqual({ server: "srv one", tool: "t&x" });
    expect(init.method ?? "GET").toBe("GET");
    expect(init.body).toBeUndefined();
    expect(init.headers).toEqual({ "X-Ginno-Language": "zh-CN" });
  });

  it("404 → null（无 UI，调用方回退普通渲染）", async () => {
    replyRaw(404, { detail: "not found" });
    await expect(getMcpToolUi("srv", "echo")).resolves.toBeNull();
  });

  it("500 → throw 'mcp ui fetch failed (500)'", async () => {
    replyRaw(500, { detail: "boom" });
    await expect(getMcpToolUi("srv", "echo")).rejects.toThrow("mcp ui fetch failed (500)");
  });

  it("403 也走 !ok 抛错分支（不是 404 就不吞）", async () => {
    replyRaw(403, { detail: "forbidden" });
    await expect(getMcpToolUi("srv", "echo")).rejects.toThrow("mcp ui fetch failed (403)");
  });

  it("fetch 网络异常 → 原样传播", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    await expect(getMcpToolUi("srv", "echo")).rejects.toThrow("offline");
  });
});

describe("mcpAppCall（直发 fetch，权限/错误分支）", () => {
  it("200 ok:true → 载荷透传；POST body {server,tool,arguments,origin} + 双头", async () => {
    const payload = { ok: true, content: ["hello"], structured: { n: 1 } };
    replyJson(payload);
    await expect(mcpAppCall("srv", "echo", { msg: "hi", n: 2 })).resolves.toEqual(payload);
    const [url, init] = onlyCall();
    expect(url).toBe(`${API}/mcp/call`);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      server: "srv",
      tool: "echo",
      arguments: { msg: "hi", n: 2 },
      origin: "mcp-app",
    });
    expect(init.headers).toEqual({
      "Content-Type": "application/json",
      "X-Ginno-Language": "zh-CN",
    });
  });

  it("空 args → arguments:{}（origin 恒为 mcp-app）", async () => {
    replyJson({ ok: true, content: [] });
    await mcpAppCall("srv", "echo", {});
    const [, init] = onlyCall();
    expect(JSON.parse(init.body as string)).toEqual({
      server: "srv",
      tool: "echo",
      arguments: {},
      origin: "mcp-app",
    });
  });

  it("403 + {error,decision} → McpAppPermissionError（message/decision/name）", async () => {
    replyRaw(403, { ok: false, error: "需要用户批准", decision: "ask" });
    const p = mcpAppCall("srv", "echo", {});
    await expect(p).rejects.toBeInstanceOf(McpAppPermissionError);
    await expect(p).rejects.toThrow("需要用户批准");
    await expect(p).rejects.toMatchObject({ name: "McpAppPermissionError", decision: "ask" });
  });

  it("403 + json 解析失败 → 兜底文案 'denied by permission policy'，decision undefined", async () => {
    replyRaw(403, null, true);
    const p = mcpAppCall("srv", "echo", {});
    await expect(p).rejects.toBeInstanceOf(McpAppPermissionError);
    await expect(p).rejects.toThrow("denied by permission policy");
    await expect(p).rejects.toMatchObject({ decision: undefined });
  });

  it("403 + 无 error 字段 → 同样兜底文案", async () => {
    replyRaw(403, { ok: false });
    await expect(mcpAppCall("srv", "echo", {})).rejects.toThrow("denied by permission policy");
  });

  it("500 + {error} → 普通 Error 透出后端文案", async () => {
    replyRaw(500, { ok: false, error: "server crashed" });
    const p = mcpAppCall("srv", "echo", {});
    await expect(p).rejects.toBeInstanceOf(Error);
    await expect(p).rejects.toThrow("server crashed");
    await expect(p).rejects.not.toBeInstanceOf(McpAppPermissionError);
  });

  it("200 但 data.ok=false → Error(data.error)（HTTP 成功≠调用成功）", async () => {
    replyJson({ ok: false, error: "tool execution failed" });
    await expect(mcpAppCall("srv", "echo", {})).rejects.toThrow("tool execution failed");
  });

  it("200 data.ok=false 且无 error → 兜底 'mcp call failed (200)'", async () => {
    replyJson({ ok: false });
    await expect(mcpAppCall("srv", "echo", {})).rejects.toThrow("mcp call failed (200)");
  });

  it("200 但 json 解析失败 → data=null → 'mcp call failed (200)'（catch(()=>null) 分支）", async () => {
    replyRaw(200, null, true);
    await expect(mcpAppCall("srv", "echo", {})).rejects.toThrow("mcp call failed (200)");
  });

  it("fetch 网络异常 → 原样传播", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    await expect(mcpAppCall("srv", "echo", {})).rejects.toThrow("offline");
  });
});

describe("McpAppPermissionError 类契约", () => {
  it("是 Error 子类，name 固定，decision 可选", () => {
    const e = new McpAppPermissionError("denied");
    expect(e).toBeInstanceOf(Error);
    expect(e.name).toBe("McpAppPermissionError");
    expect(e.message).toBe("denied");
    expect(e.decision).toBeUndefined();
    const e2 = new McpAppPermissionError("denied", "deny");
    expect(e2.decision).toBe("deny");
  });
});

/**
 * toolsWithUi 单例 store（模块级实例，fetcher=getMcpToolsWithUi）。
 * 缓存跨用例存续，本 describe 内用例**按声明顺序**构成一条状态链：
 * 未拉取 → 首拉缓存 → 新鲜期命中 → 强制重拉 → 失败不落缓存 → 空清单。
 * （store 纯逻辑的细粒度分支由 lib/mcpApps.test.ts 覆盖，此处只验证
 * api 层组装出的默认实例确实接上了真实 fetch 通道。）
 */
describe("toolsWithUi 默认 store（集成，顺序敏感）", () => {
  const MAP = {
    mcp_srv_echo: { server: "srv", tool: "mcp_srv_echo", resourceUri: "ui://srv/echo" },
  };
  const MAP2 = {
    mcp_srv_ping: { server: "srv", tool: "mcp_srv_ping", resourceUri: "ui://srv/ping" },
  };

  it("首次拉取前：peek()=null、lookup()=null、零请求", () => {
    expect(toolsWithUi.peek()).toBeNull();
    expect(toolsWithUi.lookup("mcp_srv_echo")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("get()：经真实 fetcher 拉清单并缓存；lookup 命中/未命中", async () => {
    replyJson({ tools: MAP });
    await expect(toolsWithUi.get()).resolves.toEqual(MAP);
    const [url, init] = onlyCall();
    expect(url).toBe(`${API}/mcp/tools-with-ui`);
    expect(init.headers).toEqual({ "X-Ginno-Language": "zh-CN" });
    expect(toolsWithUi.peek()).toEqual(MAP);
    expect(toolsWithUi.lookup("mcp_srv_echo")).toEqual(MAP.mcp_srv_echo);
    // 缓存新鲜时 miss 不触发后台 refresh（stale 守卫）
    expect(toolsWithUi.lookup("not_exist")).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("新鲜期内再 get()：直接回缓存，零新请求", async () => {
    await expect(toolsWithUi.get()).resolves.toEqual(MAP);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("refresh()：强制重拉并替换缓存", async () => {
    replyJson({ tools: MAP2 });
    await expect(toolsWithUi.refresh()).resolves.toEqual(MAP2);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(toolsWithUi.peek()).toEqual(MAP2);
    expect(toolsWithUi.lookup("mcp_srv_echo")).toBeNull();
  });

  it("refresh 失败：reject 传播且不落缓存（旧缓存保留）", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    await expect(toolsWithUi.refresh()).rejects.toThrow("offline");
    expect(toolsWithUi.peek()).toEqual(MAP2);
  });

  it("空清单（无 tools 字段）→ {}；短新鲜期内 get() 仍走缓存", async () => {
    replyJson({});
    await expect(toolsWithUi.refresh()).resolves.toEqual({});
    expect(toolsWithUi.peek()).toEqual({});
    await expect(toolsWithUi.get()).resolves.toEqual({});
    // refresh 一次 + get 命中缓存 = 总共 1 次请求
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
