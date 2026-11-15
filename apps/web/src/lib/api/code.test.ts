/**
 * api/code.ts（代码面板 API 客户端）单元测试。
 *
 * 打桩策略：桩 globalThis.fetch，**不 mock ./core 的 json**——让真实的
 * URL 拼接 / URLSearchParams 序列化 / locale 头合并逻辑全部走到。
 * i18n provider 是 React 组件模块（拉 next-intl/runtime 全家桶），按约定
 * vi.mock 桩成固定 locale，顺带让 X-Ginno-Language 头可断言。
 *
 * node 环境无 window，core.BASE 回落 http://127.0.0.1:8787/api。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../i18n/provider", () => ({ currentLocale: () => "zh-CN" }));

import {
  CodeApiError,
  CodeConflictError,
  codeDelete,
  codeDiff,
  codeFileUrl,
  codeMkdir,
  codeMove,
  codeRawUrl,
  codeRename,
  getCodeGit,
  listCodeDir,
  listCodeRoots,
  readCodeFile,
  searchCode,
  writeCodeFile,
} from "./code";
import { BASE } from "./core";

const API = "http://127.0.0.1:8787/api";
const P = "default"; // project_slug
const S = "sess-1"; // session_id
const R = "root-a"; // root id

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

/** 让下一次 fetch 返回给定 JSON body（HTTP 200 信封）。 */
function reply(body: unknown) {
  fetchMock.mockResolvedValue({ json: async () => body });
}

/** 断言唯一一次 GET 请求：完整路径 + 精确 query 参数集 + locale 头。 */
function expectGet(path: string, params: Record<string, string>) {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  const u = new URL(url);
  expect(`${u.origin}${u.pathname}`).toBe(`${API}${path}`);
  expect(Object.fromEntries(u.searchParams)).toEqual(params);
  expect(init.method ?? "GET").toBe("GET");
  expect(init.headers).toEqual({ "X-Ginno-Language": "zh-CN" });
}

/** 断言唯一一次 POST/PUT 请求：路径、method、JSON body（解析后深比较）、双头。 */
function expectWrite(method: "POST" | "PUT", path: string, body: Record<string, unknown>) {
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
  it("BASE 为 127.0.0.1:8787/api，后续 URL 断言以此为基准", () => {
    expect(BASE).toBe(API);
  });
});

describe("listCodeRoots", () => {
  it("成功：GET /code/roots 带 project_slug/session_id，原样返回 roots", async () => {
    const roots = [
      { id: "root-a", name: "ginno", path: "/w/ginno", access: "rw", missing: false, is_repo: true, branch: "main" },
    ];
    reply({ ok: true, roots });
    await expect(listCodeRoots(P, S)).resolves.toEqual(roots);
    expectGet("/code/roots", { project_slug: P, session_id: S });
  });

  it("ok:false 带 code/message → 抛 CodeApiError 携带服务端码与文案", async () => {
    reply({ ok: false, code: "root-missing", message: "根目录不存在" });
    const err = await listCodeRoots(P, S).catch((e) => e);
    expect(err).toBeInstanceOf(CodeApiError);
    expect(err.code).toBe("root-missing");
    expect(err.message).toBe("根目录不存在");
    expect(err.name).toBe("CodeApiError");
  });

  it("ok:false 缺 code/message → 回落 unknown-root，message 回落为 code", async () => {
    reply({ ok: false });
    const err = await listCodeRoots(P, S).catch((e) => e);
    expect(err).toBeInstanceOf(CodeApiError);
    expect(err.code).toBe("unknown-root");
    expect(err.message).toBe("unknown-root"); // CodeApiError: super(message || code)
  });

  it("ok:true 但缺 roots 字段 → 同样按 unknown-root 抛错", async () => {
    reply({ ok: true });
    await expect(listCodeRoots(P, S)).rejects.toMatchObject({
      name: "CodeApiError",
      code: "unknown-root",
    });
  });
});

