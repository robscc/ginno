/**
 * api/chat.ts（WS 入口 + 外部 agent 委托 API 客户端）单元测试。
 *
 * 打桩策略：桩 globalThis.fetch（走真实 core.json()，不 mock ./core）+
 * 桩 globalThis.WebSocket（FakeWebSocket 记录构造 URL，绝不真连）；
 * i18n provider 桩成固定 zh-CN。node 无 window → BASE/wsBase 回落 127.0.0.1:8787。
 *
 * 重点分支：openSocket 的 ?/& 分隔符与 lang 下发、openSessionSocket/openRunSocket
 * 的 window http/https/OVERRIDE_PORT 三态、stopDelegation 无 body 单头。
 * 本模块无 SSE/AbortSignal（流式在别处），取消语义不适用。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../i18n/provider", () => ({ currentLocale: () => "zh-CN" }));

import {
  getExternalAgents,
  openRunSocket,
  openSessionSocket,
  openSocket,
  stopDelegation,
} from "./chat";
import { BASE, OVERRIDE_PORT } from "./core";

const API = "http://127.0.0.1:8787/api";

/** 记录构造 URL 的 WebSocket 替身（node 环境绝不发起真实连接）。 */
class FakeWebSocket {
  static urls: string[] = [];
  url: string;
  constructor(url: string) {
    this.url = url;
    FakeWebSocket.urls.push(url);
  }
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  FakeWebSocket.urls = [];
  vi.stubGlobal("WebSocket", FakeWebSocket);
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

function stubWindow(location: { protocol: string; hostname: string; host: string }) {
  vi.stubGlobal("window", { location });
}

describe("BASE 回落（node 无 window）", () => {
  it("BASE 为 127.0.0.1:8787/api", () => {
    expect(BASE).toBe(API);
  });
});

describe("openSocket（lang 下发 helper）", () => {
  it("无 query 的 URL → 追加 ?lang=zh-CN，返回构造出的实例", () => {
    const sock = openSocket("ws://127.0.0.1:8787/api/ws/sessions/s1");
    expect(FakeWebSocket.urls).toEqual(["ws://127.0.0.1:8787/api/ws/sessions/s1?lang=zh-CN"]);
    expect(sock).toBeInstanceOf(FakeWebSocket);
    expect(sock.url).toBe("ws://127.0.0.1:8787/api/ws/sessions/s1?lang=zh-CN");
  });

  it("已带 query 的 URL → 用 & 追加 lang（不产生第二个 ?）", () => {
    openSocket("ws://h/api/ws/x?token=abc");
    expect(FakeWebSocket.urls).toEqual(["ws://h/api/ws/x?token=abc&lang=zh-CN"]);
  });

  it("每次调用恰好构造一个 WebSocket", () => {
    openSocket("ws://h/a");
    openSocket("ws://h/b");
    expect(FakeWebSocket.urls).toEqual(["ws://h/a?lang=zh-CN", "ws://h/b?lang=zh-CN"]);
  });
});

describe("openSessionSocket", () => {
  it("node 无 window → ws://127.0.0.1:8787/api/ws/sessions/{id}?lang=", () => {
    openSessionSocket("s-1");
    expect(FakeWebSocket.urls).toEqual([
      `ws://127.0.0.1:${OVERRIDE_PORT ?? 8787}/api/ws/sessions/s-1?lang=zh-CN`,
    ]);
  });

  it("window http → ws://location.host/api/ws/sessions/{id}", () => {
    stubWindow({ protocol: "http:", hostname: "desk.local", host: "desk.local:3000" });
    openSessionSocket("s-2");
    const host = OVERRIDE_PORT ? `desk.local:${OVERRIDE_PORT}` : "desk.local:3000";
    expect(FakeWebSocket.urls).toEqual([`ws://${host}/api/ws/sessions/s-2?lang=zh-CN`]);
  });

  it("window https → wss://（wsBase 协议分支）", () => {
    stubWindow({ protocol: "https:", hostname: "app.example.com", host: "app.example.com" });
    openSessionSocket("s-3");
    const host = OVERRIDE_PORT ? `app.example.com:${OVERRIDE_PORT}` : "app.example.com";
    expect(FakeWebSocket.urls).toEqual([`wss://${host}/api/ws/sessions/s-3?lang=zh-CN`]);
  });
});

describe("openRunSocket", () => {
  it("node 无 window → ws://127.0.0.1:8787/api/ws/runs/{id}?lang=", () => {
    openRunSocket("r-1");
    expect(FakeWebSocket.urls).toEqual([
      `ws://127.0.0.1:${OVERRIDE_PORT ?? 8787}/api/ws/runs/r-1?lang=zh-CN`,
    ]);
  });

  it("window http → ws://location.host/api/ws/runs/{id}", () => {
    stubWindow({ protocol: "http:", hostname: "desk.local", host: "desk.local:3000" });
    openRunSocket("r-2");
    const host = OVERRIDE_PORT ? `desk.local:${OVERRIDE_PORT}` : "desk.local:3000";
    expect(FakeWebSocket.urls).toEqual([`ws://${host}/api/ws/runs/r-2?lang=zh-CN`]);
  });

  it("window https → wss://", () => {
    stubWindow({ protocol: "https:", hostname: "app.example.com", host: "app.example.com" });
    openRunSocket("r-3");
    const host = OVERRIDE_PORT ? `app.example.com:${OVERRIDE_PORT}` : "app.example.com";
    expect(FakeWebSocket.urls).toEqual([`wss://${host}/api/ws/runs/r-3?lang=zh-CN`]);
  });

  it("OVERRIDE_PORT 显式覆盖宿主端口（resetModules 重读 env，run+session 双通道）", async () => {
    const prev = process.env.NEXT_PUBLIC_RUNTIME_PORT;
    process.env.NEXT_PUBLIC_RUNTIME_PORT = "9999";
    vi.resetModules();
    try {
      const mod = await import("./chat");
      stubWindow({ protocol: "https:", hostname: "app.example.com", host: "app.example.com" });
      mod.openRunSocket("r-9");
      mod.openSessionSocket("s-9");
      expect(FakeWebSocket.urls).toEqual([
        "wss://app.example.com:9999/api/ws/runs/r-9?lang=zh-CN",
        "wss://app.example.com:9999/api/ws/sessions/s-9?lang=zh-CN",
      ]);
    } finally {
      if (prev === undefined) delete process.env.NEXT_PUBLIC_RUNTIME_PORT;
      else process.env.NEXT_PUBLIC_RUNTIME_PORT = prev;
      vi.resetModules();
    }
  });
});

describe("stopDelegation / getExternalAgents", () => {
  it("stopDelegation：POST /delegations/{id}/stop，无 body、locale 单头", async () => {
    reply({ ok: true });
    await expect(stopDelegation("d-1")).resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API}/delegations/d-1/stop`);
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
    expect(init.headers).toEqual({ "X-Ginno-Language": "zh-CN" });
  });

  it("stopDelegation：失败信封透传（json() 不抛错）", async () => {
    reply({ ok: false, error: "delegation not found" });
    await expect(stopDelegation("d-x")).resolves.toEqual({ ok: false, error: "delegation not found" });
  });

  it("getExternalAgents：GET /external-agents，数组原样透传", async () => {
    const agents = [
      { name: "claude", installed: true, path: "/usr/local/bin/claude" },
      { name: "codex", installed: false, path: "" },
    ];
    reply(agents);
    await expect(getExternalAgents()).resolves.toEqual(agents);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API}/external-agents`);
    expect(init.method ?? "GET").toBe("GET");
    expect(init.body).toBeUndefined();
    expect(init.headers).toEqual({ "X-Ginno-Language": "zh-CN" });
  });

  it("getExternalAgents：空数组（未安装任何外部 agent）", async () => {
    reply([]);
    await expect(getExternalAgents()).resolves.toEqual([]);
  });
});

describe("错误传播", () => {
  it("fetch 网络异常 → Promise reject", async () => {
    fetchMock.mockRejectedValue(new Error("sidecar unreachable"));
    await expect(getExternalAgents()).rejects.toThrow("sidecar unreachable");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("响应 body 非法 JSON → r.json() 异常传播", async () => {
    fetchMock.mockResolvedValue({
      json: async () => {
        throw new SyntaxError("Unexpected end of JSON input");
      },
    });
    await expect(stopDelegation("d-1")).rejects.toThrow("Unexpected end of JSON input");
  });
});
