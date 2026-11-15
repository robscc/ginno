/**
 * api/knowledge.ts（知识库 / LLMWiki / memory refinery / citations）单元测试。
 *
 * 打桩策略：桩 globalThis.fetch，**不 mock ./core 的 json**——真实走
 * encodeURIComponent / URLSearchParams 条件拼参与 locale 头合并。
 * i18n provider 桩成固定 locale（zh-CN）。
 *
 * 重点分支：可选参数传/不传（agentId/provider/title/removeFromMemory）、
 * 空数组守卫（removeFromMemory?.length）、content !== undefined 与 falsy
 * 守卫的差异（空串 content 必须发送）、无 H 头的 POST/DELETE、
 * openLinkExternal 的 window.open 兜底三路（ok / !ok / reject）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../i18n/provider", () => ({ currentLocale: () => "zh-CN" }));

import { BASE } from "./core";
import {
  applyMemoryDraft,
  applyMemoryDraftById,
  clearMemoryPool,
  deleteExpiredMemoryDrafts,
  discardMemoryDraft,
  discardMemoryDraftById,
  getCitationUsage,
  getMemory,
  getMemoryDraft,
  getMemoryDraftById,
  getMemoryPool,
  getMemoryRuns,
  getMemoryStatus,
  getMemoryTrend,
  getMemoryUsage,
  getWebUsage,
  kbList,
  kbPromoteApply,
  kbPromotePreview,
  kbSearch,
  kbServers,
  kbWikiCreatePage,
  kbWikiDiscover,
  kbWikiList,
  kbWikiOrphans,
  kbWikiPage,
  kbWikiProbe,
  kbWikiPutConfig,
  kbWikiPutPage,
  kbWikiReindex,
  kbWikiRelated,
  kbWikiSearch,
  kbWikiSearchByTag,
  kbWikiStats,
  listMemoryDrafts,
  openExternal,
  openLinkExternal,
  putMemory,
  removeMemorySection,
  retryMemoryRun,
  splitMemoryDraft,
  summarizeMemory,
  testWebSearch,
} from "./knowledge";

const API = "http://127.0.0.1:8787/api";
const LOCALE_HEADER = { "X-Ginno-Language": "zh-CN" };
const WRITE_HEADERS = { ...LOCALE_HEADER, "Content-Type": "application/json" };

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

/** GET + query 精确参数集深比较（URLSearchParams 解析口径）。 */
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

/** 无 body 的写请求（部分 POST/DELETE 不带 H 头——只有 locale 头）。 */
function expectWriteNoBody(method: "POST" | "DELETE", path: string, jsonHeader: boolean) {
  const [url, init] = onlyCall();
  expect(url).toBe(`${API}${path}`);
  expect(init.method).toBe(method);
  expect(init.body).toBeUndefined();
  expect(init.headers).toEqual(jsonHeader ? WRITE_HEADERS : LOCALE_HEADER);
}

/** 冲刷 openLinkExternal 的 fire-and-forget Promise 链（全微任务）。 */
const flush = () => new Promise((r) => setTimeout(r, 0));

describe("BASE 回落（node 无 window）", () => {
  it("BASE 为 127.0.0.1:8787/api", () => {
    expect(BASE).toBe(API);
  });
});

