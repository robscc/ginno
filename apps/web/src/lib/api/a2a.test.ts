/**
 * api/a2a.ts 单测（remote-session-design.md R3 的 peer 读面）。
 *
 * 打桩策略照 connectors.test.ts：桩 globalThis.fetch，走真实 core.json()，
 * i18n provider 桩成固定 zh-CN。重点：GET URL 契约、脱敏投影透传、
 * 空表/缺字段/网络异常一律回落 []（选择器不许崩）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../i18n/provider", () => ({ currentLocale: () => "zh-CN" }));

import { listA2aPeers } from "./a2a";

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

const PEER = {
  slug: "expense-bot",
  id: "a2a-expense-bot",
  name: "报销助手",
  endpoint: "https://192.168.1.9:8790/a2a",
  enabled: true,
  trust: "readonly",
  caps: ["L0"],
};

describe("listA2aPeers", () => {
  it("GET /a2a/peers，locale 头，peers 数组透传", async () => {
    fetchMock.mockResolvedValue({ json: async () => ({ ok: true, peers: [PEER] }) });
    await expect(listA2aPeers()).resolves.toEqual([PEER]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API}/a2a/peers`);
    expect(init.method ?? "GET").toBe("GET");
    expect(init.body).toBeUndefined();
    expect(init.headers).toEqual({ "X-Ginno-Language": "zh-CN" });
  });

  it("后端读失败契约 {ok:true, peers:[]} → 空表", async () => {
    fetchMock.mockResolvedValue({ json: async () => ({ ok: true, peers: [] }) });
    await expect(listA2aPeers()).resolves.toEqual([]);
  });

  it("响应缺 peers 字段 → 空表（旧 sidecar 兼容）", async () => {
    fetchMock.mockResolvedValue({ json: async () => ({ ok: true }) });
    await expect(listA2aPeers()).resolves.toEqual([]);
  });

  it("网络异常 → 空表，不抛（首页选择器不许崩）", async () => {
    fetchMock.mockRejectedValue(new Error("ECONNREFUSED"));
    await expect(listA2aPeers()).resolves.toEqual([]);
  });

  it("多条 peer 原样透传（含 disabled，过滤是 UI 的事）", async () => {
    const off = { ...PEER, slug: "off-node", enabled: false };
    fetchMock.mockResolvedValue({ json: async () => ({ ok: true, peers: [PEER, off] }) });
    await expect(listA2aPeers()).resolves.toEqual([PEER, off]);
  });
});