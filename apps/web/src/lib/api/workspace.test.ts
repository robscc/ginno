/**
 * api/workspace.ts（context folders / todos / artifacts / files / mods /
 * settings / MCP / skills / Tool Policy API 客户端）单元测试。
 *
 * 打桩策略：桩 globalThis.fetch，**不 mock ./core 的 json**——真实走
 * locale 头合并、URLSearchParams 条件拼参与 JSON 序列化。i18n provider
 * 桩成固定 locale（与第一批 misc.test.ts 同范式）。
 *
 * 重点分支：可选参数传/不传/空串 falsy 守卫、默认参数（project_slug /
 * overwrite / includeTools）、encodeURIComponent、?? 与 falsy 守卫的语义
 * 差异（offset=0 保留）、testMcp 的 timeout:undefined 被 JSON.stringify
 * 丢弃、uploadFile 不带 Content-Type（multipart boundary 由浏览器设置）、
 * downloadFile 的 content-disposition 解析优先级与三条错误路径。
 *
 * downloadFile 依赖 DOM（document.createElement / body.appendChild）与
 * URL.createObjectURL：node 环境用最小 document 桩 + spyOn URL 静态方法，
 * 假定时器验证 30s 后 revoke。uiText 未桩——node 下其 locale 镜像恒为
 * 默认 en（provider 已 mock，不会调 setUiTextLocale），故通用错误文案
 * 断言 en catalog 原文。
 */
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";

vi.mock("../../i18n/provider", () => ({ currentLocale: () => "zh-CN" }));

import {
  attachFilePath,
  contextBreakdown,
  createFolder,
  createSkill,
  createTodo,
  deleteArtifact,
  deleteFolder,
  deleteSkill,
  deleteTodo,
  downloadFile,
  ensureFileRegistered,
  fileDownloadUrl,
  fileInlineUrl,
  getArtifactMetadata,
  getMcp,
  getMcpConfig,
  getSessionDetail,
  getSettings,
  getFilePreview,
  importSkillsDir,
  listArtifacts,
  listFiles,
  listFolders,
  listMods,
  listSkills,
  listTodoProviders,
  listTodos,
  mcpStatus,
  openFileExternal,
  probeFolder,
  pullTodos,
  pushTodo,
  putMcp,
  putSessionContext,
  putSettings,
  reconnectMcp,
  reloadMcp,
  revealFileInFileManager,
  saveFileToDownloads,
  setToolPolicy,
  testMcp,
  todoSyncStatus,
  uninstallMod,
  updateArtifact,
  updateFolder,
  updateMod,
  updateTodo,
  uploadFile,
  validateMod,
} from "./workspace";
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

/**
 * 断言唯一一次 GET：精确 URL 字符串（含 query 形态与顺序）+ locale 头 +
 * 无 method/body；给 params 时再做 origin+pathname 与 searchParams 深比较。
 * listSkills 传给 fetch 的是 URL 实例，String() 归一后同样可断言。
 */
function expectGet(url: string, params?: Record<string, string>) {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [got, init] = fetchMock.mock.calls[0] as [string | URL, RequestInit];
  expect(String(got)).toBe(url);
  if (params) {
    const u = new URL(String(got));
    expect(`${u.origin}${u.pathname}`).toBe(url.split("?")[0]);
    expect(Object.fromEntries(u.searchParams)).toEqual(params);
  }
  expect(init.method ?? "GET").toBe("GET");
  expect(init.body).toBeUndefined();
  expect(init.headers).toEqual({ "X-Ginno-Language": "zh-CN" });
}