describe("kb 基础查询", () => {
  it("kbServers：GET /kb/servers，空数组透传", async () => {
    reply([]);
    await expect(kbServers()).resolves.toEqual([]);
    expectGet(`${API}/kb/servers`);
  });

  it("kbSearch：q 进 query", async () => {
    const body = { q: "agent", results: ["a.md"] };
    reply(body);
    await expect(kbSearch("agent")).resolves.toEqual(body);
    expectGetQuery(`${API}/kb/search?q=agent`, { q: "agent" });
  });

  it("kbSearch：特殊字符 encodeURIComponent（空格/&/中文）", async () => {
    reply({ q: "股市 & AI", results: [] });
    await kbSearch("股市 & AI");
    expectGet(`${API}/kb/search?q=${encodeURIComponent("股市 & AI")}`);
    const [url] = onlyCall();
    expect(Object.fromEntries(new URL(url).searchParams)).toEqual({ q: "股市 & AI" });
  });

  it("kbList：缺省 path='' 仍拼 ?path=（空串不是缺省）", async () => {
    reply({ path: "", results: [] });
    await kbList();
    expectGetQuery(`${API}/kb/list?path=`, { path: "" });
  });

  it("kbList：显式 path 编码斜杠", async () => {
    reply({ path: "Ginno/Raw", results: ["x.md"] });
    await kbList("Ginno/Raw");
    expectGet(`${API}/kb/list?path=Ginno%2FRaw`);
  });
});

describe("wiki 检索 / 列表 / 统计", () => {
  it("kbWikiSearch：q 参数 + 结果透传", async () => {
    const body = { ok: true, results: [{ path: "p", title: "t", score: 1 }] };
    reply(body);
    await expect(kbWikiSearch("t")).resolves.toEqual(body);
    expectGetQuery(`${API}/kb/wiki/search?q=t`, { q: "t" });
  });

  it("kbWikiSearch：error 响应原样透传（json 不检查 ok）", async () => {
    const body = { ok: false, error: "vault not configured", results: [] };
    reply(body);
    await expect(kbWikiSearch("t")).resolves.toEqual(body);
  });

  it("kbWikiSearchByTag：tag 参数（与 q 互斥的独立入口）", async () => {
    reply({ ok: true, results: [] });
    await kbWikiSearchByTag("ai-agent");
    expectGetQuery(`${API}/kb/wiki/search?tag=ai-agent`, { tag: "ai-agent" });
  });

  it("kbWikiList：GET /kb/wiki/list", async () => {
    const body = { ok: true, pages: [] };
    reply(body);
    await expect(kbWikiList()).resolves.toEqual(body);
    expectGet(`${API}/kb/wiki/list`);
  });

  it("kbWikiStats：GET /kb/wiki/stats", async () => {
    const body = { pages: 3, tags: 2 };
    reply(body);
    await expect(kbWikiStats()).resolves.toEqual(body);
    expectGet(`${API}/kb/wiki/stats`);
  });

  it("kbWikiDiscover：GET /kb/wiki/discover", async () => {
    const body = { ok: true };
    reply(body);
    await expect(kbWikiDiscover()).resolves.toEqual(body);
    expectGet(`${API}/kb/wiki/discover`);
  });

  it("kbWikiOrphans：GET /kb/wiki/orphans", async () => {
    const body = { ok: true, pages: [] };
    reply(body);
    await expect(kbWikiOrphans()).resolves.toEqual(body);
    expectGet(`${API}/kb/wiki/orphans`);
  });

  it("kbWikiReindex：POST /kb/wiki/index，无 body 且不带 Content-Type 头", async () => {
    const body = { ok: true, indexed: 5, tags: ["a"] };
    reply(body);
    await expect(kbWikiReindex()).resolves.toEqual(body);
    expectWriteNoBody("POST", "/kb/wiki/index", false);
  });
});

describe("kbWikiRelated", () => {
  it("缺省 top_k=10", async () => {
    reply({ ok: true, related: [], clusters: [] });
    await kbWikiRelated("标题");
    expectGet(`${API}/kb/wiki/related?title=${encodeURIComponent("标题")}&top_k=10`);
  });

  it("显式 top_k 覆盖缺省", async () => {
    reply({ ok: true, related: [], clusters: [] });
    await kbWikiRelated("t", 3);
    expectGetQuery(`${API}/kb/wiki/related?title=t&top_k=3`, { title: "t", top_k: "3" });
  });
});