describe("listCodeDir", () => {
  it("成功：root/path 进 query（path 空串=根本身），字段原样透传", async () => {
    const entries = [
      { name: "src", type: "dir", size: null, mtime: 1, hidden: false, hidden_count: 12 },
    ];
    reply({ ok: true, root: R, path: "", entries, truncated: true });
    await expect(listCodeDir(P, S, R, "")).resolves.toEqual({
      root: R,
      path: "",
      entries,
      truncated: true,
    });
    expectGet("/code/list", { project_slug: P, session_id: S, root: R, path: "" });
  });

  it("path 含子目录与特殊字符 → URLSearchParams 正确编码", async () => {
    reply({ ok: true, entries: [] });
    await listCodeDir(P, S, R, "src/子目录/a b.ts");
    const [url] = fetchMock.mock.calls[0] as [string];
    const u = new URL(url);
    expect(u.searchParams.get("path")).toBe("src/子目录/a b.ts");
    expect(u.search).toContain("path=src%2F%E5%AD%90%E7%9B%AE%E5%BD%95%2Fa+b.ts");
  });

  it("ok:true 极简响应 → root/path 回落入参，entries 空数组，truncated false", async () => {
    reply({ ok: true });
    await expect(listCodeDir(P, S, R, "sub")).resolves.toEqual({
      root: R,
      path: "sub",
      entries: [],
      truncated: false,
    });
  });

  it("ok:false（not-directory）→ 抛 CodeApiError", async () => {
    reply({ ok: false, code: "not-directory", message: "不是目录" });
    await expect(listCodeDir(P, S, R, "f.ts")).rejects.toMatchObject({
      name: "CodeApiError",
      code: "not-directory",
      message: "不是目录",
    });
  });

  it("ok:false 缺 code/message → 回落 unknown-root", async () => {
    reply({ ok: false });
    await expect(listCodeDir(P, S, R, "f.ts")).rejects.toMatchObject({
      name: "CodeApiError",
      code: "unknown-root",
      message: "unknown-root",
    });
  });
});

describe("readCodeFile", () => {
  const full = {
    ok: true,
    root: R,
    path: "a.ts",
    version: "v1",
    encoding: "utf-8",
    language: "typescript",
    editable: true,
    readonly_reason: null,
    text: "let x = 1;",
    eof: true,
  };

  it("不传 rev → query 无 rev 参数；完整响应原样返回", async () => {
    reply(full);
    await expect(readCodeFile(P, S, R, "a.ts")).resolves.toEqual({
      root: R,
      path: "a.ts",
      version: "v1",
      encoding: "utf-8",
      language: "typescript",
      editable: true,
      readonly_reason: null,
      text: "let x = 1;",
      eof: true,
    });
    expectGet("/code/read", { project_slug: P, session_id: S, root: R, path: "a.ts" });
  });

  it('rev="HEAD" → query 追加 rev=HEAD', async () => {
    reply(full);
    await readCodeFile(P, S, R, "a.ts", "HEAD");
    expectGet("/code/read", { project_slug: P, session_id: S, root: R, path: "a.ts", rev: "HEAD" });
  });

  it("ok:true 极简响应 → 全默认值（utf-8/plaintext/不可编辑/eof true）", async () => {
    reply({ ok: true });
    await expect(readCodeFile(P, S, R, "a.ts")).resolves.toEqual({
      root: R,
      path: "a.ts",
      version: "",
      encoding: "utf-8",
      language: "plaintext",
      editable: false,
      readonly_reason: null,
      text: null,
      eof: true,
    });
  });

  it("降级（binary + editable:false + eof:false）是 ok:true 正常答案，不抛错", async () => {
    reply({ ok: true, editable: false, readonly_reason: "binary", text: null, eof: false });
    const r = await readCodeFile(P, S, R, "img.png");
    expect(r.editable).toBe(false);
    expect(r.readonly_reason).toBe("binary");
    expect(r.eof).toBe(false); // eof !== false 才为 true
  });

  it("ok:false（denied-path）→ 抛 CodeApiError", async () => {
    reply({ ok: false, code: "denied-path", message: "路径被拒绝" });
    await expect(readCodeFile(P, S, R, "../secret")).rejects.toMatchObject({
      name: "CodeApiError",
      code: "denied-path",
    });
  });

  it("ok:false 缺 code/message → 回落 unknown-root", async () => {
    reply({ ok: false });
    await expect(readCodeFile(P, S, R, "x")).rejects.toMatchObject({
      name: "CodeApiError",
      code: "unknown-root",
      message: "unknown-root",
    });
  });
});