/** 断言唯一一次带 JSON body 的写请求：URL、method、body 深比较、双头合并。 */
function expectWrite(method: "POST" | "PUT" | "PATCH", path: string, body: unknown) {
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

/**
 * 断言唯一一次无 body 请求（DELETE / 裸 POST）。withH 区分源码是否传了
 * headers: H——deleteXxx / reloadMcp / reconnectMcp 只有 locale 头，
 * openFileExternal / revealFileInFileManager 带 Content-Type。
 */
function expectNoBody(method: "POST" | "DELETE", path: string, withH: boolean) {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toBe(`${API}${path}`);
  expect(init.method).toBe(method);
  expect(init.body).toBeUndefined();
  expect(init.headers).toEqual(
    withH
      ? { "X-Ginno-Language": "zh-CN", "Content-Type": "application/json" }
      : { "X-Ginno-Language": "zh-CN" },
  );
}

describe("BASE 回落（node 无 window）", () => {
  it("BASE 为 127.0.0.1:8787/api", () => {
    expect(BASE).toBe(API);
  });
});

// ---- context folders ----

describe("listFolders", () => {
  it("GET /folders，响应原样透传（含 runtime_home）", async () => {
    const body = {
      ok: true,
      folders: [{ id: "f1", path: "/tmp/a", name: "a", access: "rw" }],
      runtime_home: "/Users/x/.ginno",
    };
    reply(body);
    await expect(listFolders()).resolves.toEqual(body);
    expectGet(`${API}/folders`, {});
  });

  it("空清单 → folders:[] 原样返回", async () => {
    reply({ ok: true, folders: [] });
    await expect(listFolders()).resolves.toEqual({ ok: true, folders: [] });
  });
});

describe("createFolder", () => {
  it("POST /folders，全字段 body 原样序列化", async () => {
    const data = {
      path: "/tmp/proj",
      name: "proj",
      access: "ro" as const,
      load_rules: true,
      auto_mount: false,
    };
    const body = { ok: true, folder: { id: "f9", ...data } };
    reply(body);
    await expect(createFolder(data)).resolves.toEqual(body);
    expectWrite("POST", "/folders", data);
  });

  it("仅 path（可选字段全不传）→ body 只有 path", async () => {
    reply({ ok: true });
    await createFolder({ path: "/tmp/min" });
    expectWrite("POST", "/folders", { path: "/tmp/min" });
  });

  it("ok:false 错误信封原样透传（客户端不拆信封不抛错）", async () => {
    const body = { ok: false, error: "路径不存在" };
    reply(body);
    await expect(createFolder({ path: "/nope" })).resolves.toEqual(body);
  });
});

describe("probeFolder", () => {
  it("POST /folders/probe，body {path}", async () => {
    const body = { has_rules: true, rule_files: ["CLAUDE.md"] };
    reply(body);
    await expect(probeFolder("/tmp/proj")).resolves.toEqual(body);
    expectWrite("POST", "/folders/probe", { path: "/tmp/proj" });
  });
});

describe("updateFolder", () => {
  it("PATCH /folders/{id}，patch 原样序列化", async () => {
    const patch = { name: "renamed", access: "ro" as const };
    const body = { ok: true, folder: { id: "f1", ...patch } };
    reply(body);
    await expect(updateFolder("f1", patch)).resolves.toEqual(body);
    expectWrite("PATCH", "/folders/f1", patch);
  });
});

describe("deleteFolder", () => {
  it("DELETE /folders/{id}，无 body、仅 locale 头", async () => {
    reply({ ok: true, removed: true });
    await expect(deleteFolder("f1")).resolves.toEqual({ ok: true, removed: true });
    expectNoBody("DELETE", "/folders/f1", false);
  });

  it("removed:false（幂等删除不存在的 id）原样透传", async () => {
    reply({ ok: true, removed: false });
    await expect(deleteFolder("ghost")).resolves.toEqual({ ok: true, removed: false });
  });
});

describe("putSessionContext", () => {
  it("PUT /sessions/{id}/context，folder_ids + primary_id 全量替换", async () => {
    const body = { folder_ids: ["f1", "f2"], primary_id: "f1" };
    const resp = {
      ok: true,
      session: { id: "s1", context_folders: ["f1", "f2"], primary_folder: "f1" },
      primary_path: "/tmp/a",
    };
    reply(resp);
    await expect(putSessionContext("s1", body)).resolves.toEqual(resp);
    expectWrite("PUT", "/sessions/s1/context", body);
  });

  it("空挂载集 + primary_id:null（清空语义，null 必须保留在 body）", async () => {
    reply({ ok: true });
    await putSessionContext("s1", { folder_ids: [], primary_id: null });
    expectWrite("PUT", "/sessions/s1/context", { folder_ids: [], primary_id: null });
  });

  it("primary_id 不传 → body 无该键", async () => {
    reply({ ok: true });
    await putSessionContext("s2", { folder_ids: ["f3"] });
    expectWrite("PUT", "/sessions/s2/context", { folder_ids: ["f3"] });
  });
});

// ---- todos ----

describe("listTodos", () => {
  it("GET /todos，数组原样透传", async () => {
    const todos = [
      {
        id: "t1",
        title: "补测",
        priority: "high",
        category: "dev",
        due: "",
        done: false,
        links: {},
        created: 1,
        completed_at: null,
      },
    ];
    reply(todos);
    await expect(listTodos()).resolves.toEqual(todos);
    expectGet(`${API}/todos`, {});
  });

  it("空清单 → []", async () => {
    reply([]);
    await expect(listTodos()).resolves.toEqual([]);
  });
});

describe("createTodo", () => {
  it("POST /todos，Partial<Todo> 原样序列化", async () => {
    const data = { title: "写测试", priority: "high" as const, tags: ["cov"] };
    const body = { ok: true, todo: { id: "t9", ...data } };
    reply(body);
    await expect(createTodo(data)).resolves.toEqual(body);
    expectWrite("POST", "/todos", data);
  });
});

describe("updateTodo", () => {
  it("PATCH /todos/{id}，patch 原样序列化", async () => {
    const body = { ok: true, todo: { id: "t1", done: true } };
    reply(body);
    await expect(updateTodo("t1", { done: true })).resolves.toEqual(body);
    expectWrite("PATCH", "/todos/t1", { done: true });
  });

  it("ok:false 错误信封原样透传", async () => {
    reply({ ok: false, error: "todo 不存在" });
    await expect(updateTodo("ghost", { done: true })).resolves.toEqual({
      ok: false,
      error: "todo 不存在",
    });
  });
});

describe("deleteTodo", () => {
  it("DELETE /todos/{id}，无 body、仅 locale 头", async () => {
    reply({ ok: true });
    await expect(deleteTodo("t1")).resolves.toEqual({ ok: true });
    expectNoBody("DELETE", "/todos/t1", false);
  });
});

// ---- skills（list 在 artifacts 前，与源码顺序一致）----

describe("listSkills", () => {
  it("不传 project_slug → 裸 /skills；fetch 收到 URL 实例", async () => {
    const body = [{ name: "todo", description: "d", scope: "global" }];
    reply(body);
    await expect(listSkills()).resolves.toEqual(body);
    expect(fetchMock.mock.calls[0][0]).toBeInstanceOf(URL);
    expectGet(`${API}/skills`, {});
  });

  it("传 project_slug → ?project_slug=…", async () => {
    reply([]);
    await listSkills("default");
    expectGet(`${API}/skills?project_slug=default`, { project_slug: "default" });
  });

  it("空串 slug 被 falsy 守卫跳过 → 不带参数", async () => {
    reply([]);
    await listSkills("");
    expectGet(`${API}/skills`, {});
  });
});

// ---- artifacts ----

describe("listArtifacts", () => {
  it("缺省 → project_slug=default、无 session_id", async () => {
    const body = [{ id: "a1", name: "report.md" }];
    reply(body);
    await expect(listArtifacts()).resolves.toEqual(body);
    expectGet(`${API}/artifacts?project_slug=default`, { project_slug: "default" });
  });

  it("session_id 走 encodeURIComponent（斜杠/空格不破坏 query）", async () => {
    reply([]);
    await listArtifacts("default", "s/1 2");
    expectGet(`${API}/artifacts?project_slug=default&session_id=s%2F1%202`, {
      project_slug: "default",
      session_id: "s/1 2",
    });
  });

  it("自定义 project_slug + session_id 同时下发", async () => {
    reply([]);
    await listArtifacts("team-a", "s1");
    expectGet(`${API}/artifacts?project_slug=team-a&session_id=s1`, {
      project_slug: "team-a",
      session_id: "s1",
    });
  });
});

describe("deleteArtifact", () => {
  it("DELETE /artifacts/{id}?project_slug=default（缺省），仅 locale 头", async () => {
    reply({ ok: true });
    await expect(deleteArtifact("a1")).resolves.toEqual({ ok: true });
    expectNoBody("DELETE", "/artifacts/a1?project_slug=default", false);
  });

  it("自定义 project_slug 进 query", async () => {
    reply({ ok: true });
    await deleteArtifact("a1", "team-a");
    expectNoBody("DELETE", "/artifacts/a1?project_slug=team-a", false);
  });
});

describe("getArtifactMetadata", () => {
  it("GET /artifacts/{id}/metadata?project_slug=default，inspector 载荷透传", async () => {
    const body = { artifact: { id: "a1" }, file: { size: 10 }, schema: { type: "markdown" } };
    reply(body);
    await expect(getArtifactMetadata("a1")).resolves.toEqual(body);
    expectGet(`${API}/artifacts/a1/metadata?project_slug=default`, { project_slug: "default" });
  });

  it("自定义 project_slug", async () => {
    reply({});
    await getArtifactMetadata("a2", "team-a");
    expectGet(`${API}/artifacts/a2/metadata?project_slug=team-a`, { project_slug: "team-a" });
  });
});

describe("updateArtifact", () => {
  it("PUT /artifacts/{id}?project_slug=default，patch 原样序列化", async () => {
    const patch = { schema: "table", file_kind: "spreadsheet" };
    const body = { ok: true, artifact: { id: "a1" } };
    reply(body);
    await expect(updateArtifact("a1", patch)).resolves.toEqual(body);
    expectWrite("PUT", "/artifacts/a1?project_slug=default", patch);
  });

  it("自定义 project_slug + 错误信封透传", async () => {
    reply({ ok: false, error: "未知 schema" });
    await expect(updateArtifact("a1", { file_kind: "x" }, "team-a")).resolves.toEqual({
      ok: false,
      error: "未知 schema",
    });
    expectWrite("PUT", "/artifacts/a1?project_slug=team-a", { file_kind: "x" });
  });
});

// ---- files ----

describe("uploadFile", () => {
  it("POST /files，FormData 含 session_id+file，**不带 Content-Type**（boundary 由浏览器设置）", async () => {
    const file = new File(["hello"], "notes.txt", { type: "text/plain" });
    const body = { ok: true, file: { id: "file1", name: "notes.txt" } };
    reply(body);
    await expect(uploadFile("s1", file)).resolves.toEqual(body);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API}/files`);
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    const fd = init.body as FormData;
    expect([...fd.keys()]).toEqual(["session_id", "file"]);
    expect(fd.get("session_id")).toBe("s1");
    expect(fd.get("file")).toBe(file);
    // 关键契约：只有 locale 头，绝不能有 Content-Type
    expect(init.headers).toEqual({ "X-Ginno-Language": "zh-CN" });
  });

  it("ok:false 错误信封原样透传", async () => {
    reply({ ok: false, error: "文件过大" });
    await expect(
      uploadFile("s1", new File(["x"], "big.bin")),
    ).resolves.toEqual({ ok: false, error: "文件过大" });
  });
});

describe("listFiles", () => {
  it("缺省 → project_slug=default、无 session_id", async () => {
    const body = [{ id: "file1", name: "a.txt" }];
    reply(body);
    await expect(listFiles()).resolves.toEqual(body);
    expectGet(`${API}/files?project_slug=default`, { project_slug: "default" });
  });

  it("session_id 走 encodeURIComponent", async () => {
    reply([]);
    await listFiles("default", "s/1 2");
    expectGet(`${API}/files?project_slug=default&session_id=s%2F1%202`, {
      project_slug: "default",
      session_id: "s/1 2",
    });
  });

  it("自定义 project_slug，空清单 → []", async () => {
    reply([]);
    await expect(listFiles("team-a")).resolves.toEqual([]);
    expectGet(`${API}/files?project_slug=team-a`, { project_slug: "team-a" });
  });
});

describe("attachFilePath", () => {
  it("POST /files/attach-path，body {session_id, path}", async () => {
    const body = { ok: true, file: { id: "file2", name: "drop.pdf" } };
    reply(body);
    await expect(attachFilePath("s1", "/Users/x/drop.pdf")).resolves.toEqual(body);
    expectWrite("POST", "/files/attach-path", { session_id: "s1", path: "/Users/x/drop.pdf" });
  });
});

describe("ensureFileRegistered", () => {
  it("不传 name → body 只有 session_id+path", async () => {
    const body = { ok: true, file: { id: "file3" } };
    reply(body);
    await expect(ensureFileRegistered("s1", "/tmp/ref.md")).resolves.toEqual(body);
    expectWrite("POST", "/files/ensure-registered", { session_id: "s1", path: "/tmp/ref.md" });
  });

  it("传 name → body 追加 name", async () => {
    reply({ ok: true });
    await ensureFileRegistered("s1", "/tmp/ref.md", "重命名.md");
    expectWrite("POST", "/files/ensure-registered", {
      session_id: "s1",
      path: "/tmp/ref.md",
      name: "重命名.md",
    });
  });

  it("空串 name 被 falsy 守卫跳过", async () => {
    reply({ ok: true });
    await ensureFileRegistered("s1", "/tmp/ref.md", "");
    expectWrite("POST", "/files/ensure-registered", { session_id: "s1", path: "/tmp/ref.md" });
  });
});

describe("getFilePreview", () => {
  it("不传 opts → 缺省 offset=0&limit=100", async () => {
    const body = { kind: "text", text: "hi" };
    reply(body);
    await expect(getFilePreview("file1")).resolves.toEqual(body);
    expectGet(`${API}/files/file1/preview?offset=0&limit=100`, { offset: "0", limit: "100" });
  });

  it("空 opts 对象 → 同样缺省", async () => {
    reply({});
    await getFilePreview("file1", {});
    expectGet(`${API}/files/file1/preview?offset=0&limit=100`, { offset: "0", limit: "100" });
  });

  it("sheet+offset+limit 全传 → sheet 在前，数字转字符串", async () => {
    reply({});
    await getFilePreview("file1", { sheet: "Sheet 1", offset: 20, limit: 50 });
    expectGet(`${API}/files/file1/preview?sheet=Sheet+1&offset=20&limit=50`, {
      sheet: "Sheet 1",
      offset: "20",
      limit: "50",
    });
  });

  it("offset=0 / limit=0 走 ?? 语义保留 0（不是 falsy 守卫）；空串 sheet 跳过", async () => {
    reply({});
    await getFilePreview("file1", { sheet: "", offset: 0, limit: 0 });
    expectGet(`${API}/files/file1/preview?offset=0&limit=0`, { offset: "0", limit: "0" });
  });
});

describe("fileDownloadUrl（纯函数）", () => {
  it("缺省 → 裸 URL 不带问号", () => {
    expect(fileDownloadUrl("file1")).toBe(`${API}/files/file1/download`);
  });

  it("fmt=raw 被显式跳过（raw 是服务端缺省）", () => {
    expect(fileDownloadUrl("file1", { fmt: "raw" })).toBe(`${API}/files/file1/download`);
  });

  it("fmt=csv → ?fmt=csv", () => {
    expect(fileDownloadUrl("file1", { fmt: "csv" })).toBe(`${API}/files/file1/download?fmt=csv`);
  });

  it("只传 sheet → 仅 sheet 参数", () => {
    expect(fileDownloadUrl("file1", { sheet: "S1" })).toBe(
      `${API}/files/file1/download?sheet=S1`,
    );
  });

  it("fmt=csv+sheet → fmt 在前，sheet 走 URLSearchParams 编码（空格为 + 而非 %20）", () => {
    expect(fileDownloadUrl("file1", { fmt: "csv", sheet: "数据 表" })).toBe(
      `${API}/files/file1/download?fmt=csv&sheet=%E6%95%B0%E6%8D%AE+%E8%A1%A8`,
    );
  });
});

describe("fileInlineUrl（纯函数）", () => {
  it("返回 /files/{id}/inline", () => {
    expect(fileInlineUrl("file1")).toBe(`${API}/files/file1/inline`);
  });
});

describe("downloadFile（浏览器侧保存，DOM 桩 + 假定时器）", () => {
  let anchor: {
    href: string;
    download: string;
    click: ReturnType<typeof vi.fn>;
    remove: ReturnType<typeof vi.fn>;
  };
  let appendChild: ReturnType<typeof vi.fn>;
  let createElement: ReturnType<typeof vi.fn>;
  let createObjectURL: MockInstance<(obj: Blob | MediaSource) => string>;
  let revokeObjectURL: MockInstance<(id: string) => void>;

  beforeEach(() => {
    vi.useFakeTimers();
    anchor = { href: "", download: "", click: vi.fn(), remove: vi.fn() };
    appendChild = vi.fn();
    createElement = vi.fn(() => anchor);
    vi.stubGlobal("document", { createElement, body: { appendChild } });
    createObjectURL = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:mock-url");
    revokeObjectURL = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  /** 构造 downloadFile 用的响应桩（fetch 直发，不走 json helper）。 */
  function replyDownload(opts: {
    ok: boolean;
    status?: number;
    cd?: string | null;
    json?: () => Promise<unknown>;
  }) {
    fetchMock.mockResolvedValue({
      ok: opts.ok,
      status: opts.status ?? 200,
      headers: { get: (k: string) => (k === "content-disposition" ? (opts.cd ?? null) : null) },
      blob: async () => new Blob(["data"]),
      json: opts.json ?? (async () => ({})),
    });
  }

  it("filename*=UTF-8'' 优先 → decodeURIComponent 文件名；完整 DOM 流程 + 30s 后 revoke", async () => {
    replyDownload({ ok: true, cd: "attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.pdf" });
    await expect(downloadFile("file1", "fallback.bin")).resolves.toEqual({ ok: true });

    // 请求本身：裸 GET download URL + 仅 locale 头
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API}/files/file1/download`);
    expect(init.method ?? "GET").toBe("GET");
    expect(init.headers).toEqual({ "X-Ginno-Language": "zh-CN" });

    expect(createElement).toHaveBeenCalledWith("a");
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(anchor.href).toBe("blob:mock-url");
    expect(anchor.download).toBe("报告.pdf");
    expect(appendChild).toHaveBeenCalledWith(anchor);
    expect(anchor.click).toHaveBeenCalledTimes(1);
    expect(anchor.remove).toHaveBeenCalledTimes(1);
    // 30s 延迟回收：未到点不 revoke，到点恰好一次
    expect(revokeObjectURL).not.toHaveBeenCalled();
    vi.advanceTimersByTime(30_000);
    expect(revokeObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock-url");
  });

  it("plain filename=\"…\" → 引号内文件名；opts 透传到 download URL", async () => {
    replyDownload({ ok: true, cd: 'attachment; filename="report.csv"' });
    await expect(
      downloadFile("file1", "fallback.bin", { fmt: "csv", sheet: "S1" }),
    ).resolves.toEqual({ ok: true });
    expect(fetchMock.mock.calls[0][0]).toBe(`${API}/files/file1/download?fmt=csv&sheet=S1`);
    expect(anchor.download).toBe("report.csv");
  });

  it("两种 filename 同时存在 → star（UTF-8）优先", async () => {
    replyDownload({
      ok: true,
      cd: `attachment; filename="ascii.pdf"; filename*=UTF-8''%E4%B8%AD%E6%96%87.pdf`,
    });
    await downloadFile("file1", "fallback.bin");
    expect(anchor.download).toBe("中文.pdf");
  });

  it("无 content-disposition → 回落 fallbackName", async () => {
    replyDownload({ ok: true, cd: null });
    await expect(downloadFile("file1", "fallback.bin")).resolves.toEqual({ ok: true });
    expect(anchor.download).toBe("fallback.bin");
  });

  it("content-disposition 无 filename 字段 → 同样回落 fallbackName", async () => {
    replyDownload({ ok: true, cd: "attachment" });
    await downloadFile("file1", "fallback.bin");
    expect(anchor.download).toBe("fallback.bin");
  });

  it("HTTP 错误 + JSON detail → 用服务端 detail 作错误文案", async () => {
    replyDownload({ ok: false, status: 404, json: async () => ({ detail: "文件不存在" }) });
    await expect(downloadFile("file1", "fallback.bin")).resolves.toEqual({
      ok: false,
      error: "文件不存在",
    });
    expect(anchor.click).not.toHaveBeenCalled();
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it("HTTP 错误 + JSON 无 detail → 通用文案（uiText 默认 en，插值 status）", async () => {
    replyDownload({ ok: false, status: 500, json: async () => ({}) });
    await expect(downloadFile("file1", "fallback.bin")).resolves.toEqual({
      ok: false,
      error: "Download failed (HTTP 500)",
    });
  });

  it("HTTP 错误 + 响应非 JSON（json() 抛错）→ 吞掉解析错误，保留通用文案", async () => {
    replyDownload({
      ok: false,
      status: 502,
      json: async () => {
        throw new Error("not json");
      },
    });
    await expect(downloadFile("file1", "fallback.bin")).resolves.toEqual({
      ok: false,
      error: "Download failed (HTTP 502)",
    });
  });

  it("fetch reject（网络不可达）→ ok:false + net.unreachable 文案，不抛错", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    await expect(downloadFile("file1", "fallback.bin")).resolves.toEqual({
      ok: false,
      error: "Cannot reach the runtime",
    });
    expect(createElement).not.toHaveBeenCalled();
  });
});

describe("saveFileToDownloads", () => {
  it("缺省 opts → POST body {}（raw 全文件）", async () => {
    const body = { ok: true, path: "/Users/x/Downloads/a.pdf", name: "a.pdf" };
    reply(body);
    await expect(saveFileToDownloads("file1")).resolves.toEqual(body);
    expectWrite("POST", "/files/file1/save-to-downloads", {});
  });

  it("fmt=csv+sheet → opts 原样下发", async () => {
    reply({ ok: true });
    await saveFileToDownloads("file1", { fmt: "csv", sheet: "S1" });
    expectWrite("POST", "/files/file1/save-to-downloads", { fmt: "csv", sheet: "S1" });
  });
});

describe("openFileExternal", () => {
  it("POST /files/{id}/open-external，无 body、带 Content-Type 双头", async () => {
    reply({ ok: true });
    await expect(openFileExternal("file1")).resolves.toEqual({ ok: true });
    expectNoBody("POST", "/files/file1/open-external", true);
  });
});

describe("revealFileInFileManager", () => {
  it("POST /files/{id}/reveal，无 body、带 Content-Type 双头", async () => {
    reply({ ok: true });
    await expect(revealFileInFileManager("file1")).resolves.toEqual({ ok: true });
    expectNoBody("POST", "/files/file1/reveal", true);
  });
});

// ---- todo providers / sync ----

describe("listTodoProviders", () => {
  it("GET /todo-providers，providers 透传（含空清单）", async () => {
    const body = { ok: true, providers: [{ id: "obsidian", name: "Obsidian" }] };
    reply(body);
    await expect(listTodoProviders()).resolves.toEqual(body);
    expectGet(`${API}/todo-providers`, {});
  });
});

describe("pullTodos", () => {
  it("POST /todos/pull，body {provider}", async () => {
    const body = { ok: true, run: { id: "run1" } };
    reply(body);
    await expect(pullTodos("obsidian")).resolves.toEqual(body);
    expectWrite("POST", "/todos/pull", { provider: "obsidian" });
  });
});

describe("pushTodo", () => {
  it("POST /todos/{todoId}/push，body {provider}", async () => {
    const body = { ok: true, run: { id: "run2" } };
    reply(body);
    await expect(pushTodo("t1", "obsidian")).resolves.toEqual(body);
    expectWrite("POST", "/todos/t1/push", { provider: "obsidian" });
  });
});

describe("todoSyncStatus", () => {
  it("GET /todos/sync-status，entries 透传（含空清单）", async () => {
    reply({ ok: true, entries: [] });
    await expect(todoSyncStatus()).resolves.toEqual({ ok: true, entries: [] });
    expectGet(`${API}/todos/sync-status`, {});
  });
});

// ---- mods ----

describe("listMods", () => {
  it("GET /mods，ModsStatus 原样透传", async () => {
    const body = {
      ok: true,
      mods: [{ name: "hello", status: "loaded", compat: { events: {}, ops: {} } }],
      runtime: { status: "connected", connected: true, brokerPath: "/bin/broker" },
    };
    reply(body);
    await expect(listMods()).resolves.toEqual(body);
    expectGet(`${API}/mods`, {});
  });

  it("零 mod + runtime:null（broker 未解析）原样透传", async () => {
    reply({ ok: true, mods: [], runtime: null });
    await expect(listMods()).resolves.toEqual({ ok: true, mods: [], runtime: null });
  });
});

describe("updateMod", () => {
  it("PUT /mods/{name}，enabled/config/grants patch 原样序列化", async () => {
    const patch = { enabled: false, config: { k: "v" }, grants: { "fs.read": true } };
    const body = { ok: true, name: "hello", enabled: false };
    reply(body);
    await expect(updateMod("hello", patch)).resolves.toEqual(body);
    expectWrite("PUT", "/mods/hello", patch);
  });

  it("name 走 encodeURIComponent（空格/斜杠不破坏路径）", async () => {
    reply({ ok: true });
    await updateMod("my mod/x", { enabled: true });
    expectWrite("PUT", `/mods/${encodeURIComponent("my mod/x")}`, { enabled: true });
  });
});

describe("uninstallMod", () => {
  it("DELETE /mods/{encoded name}，无 body、仅 locale 头", async () => {
    const body = { ok: true, removed: { name: "hello", path: "/mods/hello", scope: "global" } };
    reply(body);
    await expect(uninstallMod("my mod/x")).resolves.toEqual(body);
    expectNoBody("DELETE", `/mods/${encodeURIComponent("my mod/x")}`, false);
  });
});

describe("validateMod", () => {
  it("POST /mods/validate，body {name}", async () => {
    const body = { ok: true, issues: [] };
    reply(body);
    await expect(validateMod("hello")).resolves.toEqual(body);
    expectWrite("POST", "/mods/validate", { name: "hello" });
  });
});

// ---- settings / MCP ----

describe("getSettings", () => {
  it("GET /settings，配置对象原样透传", async () => {
    const body = { locale: "zh-CN", theme: "dark" };
    reply(body);
    await expect(getSettings()).resolves.toEqual(body);
    expectGet(`${API}/settings`, {});
  });
});

describe("putSettings", () => {
  it("PUT /settings，data 原样序列化", async () => {
    const data = { locale: "en", nested: { a: 1 } };
    reply({ ok: true });
    await expect(putSettings(data)).resolves.toEqual({ ok: true });
    expectWrite("PUT", "/settings", data);
  });
});

describe("getMcp", () => {
  it("不传 opts → 裸 /mcp（5s 轮询轻查询）", async () => {
    const body = { servers: ["trending"], tools: ["mcp_trending_list"] };
    reply(body);
    await expect(getMcp()).resolves.toEqual(body);
    expectGet(`${API}/mcp`, {});
  });

  it("空 opts 对象 → 同样裸 /mcp", async () => {
    reply({ servers: [], tools: [] });
    await getMcp({});
    expectGet(`${API}/mcp`, {});
  });

  it("tools:false → 不带 ?tools=1", async () => {
    reply({ servers: [], tools: [] });
    await getMcp({ tools: false });
    expectGet(`${API}/mcp`, {});
  });

  it("tools:true → ?tools=1，status/toolDetails 透传", async () => {
    const body = {
      servers: ["trending"],
      tools: [],
      status: [{ name: "trending", connected: true, tools: 3, toolDetails: [] }],
    };
    reply(body);
    await expect(getMcp({ tools: true })).resolves.toEqual(body);
    expectGet(`${API}/mcp?tools=1`, { tools: "1" });
  });
});

describe("getMcpConfig", () => {
  it("GET /mcp/config，mcpServers 原样透传", async () => {
    const body = { mcpServers: { trending: { command: "npx" } } };
    reply(body);
    await expect(getMcpConfig()).resolves.toEqual(body);
    expectGet(`${API}/mcp/config`, {});
  });

  it("parseError 非空（mcp.json 损坏）原样透传——UI 据此锁写", async () => {
    const body = { mcpServers: {}, parseError: "Unexpected token }" };
    reply(body);
    await expect(getMcpConfig()).resolves.toEqual(body);
  });
});

describe("putMcp", () => {
  it("PUT /mcp，data 原样序列化", async () => {
    const data = { mcpServers: { trending: { command: "npx", args: ["-y", "x"] } } };
    reply({ ok: true });
    await expect(putMcp(data)).resolves.toEqual({ ok: true });
    expectWrite("PUT", "/mcp", data);
  });
});

describe("reloadMcp", () => {
  it("POST /mcp/reload，无 body、仅 locale 头", async () => {
    const body = { ok: true, servers: ["trending"] };
    reply(body);
    await expect(reloadMcp()).resolves.toEqual(body);
    expectNoBody("POST", "/mcp/reload", false);
  });
});

describe("reconnectMcp", () => {
  it("不传 server → 裸 /mcp/reconnect（全量重连）", async () => {
    const body = { ok: true, status: [] };
    reply(body);
    await expect(reconnectMcp()).resolves.toEqual(body);
    expectNoBody("POST", "/mcp/reconnect", false);
  });

  it("server 走 encodeURIComponent → ?server=…（单 server 绕过冷却）", async () => {
    reply({ ok: true });
    await reconnectMcp("服务器 名");
    expectNoBody("POST", `/mcp/reconnect?server=${encodeURIComponent("服务器 名")}`, false);
  });

  it("空串 server 被 falsy 守卫跳过；ok:false（未配置名）信封透传", async () => {
    reply({ ok: false, error: "未配置的 server" });
    await expect(reconnectMcp("")).resolves.toEqual({ ok: false, error: "未配置的 server" });
    expectNoBody("POST", "/mcp/reconnect", false);
  });
});

describe("testMcp", () => {
  it("不传 timeout → JSON.stringify 丢弃 undefined，body 只有 entry", async () => {
    const entry = { name: "draft", command: "npx", args: ["-y", "pkg"] };
    const body = { ok: true, tools: ["t1"], count: 1 };
    reply(body);
    await expect(testMcp(entry)).resolves.toEqual(body);
    expectWrite("POST", "/mcp/test", { entry });
  });

  it("显式 timeout → body {entry, timeout}", async () => {
    reply({ ok: true });
    await testMcp({ name: "draft" }, 5000);
    expectWrite("POST", "/mcp/test", { entry: { name: "draft" }, timeout: 5000 });
  });

  it("timeout=0 保留（不是 falsy 守卫）；ok:false 带 provider 原始错误透传", async () => {
    reply({ ok: false, error: "connect ECONNREFUSED" });
    await expect(testMcp({ name: "draft" }, 0)).resolves.toEqual({
      ok: false,
      error: "connect ECONNREFUSED",
    });
    expectWrite("POST", "/mcp/test", { entry: { name: "draft" }, timeout: 0 });
  });
});

// ---- skills（写操作）----

describe("createSkill", () => {
  it("POST /skills，body {name, body}", async () => {
    const data = { name: "my-skill", body: "---\nname: my-skill\n---\n# hi" };
    reply({ ok: true });
    await expect(createSkill(data)).resolves.toEqual({ ok: true });
    expectWrite("POST", "/skills", data);
  });
});

describe("deleteSkill", () => {
  it("DELETE /skills/{name}，无 body、仅 locale 头", async () => {
    reply({ ok: true });
    await expect(deleteSkill("my-skill")).resolves.toEqual({ ok: true });
    expectNoBody("DELETE", "/skills/my-skill", false);
  });

  it("name 原样拼路径（与 updateMod 不同，源码未做 encodeURIComponent）", async () => {
    reply({ ok: true });
    await deleteSkill("a b");
    expectNoBody("DELETE", "/skills/a b", false);
  });
});

describe("importSkillsDir", () => {
  it("缺省 overwrite=false → body {path, overwrite:false}", async () => {
    const body = { ok: true, scanned: 2, imported: [], skipped: [], errors: [] };
    reply(body);
    await expect(importSkillsDir("/tmp/skills")).resolves.toEqual(body);
    expectWrite("POST", "/skills/import-dir", { path: "/tmp/skills", overwrite: false });
  });

  it("overwrite=true 显式下发", async () => {
    reply({ ok: true });
    await importSkillsDir("/tmp/skills", true);
    expectWrite("POST", "/skills/import-dir", { path: "/tmp/skills", overwrite: true });
  });
});

// ---- Session Tool Policy ----

describe("mcpStatus", () => {
  it("缺省 includeTools=false → 裸 /mcp", async () => {
    const body = { servers: ["trending"], status: [{ name: "trending", tools: 3 }] };
    reply(body);
    await expect(mcpStatus()).resolves.toEqual(body);
    expectGet(`${API}/mcp`, {});
  });

  it("includeTools=true → ?tools=1", async () => {
    reply({ servers: [] });
    await mcpStatus(true);
    expectGet(`${API}/mcp?tools=1`, { tools: "1" });
  });

  it("includeTools=false 显式 → 裸 /mcp", async () => {
    reply({ servers: [] });
    await mcpStatus(false);
    expectGet(`${API}/mcp`, {});
  });
});

describe("contextBreakdown", () => {
  it("GET /sessions/{id}/context_breakdown，账本载荷原样透传", async () => {
    const body = {
      ok: true,
      used: 12000,
      measured: true,
      window: 200000,
      threshold: 160000,
      counter: "usage",
      tools: [],
      toolsTotal: 0,
      skills: { count: 3, disabled: 0, tokens: 900 },
      system: 3000,
      history: 8000,
      fixed: 100,
      policy: { mode: "all", mcp_disabled: [], mcp_tools_disabled: {}, skills_disabled: [] },
    };
    reply(body);
    await expect(contextBreakdown("s1")).resolves.toEqual(body);
    expectGet(`${API}/sessions/s1/context_breakdown`, {});
  });
});

describe("setToolPolicy", () => {
  it("PUT /sessions/{id}/tool_policy，全字段 body 原样序列化", async () => {
    const body = {
      mode: "custom",
      mcp_disabled: ["trending"],
      mcp_tools_disabled: { map: ["geocode"] },
      skills_disabled: ["banner-design"],
    };
    reply({ ok: true });
    await expect(setToolPolicy("s1", body)).resolves.toEqual({ ok: true });
    expectWrite("PUT", "/sessions/s1/tool_policy", body);
  });

  it("mcp_tools_disabled 不传 → body 无该键；错误信封透传", async () => {
    reply({ ok: false, error: "未知 mode" });
    await expect(
      setToolPolicy("s1", { mode: "x", mcp_disabled: [], skills_disabled: [] }),
    ).resolves.toEqual({ ok: false, error: "未知 mode" });
    expectWrite("PUT", "/sessions/s1/tool_policy", {
      mode: "x",
      mcp_disabled: [],
      skills_disabled: [],
    });
  });
});

describe("getSessionDetail", () => {
  it("GET /sessions/{id}，会话详情原样透传", async () => {
    const body = { id: "s1", title: "补测", tool_policy: { mode: "all" } };
    reply(body);
    await expect(getSessionDetail("s1")).resolves.toEqual(body);
    expectGet(`${API}/sessions/s1`, {});
  });
});

// ---- 错误传播（json helper 不吞错、不拆信封）----

describe("错误传播", () => {
  it("fetch reject → 读操作原样抛出（json 不做 catch）", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    await expect(listFolders()).rejects.toThrow("offline");
  });

  it("fetch reject → 写操作原样抛出", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(putSettings({ a: 1 })).rejects.toThrow("Failed to fetch");
  });
});