describe("kbWikiPage 条件拼参", () => {
  it("双缺省：URL 仍带 '?'（URLSearchParams 空串）", async () => {
    reply({ ok: true });
    await kbWikiPage();
    expectGet(`${API}/kb/wiki/page?`);
  });

  it("只传 path", async () => {
    reply({ ok: true });
    await kbWikiPage("Ginno/Wiki/x.md");
    expectGetQuery(`${API}/kb/wiki/page?path=Ginno%2FWiki%2Fx.md`, { path: "Ginno/Wiki/x.md" });
  });

  it("只传 title", async () => {
    reply({ ok: true });
    await kbWikiPage("", "标题");
    expectGetQuery(`${API}/kb/wiki/page?title=${encodeURIComponent("标题")}`, { title: "标题" });
  });

  it("path + title 同传：path 在前", async () => {
    reply({ ok: true });
    await kbWikiPage("a.md", "t");
    const [url] = onlyCall();
    expect(url.startsWith(`${API}/kb/wiki/page?`)).toBe(true);
    expect(Object.fromEntries(new URL(url).searchParams)).toEqual({ path: "a.md", title: "t" });
    expect(url).toBe(`${API}/kb/wiki/page?path=a.md&title=t`);
  });
});

describe("wiki 写操作", () => {
  it("kbWikiPutPage：PUT /kb/wiki/page，body {path, raw}", async () => {
    const body = { ok: true, path: "a.md" };
    reply(body);
    await expect(kbWikiPutPage("a.md", "# raw")).resolves.toEqual(body);
    expectWrite("PUT", "/kb/wiki/page", { path: "a.md", raw: "# raw" });
  });

  it("kbWikiCreatePage：POST /kb/wiki/page，body {path, raw}", async () => {
    reply({ ok: true, path: "b.md" });
    await kbWikiCreatePage("b.md", "content");
    expectWrite("POST", "/kb/wiki/page", { path: "b.md", raw: "content" });
  });

  it("kbWikiPutPage：error 响应透传", async () => {
    const body = { ok: false, error: "path outside vault" };
    reply(body);
    await expect(kbWikiPutPage("../evil", "x")).resolves.toEqual(body);
  });

  it("kbWikiPutConfig：PUT /kb/wiki/config，data 原样作 body", async () => {
    reply({ ok: true });
    const cfg = { vault_path: "/v", wiki_dir: "Wiki", enabled: true };
    await kbWikiPutConfig(cfg);
    expectWrite("PUT", "/kb/wiki/config", cfg);
  });

  it("kbWikiProbe：path 编码进 query", async () => {
    const body = { ok: true, vault_path: "/v", wiki_pages: 2 };
    reply(body);
    await expect(kbWikiProbe("/Users/x/vault")).resolves.toEqual(body);
    expectGet(`${API}/kb/wiki/probe?path=${encodeURIComponent("/Users/x/vault")}`);
  });
});