describe("codeRawUrl（纯 URL 构造，不发请求）", () => {
  it("不带 version → 无 v 参数；特殊字符按 URLSearchParams 编码", () => {
    expect(codeRawUrl(P, S, R, "img/a b.png")).toBe(
      `${API}/code/raw?project_slug=${P}&session_id=${S}&root=${R}&path=img%2Fa+b.png`,
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("带 version → 追加 v 参数用于缓存击穿", () => {
    expect(codeRawUrl(P, S, R, "img/a.png", "v2")).toBe(
      `${API}/code/raw?project_slug=${P}&session_id=${S}&root=${R}&path=img%2Fa.png&v=v2`,
    );
  });
});

describe("codeFileUrl（路径段形态，供 iframe 相对资源解析）", () => {
  it("path 拆成路径段而非 query；空段被过滤", () => {
    expect(codeFileUrl(P, S, R, "//site//index.html//")).toBe(
      `${API}/code/file/${P}/${S}/${R}/site/index.html`,
    );
  });

  it("各段独立 encodeURIComponent；version 只缓存击穿入口文档", () => {
    expect(codeFileUrl("p g", "s/1", "r o", "页面/a b.html", "v 1")).toBe(
      `${API}/code/file/p%20g/s%2F1/r%20o/%E9%A1%B5%E9%9D%A2/a%20b.html?v=v%201`,
    );
  });

  it("不带 version → 无 query", () => {
    expect(codeFileUrl(P, S, R, "index.html")).toBe(`${API}/code/file/${P}/${S}/${R}/index.html`);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("writeCodeFile", () => {
  it("成功：PUT /code/write，body 蛇形命名，encoding 缺省 utf-8，返回新版本", async () => {
    reply({ ok: true, version: "v2" });
    await expect(
      writeCodeFile(P, S, R, "a.ts", { content: "let x = 2;", baseVersion: "v1" }),
    ).resolves.toBe("v2");
    expectWrite("PUT", "/code/write", {
      project_slug: P,
      session_id: S,
      root: R,
      path: "a.ts",
      content: "let x = 2;",
      base_version: "v1",
      encoding: "utf-8",
    });
  });

  it("显式 encoding（gbk）原样下发——读什么编码写什么编码", async () => {
    reply({ ok: true, version: "v3" });
    await writeCodeFile(P, S, R, "a.txt", { content: "中文", baseVersion: "v2", encoding: "gbk" });
    expectWrite("PUT", "/code/write", {
      project_slug: P,
      session_id: S,
      root: R,
      path: "a.txt",
      content: "中文",
      base_version: "v2",
      encoding: "gbk",
    });
  });

  it("conflict → 抛 CodeConflictError 携带盘上版本（免二次往返）", async () => {
    reply({ ok: false, code: "conflict", version: "v9", message: "文件已被修改" });
    const err = await writeCodeFile(P, S, R, "a.ts", { content: "x", baseVersion: "v1" }).catch((e) => e);
    expect(err).toBeInstanceOf(CodeConflictError);
    expect(err.name).toBe("CodeConflictError");
    expect(err.version).toBe("v9");
    expect(err.message).toBe("文件已被修改");
  });

  it("conflict 缺 version/message → 版本空串 + 默认文案", async () => {
    reply({ ok: false, code: "conflict" });
    const err = await writeCodeFile(P, S, R, "a.ts", { content: "x", baseVersion: "v1" }).catch((e) => e);
    expect(err).toBeInstanceOf(CodeConflictError);
    expect(err.version).toBe("");
    expect(err.message).toBe("File has been modified on disk");
  });

  it("其它拒绝（read-only-mount）→ CodeApiError 带服务端码", async () => {
    reply({ ok: false, code: "read-only-mount", message: "只读挂载" });
    await expect(
      writeCodeFile(P, S, R, "a.ts", { content: "x", baseVersion: "v1" }),
    ).rejects.toMatchObject({ name: "CodeApiError", code: "read-only-mount", message: "只读挂载" });
  });

  it("拒绝缺 code/message → 回落 not-text / Save failed", async () => {
    reply({ ok: false });
    await expect(
      writeCodeFile(P, S, R, "a.ts", { content: "x", baseVersion: "v1" }),
    ).rejects.toMatchObject({ name: "CodeApiError", code: "not-text", message: "Save failed" });
  });

  it("ok:true 但缺 version → 不算成功，按 CodeApiError 处理", async () => {
    reply({ ok: true });
    await expect(
      writeCodeFile(P, S, R, "a.ts", { content: "x", baseVersion: "v1" }),
    ).rejects.toMatchObject({ name: "CodeApiError", code: "not-text" });
  });
});

describe("codeDiff", () => {
  it("成功：蛇形 wire → 驼峰 CodeDiff 全字段映射", async () => {
    reply({
      ok: true,
      baseline_exists: true,
      base_content: "old",
      base_version: "v1",
      current_content: "new",
      current_version: "v2",
      degraded: true,
    });
    await expect(codeDiff(P, S, R, "a.ts")).resolves.toEqual({
      baselineExists: true,
      baseContent: "old",
      baseVersion: "v1",
      currentContent: "new",
      currentVersion: "v2",
      degraded: true,
    });
    expectGet("/code/diff", { project_slug: P, session_id: S, root: R, path: "a.ts" });
  });

  it("baseline_exists:false 是正常答案；缺字段全回落（null/空串/false）", async () => {
    reply({ ok: true, baseline_exists: false });
    await expect(codeDiff(P, S, R, "a.ts")).resolves.toEqual({
      baselineExists: false,
      baseContent: null,
      baseVersion: "",
      currentContent: null,
      currentVersion: "",
      degraded: false,
    });
  });

  it("ok:false → 抛 CodeApiError", async () => {
    reply({ ok: false, code: "outside-root", message: "越界" });
    await expect(codeDiff(P, S, R, "../x")).rejects.toMatchObject({
      name: "CodeApiError",
      code: "outside-root",
    });
  });

  it("ok:false 缺 code/message → 回落 unknown-root", async () => {
    reply({ ok: false });
    await expect(codeDiff(P, S, R, "x")).rejects.toMatchObject({
      name: "CodeApiError",
      code: "unknown-root",
      message: "unknown-root",
    });
  });
});

describe("getCodeGit", () => {
  it("成功：query 只带 root（无 path）；状态原样透传", async () => {
    const entries = { "src/a.ts": "M", "b.ts": "A" };
    reply({ ok: true, is_repo: true, toplevel: "/w/ginno", branch: "main", entries });
    await expect(getCodeGit(P, S, R)).resolves.toEqual({
      is_repo: true,
      toplevel: "/w/ginno",
      branch: "main",
      entries,
    });
    expectGet("/code/git", { project_slug: P, session_id: S, root: R });
  });

  it("非仓库（is_repo:false）是正常答案；缺字段回落 null/{}", async () => {
    reply({ ok: true, is_repo: false });
    await expect(getCodeGit(P, S, R)).resolves.toEqual({
      is_repo: false,
      toplevel: null,
      branch: null,
      entries: {},
    });
  });

  it("ok:false（unknown-root）→ 抛 CodeApiError", async () => {
    reply({ ok: false, code: "unknown-root", message: "未知根" });
    await expect(getCodeGit(P, S, "ghost")).rejects.toMatchObject({
      name: "CodeApiError",
      code: "unknown-root",
    });
  });

  it("ok:false 缺 code/message → 回落 unknown-root", async () => {
    reply({ ok: false });
    await expect(getCodeGit(P, S, R)).rejects.toMatchObject({
      name: "CodeApiError",
      code: "unknown-root",
      message: "unknown-root",
    });
  });
});

describe("codeMkdir", () => {
  it('kind:"file" → POST /code/mkdir，body 含 kind', async () => {
    reply({ ok: true });
    await expect(codeMkdir(P, S, R, "src/new.ts", "file")).resolves.toBeUndefined();
    expectWrite("POST", "/code/mkdir", {
      project_slug: P,
      session_id: S,
      root: R,
      path: "src/new.ts",
      kind: "file",
    });
  });

  it('kind:"dir" 同样下发；ok:false（bad-name）→ 抛 CodeApiError', async () => {
    reply({ ok: false, code: "bad-name", message: "非法名称" });
    await expect(codeMkdir(P, S, R, "a/b", "dir")).rejects.toMatchObject({
      name: "CodeApiError",
      code: "bad-name",
    });
    expectWrite("POST", "/code/mkdir", {
      project_slug: P,
      session_id: S,
      root: R,
      path: "a/b",
      kind: "dir",
    });
  });
});

describe("codeRename", () => {
  it("成功：to 为完整根相对目标路径（服务端不复用源父目录）", async () => {
    reply({ ok: true });
    await codeRename(P, S, R, "src/a.ts", "src/b.ts");
    expectWrite("POST", "/code/rename", {
      project_slug: P,
      session_id: S,
      root: R,
      path: "src/a.ts",
      to: "src/b.ts",
    });
  });

  it("ok:false（exists）→ 抛 CodeApiError", async () => {
    reply({ ok: false, code: "exists", message: "目标已存在" });
    await expect(codeRename(P, S, R, "a.ts", "b.ts")).rejects.toMatchObject({
      name: "CodeApiError",
      code: "exists",
      message: "目标已存在",
    });
  });

  it("ok:false 缺 code/message → assertOk 回落 unknown-root（mkdir/move/delete 共用）", async () => {
    reply({ ok: false });
    await expect(codeRename(P, S, R, "a.ts", "b.ts")).rejects.toMatchObject({
      name: "CodeApiError",
      code: "unknown-root",
      message: "unknown-root",
    });
  });
});

describe("codeMove", () => {
  it("成功：POST /code/move，body 带完整 to 路径", async () => {
    reply({ ok: true });
    await codeMove(P, S, R, "a.ts", "sub/a.ts");
    expectWrite("POST", "/code/move", {
      project_slug: P,
      session_id: S,
      root: R,
      path: "a.ts",
      to: "sub/a.ts",
    });
  });

  it("ok:false（invalid-target，移入自身子树）→ 抛 CodeApiError", async () => {
    reply({ ok: false, code: "invalid-target", message: "不能移入自身子树" });
    await expect(codeMove(P, S, R, "d", "d/sub")).rejects.toMatchObject({
      name: "CodeApiError",
      code: "invalid-target",
    });
  });
});

describe("codeDelete", () => {
  it("默认 opts → body 不含 confirm/confirm_count/check_only；count 缺省 0", async () => {
    reply({ ok: true });
    await expect(codeDelete(P, S, R, "a.ts")).resolves.toEqual({
      count: 0,
      message: undefined,
      trashPath: undefined,
    });
    expectWrite("POST", "/code/delete", {
      project_slug: P,
      session_id: S,
      root: R,
      path: "a.ts",
    });
  });

  it("confirm + confirmCount:0 + checkOnly 全下发（0 走 != null 不被吞）；响应映射 trashPath", async () => {
    reply({ ok: true, count: 5, message: "已移入回收站", trash_path: "/w/.trash-1" });
    await expect(
      codeDelete(P, S, R, "dir", { confirm: true, confirmCount: 0, checkOnly: true }),
    ).resolves.toEqual({ count: 5, message: "已移入回收站", trashPath: "/w/.trash-1" });
    expectWrite("POST", "/code/delete", {
      project_slug: P,
      session_id: S,
      root: R,
      path: "dir",
      confirm: true,
      confirm_count: 0,
      check_only: true,
    });
  });

  it("仅 checkOnly → 只追加 check_only（预检不删除）", async () => {
    reply({ ok: true, count: 3 });
    const r = await codeDelete(P, S, R, "dir", { checkOnly: true });
    expect(r.count).toBe(3);
    expectWrite("POST", "/code/delete", {
      project_slug: P,
      session_id: S,
      root: R,
      path: "dir",
      check_only: true,
    });
  });

  it("ok:false（confirm-required，非空目录需二次确认）→ 抛 CodeApiError", async () => {
    reply({ ok: false, code: "confirm-required", message: "目录非空" });
    await expect(codeDelete(P, S, R, "dir")).rejects.toMatchObject({
      name: "CodeApiError",
      code: "confirm-required",
    });
  });
});

describe("searchCode（always-resolve 契约）", () => {
  const hit = { path: "a.ts", line: 3, text: "let x", ranges: [[4, 5]] };

  it("成功带 limit → query 含 root/q/mode/limit；elapsed_ms 重命名 elapsedMs", async () => {
    reply({ ok: true, hits: [hit], scanned: 42, truncated: true, elapsed_ms: 17 });
    await expect(searchCode(P, S, R, "let", "content", 50)).resolves.toEqual({
      hits: [hit],
      scanned: 42,
      truncated: true,
      elapsedMs: 17,
    });
    expectGet("/code/search", {
      project_slug: P,
      session_id: S,
      root: R,
      q: "let",
      mode: "content",
      limit: "50",
    });
  });

  it("不传 limit → query 无 limit；极简 ok:true 全回落默认值", async () => {
    reply({ ok: true });
    await expect(searchCode(P, S, R, "a", "name")).resolves.toEqual({
      hits: [],
      scanned: 0,
      truncated: false,
      elapsedMs: 0,
    });
    expectGet("/code/search", { project_slug: P, session_id: S, root: R, q: "a", mode: "name" });
  });

  it("limit=0 仍下发（!= null 判空，与 falsy 守卫不同）", async () => {
    reply({ ok: true, hits: [] });
    await searchCode(P, S, R, "a", "name", 0);
    expectGet("/code/search", {
      project_slug: P,
      session_id: S,
      root: R,
      q: "a",
      mode: "name",
      limit: "0",
    });
  });

  it("ok:false（invalid-regex）→ 不抛错，resolve 出 error 结构供视图原地渲染", async () => {
    reply({ ok: false, code: "invalid-regex", message: "正则无法编译" });
    await expect(searchCode(P, S, R, "([", "content")).resolves.toEqual({
      hits: [],
      scanned: 0,
      truncated: false,
      elapsedMs: 0,
      error: { code: "invalid-regex", message: "正则无法编译" },
    });
  });

  it("ok:false 缺 code → error.code 回落 unknown，message 透传 undefined", async () => {
    reply({ ok: false });
    const r = await searchCode(P, S, R, "", "name");
    expect(r.error).toEqual({ code: "unknown", message: undefined });
    expect(r.hits).toEqual([]);
  });
});

describe("传输层失败（非 ok:false 信封）", () => {
  it("fetch 网络错误 → 原样 reject 原始 Error，不包成 CodeApiError", async () => {
    const boom = new Error("network down");
    fetchMock.mockRejectedValue(boom);
    const err = await listCodeRoots(P, S).catch((e) => e);
    expect(err).toBe(boom);
    expect(err).not.toBeInstanceOf(CodeApiError);
  });
});
