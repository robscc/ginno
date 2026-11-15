/**
 * api/misc.ts（用量统计 / debug-log / mod 市场 API 客户端）单元测试。
 *
 * 打桩策略：桩 globalThis.fetch，**不 mock ./core 的 json**——真实走
 * URLSearchParams 条件拼参与 locale 头合并。i18n provider 桩成固定 locale。
 *
 * 重点分支：各 opts 的 falsy 守卫（0/空串会被跳过）、无参时不带 `?`、
 * encodeURIComponent、debugLog 的 best-effort 吞错。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../i18n/provider", () => ({ currentLocale: () => "zh-CN" }));

import {
  debugLog,
  getMarketplaceItem,
  getUsageGrid,
  getUsageOverview,
  getUsageRequests,
  getUsageSessions,
  installFromMarket,
  listMarketplace,
  putMarketplaceSources,
  updateFromMarket,
} from "./misc";
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

function reply(body: unknown) {
  fetchMock.mockResolvedValue({ json: async () => body });
}

/** 断言唯一一次 GET：精确 URL（含 query 形态）+ locale 头 + 无 method/body。 */
function expectGetUrl(url: string) {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [got, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(got).toBe(url);
  expect(init.method ?? "GET").toBe("GET");
  expect(init.body).toBeUndefined();
  expect(init.headers).toEqual({ "X-Ginno-Language": "zh-CN" });
}

/** 断言唯一一次 POST/PUT：URL、method、JSON body 深比较、双头。 */
function expectWrite(method: "POST" | "PUT", path: string, body: unknown) {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toBe(`${API}${path}`);
  expect(init.method).toBe(method);
  expect(typeof init.body).toBe("string");
  expect(JSON.parse(init.body as string)).toEqual(body);
  expect(init.headers).toEqual({
    "X-Ginno-Language": "zh-CN",
    "Content-Type": "application/json",
  });
}

describe("BASE 回落（node 无 window）", () => {
  it("BASE 为 127.0.0.1:8787/api", () => {
    expect(BASE).toBe(API);
  });
});

describe("getUsageOverview", () => {
  it("缺省 days=30", async () => {
    const body = { ok: true, window: { days: 30, from: "a", to: "b" } };
    reply(body);
    await expect(getUsageOverview()).resolves.toEqual(body);
    expectGetUrl(`${API}/usage/overview?days=30`);
  });

  it("显式 days=7 覆盖缺省", async () => {
    reply({ ok: true });
    await getUsageOverview(7);
    expectGetUrl(`${API}/usage/overview?days=7`);
  });
});

describe("getUsageGrid", () => {
  it("不传 opts → URL 不带问号", async () => {
    const body = { ok: true, days: [], grid: [] };
    reply(body);
    await expect(getUsageGrid()).resolves.toEqual(body);
    expectGetUrl(`${API}/usage/grid`);
  });

  it("空 opts 对象 → 同样不带问号", async () => {
    reply({ ok: true });
    await getUsageGrid({});
    expectGetUrl(`${API}/usage/grid`);
  });

  it("只传 from → 仅 from 参数", async () => {
    reply({ ok: true });
    await getUsageGrid({ from: "2026-11-01" });
    expectGetUrl(`${API}/usage/grid?from=2026-11-01`);
  });

  it("只传 to → 仅 to 参数", async () => {
    reply({ ok: true });
    await getUsageGrid({ to: "2026-11-15" });
    expectGetUrl(`${API}/usage/grid?to=2026-11-15`);
  });

  it("from+to 全传 → 按 from,to 顺序拼接", async () => {
    reply({ ok: true });
    await getUsageGrid({ from: "2026-11-01", to: "2026-11-15" });
    expectGetUrl(`${API}/usage/grid?from=2026-11-01&to=2026-11-15`);
  });
});

describe("getUsageSessions", () => {
  it("不传 opts → 裸 URL", async () => {
    const body = { ok: true, sessions: [] };
    reply(body);
    await expect(getUsageSessions()).resolves.toEqual(body);
    expectGetUrl(`${API}/usage/sessions`);
  });

  it("from/to/sort/limit 全传 → 全部进 query，limit 转字符串", async () => {
    reply({ ok: true });
    await getUsageSessions({ from: "2026-11-01", to: "2026-11-15", sort: "tokens", limit: 20 });
    expectGetUrl(`${API}/usage/sessions?from=2026-11-01&to=2026-11-15&sort=tokens&limit=20`);
  });

  it("limit=0 被 falsy 守卫跳过（与 code.searchCode 的 != null 语义不同）", async () => {
    reply({ ok: true });
    await getUsageSessions({ sort: "recent", limit: 0 });
    expectGetUrl(`${API}/usage/sessions?sort=recent`);
  });
});

describe("getUsageRequests", () => {
  it("不传 opts → 裸 URL，响应原样透传", async () => {
    const body = { ok: true, date: "2026-11-15", total: 0, page: 1, page_size: 50, rows: [] };
    reply(body);
    await expect(getUsageRequests()).resolves.toEqual(body);
    expectGetUrl(`${API}/usage/requests`);
  });

  it("七个过滤/分页参数全传 → 全部进 query，数字转字符串", async () => {
    reply({ ok: true });
    await getUsageRequests({
      date: "2026-11-15",
      provider: "glm",
      model: "glm-5.3",
      source: "chat",
      session_id: "s-1",
      page: 2,
      page_size: 100,
    });
    expectGetUrl(
      `${API}/usage/requests?date=2026-11-15&provider=glm&model=glm-5.3&source=chat&session_id=s-1&page=2&page_size=100`,
    );
  });

  it("部分参数（date+provider）→ 只拼传了的", async () => {
    reply({ ok: true });
    await getUsageRequests({ date: "2026-11-15", provider: "glm" });
    expectGetUrl(`${API}/usage/requests?date=2026-11-15&provider=glm`);
  });

  it("page=0 / page_size=0 被 falsy 守卫跳过", async () => {
    reply({ ok: true });
    await getUsageRequests({ page: 0, page_size: 0 });
    expectGetUrl(`${API}/usage/requests`);
  });
});

describe("debugLog（best-effort 遥测）", () => {
  it("POST /debug-log，body 为 payload 的 JSON；直发 fetch 不带 locale 头、不读响应", async () => {
    // 响应对象故意不带 json()：debugLog 若尝试解析响应会当场暴露
    fetchMock.mockResolvedValue({});
    const payload = { event: "drop", files: ["a.png"], nested: { ok: true } };
    await expect(debugLog(payload)).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API}/debug-log`);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual(payload);
    expect(init.headers).toEqual({ "Content-Type": "application/json" });
  });

  it("fetch 抛错 → 静默吞掉，resolve undefined（绝不影响主流程）", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    await expect(debugLog({ event: "x" })).resolves.toBeUndefined();
  });
});

describe("listMarketplace", () => {
  it("缺省不刷新 → 裸 URL，状态原样透传", async () => {
    const body = { ok: true, sources: [], installedOrigins: {} };
    reply(body);
    await expect(listMarketplace()).resolves.toEqual(body);
    expectGetUrl(`${API}/mods/marketplace`);
  });

  it("refresh=true → ?refresh=1", async () => {
    reply({ ok: true, sources: [], installedOrigins: {} });
    await listMarketplace(true);
    expectGetUrl(`${API}/mods/marketplace?refresh=1`);
  });

  it("ok:false 信封也原样透传（misc 客户端不拆信封不抛错）", async () => {
    const body = { ok: false, error: "拉取失败", sources: [], installedOrigins: {} };
    reply(body);
    await expect(listMarketplace()).resolves.toEqual(body);
  });
});

describe("getMarketplaceItem", () => {
  it("src 与 plugin 均 encodeURIComponent（斜杠/空格/& 不破坏 query）", async () => {
    const body = { ok: true, path: "mods/hello", readme: "# hi" };
    reply(body);
    await expect(getMarketplaceItem("team/market", "mods/hello world&x")).resolves.toEqual(body);
    expectGetUrl(
      `${API}/mods/marketplace/item?src=team%2Fmarket&path=mods%2Fhello%20world%26x`,
    );
  });
});

describe("installFromMarket", () => {
  it("缺省 overwrite=false → POST body {src, path, overwrite}", async () => {
    const body = { ok: true, mod: { name: "hello" } };
    reply(body);
    await expect(installFromMarket("team/market", "mods/hello")).resolves.toEqual(body);
    expectWrite("POST", "/mods/marketplace/install", {
      src: "team/market",
      path: "mods/hello",
      overwrite: false,
    });
  });

  it("overwrite=true 显式下发；失败信封原样返回", async () => {
    reply({ ok: false, error: "已安装" });
    await expect(installFromMarket("s", "p", true)).resolves.toEqual({ ok: false, error: "已安装" });
    expectWrite("POST", "/mods/marketplace/install", { src: "s", path: "p", overwrite: true });
  });
});

describe("updateFromMarket", () => {
  it("POST /mods/marketplace/update，body 仅 {name}", async () => {
    const body = { ok: true, mod: { name: "hello", origin: null } };
    reply(body);
    await expect(updateFromMarket("hello")).resolves.toEqual(body);
    expectWrite("POST", "/mods/marketplace/update", { name: "hello" });
  });
});

describe("putMarketplaceSources", () => {
  it("PUT /mods/marketplace/sources，body {sources:[...]} 原样序列化", async () => {
    const sources = [
      { name: "official", repo: "robscc/ginno-mods", enabled: true },
      { name: "team", repo: "team/mods", enabled: false },
    ];
    const body = { ok: true, sources };
    reply(body);
    await expect(putMarketplaceSources(sources)).resolves.toEqual(body);
    expectWrite("PUT", "/mods/marketplace/sources", { sources });
  });

  it("空数组 → body {sources:[]}（清空源列表的合法形态）", async () => {
    reply({ ok: true, sources: [] });
    await putMarketplaceSources([]);
    expectWrite("PUT", "/mods/marketplace/sources", { sources: [] });
  });
});