describe("memory 读取", () => {
  it("getMemory：不传 agentId → 无 query", async () => {
    const body = { ok: true, content: "m", pool_count: 0, draft_pending: false, drafts_pending: [], kb_usable: true, status: {} };
    reply(body);
    await expect(getMemory()).resolves.toEqual(body);
    expectGet(`${API}/memory`);
  });

  it("getMemory：传 agentId → ?agent_id=", async () => {
    reply({ ok: true });
    await getMemory("dev-agent");
    expectGetQuery(`${API}/memory?agent_id=dev-agent`, { agent_id: "dev-agent" });
  });

  it("getMemory：agentId 特殊字符编码", async () => {
    reply({ ok: true });
    await getMemory("a b&c");
    expectGet(`${API}/memory?agent_id=a%20b%26c`);
  });

  it("getMemoryStatus：不传 agentId → 无 query", async () => {
    reply({ ok: true, chars: 10 });
    await getMemoryStatus();
    expectGet(`${API}/memory/status`);
  });

  it("getMemoryStatus：传 agentId", async () => {
    reply({ ok: true });
    await getMemoryStatus("dev");
    expectGetQuery(`${API}/memory/status?agent_id=dev`, { agent_id: "dev" });
  });

  it("getMemoryDraft：GET /memory/draft", async () => {
    const body = { ok: true, content: "draft" };
    reply(body);
    await expect(getMemoryDraft()).resolves.toEqual(body);
    expectGet(`${API}/memory/draft`);
  });

  it("listMemoryDrafts：缺省 limit=50，无 status", async () => {
    reply({ ok: true, drafts: [] });
    await listMemoryDrafts();
    expectGetQuery(`${API}/memory/drafts?limit=50`, { limit: "50" });
  });

  it("listMemoryDrafts：status 追加在 limit 之后", async () => {
    reply({ ok: true, drafts: [] });
    await listMemoryDrafts("pending");
    expectGetQuery(`${API}/memory/drafts?limit=50&status=pending`, { limit: "50", status: "pending" });
  });

  it("listMemoryDrafts：自定义 limit", async () => {
    reply({ ok: true, drafts: [] });
    await listMemoryDrafts("applied", 5);
    expectGetQuery(`${API}/memory/drafts?limit=5&status=applied`, { limit: "5", status: "applied" });
  });

  it("listMemoryDrafts：status='' 被 falsy 守卫跳过", async () => {
    reply({ ok: true, drafts: [] });
    await listMemoryDrafts("");
    expectGetQuery(`${API}/memory/drafts?limit=50`, { limit: "50" });
  });

  it("getMemoryDraftById：id 进路径", async () => {
    const body = { ok: true, id: "d1", content: "c" };
    reply(body);
    await expect(getMemoryDraftById("d1")).resolves.toEqual(body);
    expectGet(`${API}/memory/drafts/d1`);
  });

  it("getMemoryDraftById：id 含斜杠须编码（防路径穿越形态）", async () => {
    reply({ ok: true });
    await getMemoryDraftById("d/1");
    expectGet(`${API}/memory/drafts/d%2F1`);
  });

  it("getMemoryPool：缺省 limit=20 offset=0", async () => {
    reply({ ok: true, items: [], total: 0 });
    await getMemoryPool();
    expectGetQuery(`${API}/memory/pool?limit=20&offset=0`, { limit: "20", offset: "0" });
  });

  it("getMemoryPool：自定义分页", async () => {
    reply({ ok: true, items: [], total: 0 });
    await getMemoryPool(5, 40);
    expectGetQuery(`${API}/memory/pool?limit=5&offset=40`, { limit: "5", offset: "40" });
  });

  it("getMemoryRuns：缺省 limit=50", async () => {
    reply({ ok: true, runs: [] });
    await getMemoryRuns();
    expectGetQuery(`${API}/memory/runs?limit=50`, { limit: "50" });
  });

  it("getMemoryRuns：自定义 limit", async () => {
    reply({ ok: true, runs: [] });
    await getMemoryRuns(3);
    expectGetQuery(`${API}/memory/runs?limit=3`, { limit: "3" });
  });

  it("getMemoryTrend：缺省 days=30", async () => {
    reply({ ok: true, days: 30, trend: [] });
    await getMemoryTrend();
    expectGetQuery(`${API}/memory/trend?days=30`, { days: "30" });
  });

  it("getMemoryTrend：自定义 days", async () => {
    reply({ ok: true, days: 7, trend: [] });
    await getMemoryTrend(7);
    expectGetQuery(`${API}/memory/trend?days=7`, { days: "7" });
  });

  it("getMemoryUsage：缺省 days=7", async () => {
    reply({ ok: true });
    await getMemoryUsage();
    expectGetQuery(`${API}/memory/usage?days=7`, { days: "7" });
  });

  it("getMemoryUsage：自定义 days", async () => {
    reply({ ok: true });
    await getMemoryUsage(14);
    expectGetQuery(`${API}/memory/usage?days=14`, { days: "14" });
  });
});

describe("memory 写操作", () => {
  it("putMemory：不传 agentId → body 只有 content", async () => {
    reply({ ok: true, status: {} });
    await putMemory("new memory");
    expectWrite("PUT", "/memory", { content: "new memory" });
  });

  it("putMemory：传 agentId → body 追加 agent_id", async () => {
    reply({ ok: true, status: {} });
    await putMemory("m", "dev");
    expectWrite("PUT", "/memory", { content: "m", agent_id: "dev" });
  });

  it("removeMemorySection：lines 数组 + 无 agentId", async () => {
    reply({ ok: true, removed: 2, status: {} });
    await removeMemorySection(["l1", "l2"]);
    expectWrite("POST", "/memory/sections/remove", { lines: ["l1", "l2"] });
  });

  it("removeMemorySection：带 agentId", async () => {
    reply({ ok: true, removed: 1, status: {} });
    await removeMemorySection(["l1"], "dev");
    expectWrite("POST", "/memory/sections/remove", { lines: ["l1"], agent_id: "dev" });
  });

  it("removeMemorySection：空数组原样发送（不做 falsy 守卫）", async () => {
    reply({ ok: true, removed: 0, status: {} });
    await removeMemorySection([]);
    expectWrite("POST", "/memory/sections/remove", { lines: [] });
  });

  it("applyMemoryDraftById：缺省 opts → 空 body {}", async () => {
    reply({ ok: true, chars: 10 });
    await applyMemoryDraftById("d1");
    expectWrite("POST", "/memory/drafts/d1/apply", {});
  });

  it("applyMemoryDraftById：只传 content", async () => {
    reply({ ok: true });
    await applyMemoryDraftById("d1", { content: "edited" });
    expectWrite("POST", "/memory/drafts/d1/apply", { content: "edited" });
  });

  it("applyMemoryDraftById：content='' 必须发送（!== undefined 判断）", async () => {
    reply({ ok: true });
    await applyMemoryDraftById("d1", { content: "" });
    expectWrite("POST", "/memory/drafts/d1/apply", { content: "" });
  });

  it("applyMemoryDraftById：force/truncate 为 false 时被跳过", async () => {
    reply({ ok: true });
    await applyMemoryDraftById("d1", { force: false, truncate: false });
    expectWrite("POST", "/memory/drafts/d1/apply", {});
  });

  it("applyMemoryDraftById：只传 force", async () => {
    reply({ ok: true });
    await applyMemoryDraftById("d1", { force: true });
    expectWrite("POST", "/memory/drafts/d1/apply", { force: true });
  });

  it("applyMemoryDraftById：只传 truncate", async () => {
    reply({ ok: true });
    await applyMemoryDraftById("d1", { truncate: true });
    expectWrite("POST", "/memory/drafts/d1/apply", { truncate: true });
  });

  it("applyMemoryDraftById：三参全传 + id 编码", async () => {
    reply({ ok: true });
    await applyMemoryDraftById("d/1", { content: "c", force: true, truncate: true });
    expectWrite("POST", "/memory/drafts/d%2F1/apply", { content: "c", force: true, truncate: true });
  });

  it("applyMemoryDraftById：error 响应透传（预算不足等）", async () => {
    const body = { ok: false, error: "budget exceeded", budget: 100 };
    reply(body);
    await expect(applyMemoryDraftById("d1")).resolves.toEqual(body);
  });

  it("discardMemoryDraftById：POST 无 body、无 Content-Type 头", async () => {
    reply({ ok: true, had_draft: true });
    await discardMemoryDraftById("d1");
    expectWriteNoBody("POST", "/memory/drafts/d1/discard", false);
  });

  it("deleteExpiredMemoryDrafts：DELETE 无 body、无 Content-Type 头", async () => {
    const body = { ok: true, removed: ["d1", "d2"] };
    reply(body);
    await expect(deleteExpiredMemoryDrafts()).resolves.toEqual(body);
    expectWriteNoBody("DELETE", "/memory/drafts/expired", false);
  });

  it("clearMemoryPool：DELETE 带 body {confirm:true} + 双头", async () => {
    reply({ ok: true, removed: 3 });
    await clearMemoryPool();
    expectWrite("DELETE", "/memory/pool", { confirm: true });
  });

  it("retryMemoryRun：POST 无 body、无 Content-Type 头，runId 编码", async () => {
    reply({ ok: true, retried_from: "r0" });
    await retryMemoryRun("r/1");
    expectWriteNoBody("POST", "/memory/runs/r%2F1/retry", false);
  });

  it("splitMemoryDraft：body {keep}", async () => {
    reply({ ok: true, id: "d1" });
    await splitMemoryDraft("d1", ["l1", "l2"]);
    expectWrite("POST", "/memory/drafts/d1/split", { keep: ["l1", "l2"] });
  });

  it("splitMemoryDraft：keep 空数组原样发送", async () => {
    reply({ ok: true });
    await splitMemoryDraft("d1", []);
    expectWrite("POST", "/memory/drafts/d1/split", { keep: [] });
  });

  it("summarizeMemory：不传 provider → 空 body {}", async () => {
    reply({ ok: true, draft_exists: true });
    await summarizeMemory();
    expectWrite("POST", "/memory/summarize", {});
  });

  it("summarizeMemory：传 provider", async () => {
    reply({ ok: true });
    await summarizeMemory("glm");
    expectWrite("POST", "/memory/summarize", { provider: "glm" });
  });

  it("applyMemoryDraft：双缺省 → 空 body {}", async () => {
    reply({ ok: true });
    await applyMemoryDraft();
    expectWrite("POST", "/memory/draft/apply", {});
  });

  it("applyMemoryDraft：只传 content", async () => {
    reply({ ok: true });
    await applyMemoryDraft("edited");
    expectWrite("POST", "/memory/draft/apply", { content: "edited" });
  });

  it("applyMemoryDraft：content='' 必须发送（!== undefined 判断）", async () => {
    reply({ ok: true });
    await applyMemoryDraft("");
    expectWrite("POST", "/memory/draft/apply", { content: "" });
  });

  it("applyMemoryDraft：只传 force=true", async () => {
    reply({ ok: true });
    await applyMemoryDraft(undefined, true);
    expectWrite("POST", "/memory/draft/apply", { force: true });
  });

  it("applyMemoryDraft：content + force 同传", async () => {
    reply({ ok: true });
    await applyMemoryDraft("c", true);
    expectWrite("POST", "/memory/draft/apply", { content: "c", force: true });
  });

  it("discardMemoryDraft：POST 无 body、无 Content-Type 头", async () => {
    reply({ ok: true });
    await discardMemoryDraft();
    expectWriteNoBody("POST", "/memory/draft/discard", false);
  });
});

describe("KB promote（记忆 → wiki 晋升）", () => {
  it("kbPromotePreview：不传 title → body 只有 text", async () => {
    reply({ ok: true, path: "Wiki/x.md" });
    await kbPromotePreview("some text");
    expectWrite("POST", "/kb/wiki/promote/preview", { text: "some text" });
  });

  it("kbPromotePreview：传 title", async () => {
    reply({ ok: true });
    await kbPromotePreview("text", "标题");
    expectWrite("POST", "/kb/wiki/promote/preview", { text: "text", title: "标题" });
  });

  it("kbPromotePreview：title='' 被 falsy 守卫跳过", async () => {
    reply({ ok: true });
    await kbPromotePreview("text", "");
    expectWrite("POST", "/kb/wiki/promote/preview", { text: "text" });
  });

  it("kbPromoteApply：不传 removeFromMemory → body 只有 path/raw", async () => {
    reply({ ok: true, path: "Wiki/x.md" });
    await kbPromoteApply("Wiki/x.md", "# raw");
    expectWrite("POST", "/kb/wiki/promote/apply", { path: "Wiki/x.md", raw: "# raw" });
  });

  it("kbPromoteApply：空数组被 ?.length 守卫跳过", async () => {
    reply({ ok: true });
    await kbPromoteApply("Wiki/x.md", "raw", []);
    expectWrite("POST", "/kb/wiki/promote/apply", { path: "Wiki/x.md", raw: "raw" });
  });

  it("kbPromoteApply：非空数组 → remove_from_memory", async () => {
    const body = { ok: true, removed_from_memory: 2 };
    reply(body);
    await expect(kbPromoteApply("Wiki/x.md", "raw", ["l1", "l2"])).resolves.toEqual(body);
    expectWrite("POST", "/kb/wiki/promote/apply", {
      path: "Wiki/x.md",
      raw: "raw",
      remove_from_memory: ["l1", "l2"],
    });
  });
});

describe("citations / web sources", () => {
  it("openExternal：POST /open-external，body {url}", async () => {
    reply({ ok: true });
    await openExternal("https://example.com");
    expectWrite("POST", "/open-external", { url: "https://example.com" });
  });

  it("getCitationUsage：缺省 sort=cited limit=20", async () => {
    reply({ ok: true, rows: [] });
    await getCitationUsage();
    expectGetQuery(`${API}/kb/wiki/usage?sort=cited&limit=20`, { sort: "cited", limit: "20" });
  });

  it("getCitationUsage：自定义 sort（编码）+ limit", async () => {
    reply({ ok: true, rows: [] });
    await getCitationUsage("last used", 5);
    expectGet(`${API}/kb/wiki/usage?sort=last%20used&limit=5`);
  });

  it("getWebUsage：GET /kb/wiki/web-usage，统计透传", async () => {
    const body = { ok: true, engines: [], top_domains: [], total_searches: 0, total_cited: 0 };
    reply(body);
    await expect(getWebUsage()).resolves.toEqual(body);
    expectGet(`${API}/kb/wiki/web-usage`);
  });

  it("testWebSearch：POST /web/test-search，body {engine}", async () => {
    const body = { ok: true, results: 3 };
    reply(body);
    await expect(testWebSearch("duckduckgo")).resolves.toEqual(body);
    expectWrite("POST", "/web/test-search", { engine: "duckduckgo" });
  });
});

describe("openLinkExternal（window.open 兜底三路）", () => {
  it("sidecar ok=true：不走 window.open", async () => {
    const open = vi.fn();
    vi.stubGlobal("window", { open });
    reply({ ok: true });
    openLinkExternal("https://example.com");
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API}/open-external`);
    expect(JSON.parse(init.body as string)).toEqual({ url: "https://example.com" });
    expect(open).not.toHaveBeenCalled();
  });

  it("sidecar ok=false：兜底 window.open(url, _blank, noopener)", async () => {
    const open = vi.fn();
    vi.stubGlobal("window", { open });
    reply({ ok: false, error: "blocked host" });
    openLinkExternal("https://example.com");
    await flush();
    expect(open).toHaveBeenCalledTimes(1);
    expect(open).toHaveBeenCalledWith("https://example.com", "_blank", "noopener");
  });

  it("fetch reject（sidecar 挂了）：catch 兜底 window.open", async () => {
    const open = vi.fn();
    vi.stubGlobal("window", { open });
    fetchMock.mockRejectedValue(new Error("sidecar down"));
    openLinkExternal("https://example.com");
    await flush();
    expect(open).toHaveBeenCalledTimes(1);
    expect(open).toHaveBeenCalledWith("https://example.com", "_blank", "noopener");
  });
});

describe("错误传播", () => {
  it("fetch 网络异常 → Promise reject", async () => {
    fetchMock.mockRejectedValue(new Error("sidecar unreachable"));
    await expect(kbServers()).rejects.toThrow("sidecar unreachable");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("响应 body 非法 JSON → r.json() 异常传播", async () => {
    fetchMock.mockResolvedValue({
      json: async () => {
        throw new SyntaxError("Unexpected end of JSON input");
      },
    });
    await expect(getMemory()).rejects.toThrow("Unexpected end of JSON input");
  });
});
