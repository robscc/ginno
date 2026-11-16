/**
 * api/sessions.ts 单测（会话生命周期 / 搜索 / 用量 / goal / 会话文件 /
 * plan 编排 / AgentTeam / running）。
 *
 * 打桩策略：桩 globalThis.fetch，**不 mock ./core 的 json**——真实走
 * URL 拼接 / URLSearchParams 条件拼参 / locale 头合并 / body 序列化。
 * i18n provider 桩成固定 locale zh-CN（provider.tsx 拉 next-intl/runtime
 * 全家桶，node 环境不可用）。node 无 window → BASE 回落 127.0.0.1:8787/api。
 *
 * 断言口径：读操作断言完整 origin+pathname+精确参数集；写操作断言
 * method + JSON.parse(body) 深比较（验蛇形键名，不锁键序）+ 双头合并。
 */
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../../i18n/provider", () => ({ currentLocale: () => "zh-CN" }));

import {
  addTeamMember,
  addTeamMemberSession,
  bulkSessions,
  cancelSessionPlan,
  cleanupOrphanSessionDirs,
  clearSessionGoal,
  confirmSessionPlan,
  createSession,
  createTeam,
  createTeamFromSpec,
  deleteSession,
  deleteSessionDir,
  deleteSessionFile,
  deleteTeam,
  deleteTeamMemberSession,
  deliverToMember,
  detachLinkedSession,
  dismissLastwords,
  distillSession,
  exportProject,
  exportSessionUrl,
  fixWebSearchSearxng,
  forkSession,
  generateTeam,
  getDiagnostics,
  getLinkedSessions,
  getMemberNotes,
  getSessionGoal,
  getSessionHistory,
  getSessionUsage,
  getSuggestions,
  getTeam,
  getTeamMd,
  getTeamUsage,
  listRunning,
  listSessionDirFiles,
  listSessionFileDirs,
  listSessionPlans,
  listSessions,
  listTeams,
  markSessionSeen,
  markTeamMemberSeen,
  mountLinkedSession,
  patchSession,
  patchTeam,
  patchTeamMember,
  putMemberNotes,
  putTeamMd,
  removeTeamMember,
  resumeLastwords,
  revealSessionFile,
  searchAll,
  searchSessions,
  setSessionGoal,
  setSessionTags,
  stopSession,
  subagentPlanCancelFrame,
  subagentPlanConfirmFrame,
  type TeamSpec,
} from "./sessions";
import { BASE } from "./core";
import type { Goal, SessionMeta } from "../types";

const API = "http://127.0.0.1:8787/api";

/** 桩 fetch：返回 { json: async () => payload }，记录全部调用。 */
function stubFetch(payload: unknown = { ok: true }) {
  const mock = vi.fn().mockImplementation(async () => ({ json: async () => payload }));
  vi.stubGlobal("fetch", mock);
  return mock;
}

type Mock = ReturnType<typeof vi.fn>;

/** 取唯一一次请求。 */
function onlyRequest(mock: Mock) {
  expect(mock).toHaveBeenCalledTimes(1);
  const [url, init] = mock.mock.calls[0] as [string, RequestInit | undefined];
  return { url, init };
}

/** 断言唯一一次 GET：精确 URL + 仅 locale 头 + 无 method/body。 */
function expectGet(mock: Mock, url: string) {
  const { url: got, init } = onlyRequest(mock);
  expect(got).toBe(url);
  expect(init?.method ?? "GET").toBe("GET");
  expect(init?.body).toBeUndefined();
  expect(init?.headers).toEqual({ "X-Ginno-Language": "zh-CN" });
}

/**
 * 断言唯一一次写请求：URL + method + body 深比较 + 头合并。
 * body 缺省 = 无 body；ct 缺省按「有 body 才带 Content-Type」推断，
 * cleanupOrphanSessionDirs 这类「带 H 但无 body」的显式传 ct:true。
 */
function expectWrite(
  mock: Mock,
  method: "POST" | "PUT" | "PATCH" | "DELETE",
  url: string,
  opts: { body?: unknown; ct?: boolean } = {},
) {
  const { url: got, init } = onlyRequest(mock);
  expect(got).toBe(url);
  expect(init?.method).toBe(method);
  if (opts.body === undefined) {
    expect(init?.body).toBeUndefined();
  } else {
    expect(typeof init?.body).toBe("string");
    expect(JSON.parse(init!.body as string)).toEqual(opts.body);
  }
  const ct = opts.ct ?? opts.body !== undefined;
  expect(init?.headers).toEqual(
    ct
      ? { "X-Ginno-Language": "zh-CN", "Content-Type": "application/json" }
      : { "X-Ginno-Language": "zh-CN" },
  );
}

/** query 参数集深比较（多一个少一个都炸）。 */
function paramsOf(url: string) {
  return Object.fromEntries(new URL(url).searchParams);
}

const META: SessionMeta = {
  id: "s1",
  title: "测试会话",
  icon: "💬",
  agent_id: null,
  provider: "glm",
  model: "glm-5.3",
  created: 1_700_000_000,
  updated: 1_700_000_001,
};

const GOAL: Goal = {
  goal_id: "g1",
  objective: "完成覆盖率补测",
  status: "active",
  time_used_seconds: 60,
  turns_used: 2,
  created_at: 1_700_000_000,
  updated_at: 1_700_000_001,
};

const SPEC: TeamSpec = {
  name: "调研小队",
  charter_md: "# charter",
  members: [
    { agent_id: "dev", display_name: "开发", role: "leader", instructions: "写代码" },
    { agent_id: "review", display_name: "评审", role: "member", instructions: "审代码" },
  ],
  first_task: "开始调研",
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("BASE 回落（node 无 window）", () => {
  it("BASE 为 127.0.0.1:8787/api", () => {
    expect(BASE).toBe(API);
  });
});

// ---- 会话列表 / 搜索 / 批量 ----

describe("listSessions", () => {
  it("缺省 project_slug=default", async () => {
    const mock = stubFetch([META]);
    await expect(listSessions()).resolves.toEqual([META]);
    expectGet(mock, `${API}/sessions?project_slug=default`);
  });

  it("自定义 project_slug 原样拼接", async () => {
    const mock = stubFetch([]);
    await expect(listSessions("proj-x")).resolves.toEqual([]);
    expectGet(mock, `${API}/sessions?project_slug=proj-x`);
  });
});

describe("searchSessions 条件拼参", () => {
  it("不传 opts：仅 q", async () => {
    const payload = { ok: true, total: 0, limit: 20, offset: 0, sessions: [] };
    const mock = stubFetch(payload);
    await expect(searchSessions("hello")).resolves.toEqual(payload);
    expectGet(mock, `${API}/sessions/search?q=hello`);
  });

  it("q 含空格/中文：URLSearchParams 编码（空格→+）", async () => {
    const mock = stubFetch({ ok: true, total: 0, limit: 20, offset: 0, sessions: [] });
    await searchSessions("你好 world");
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${API}/sessions/search?q=%E4%BD%A0%E5%A5%BD+world`);
    expect(paramsOf(url)).toEqual({ q: "你好 world" });
  });

  it("limit/offset/project_slug 全传：精确参数集", async () => {
    const mock = stubFetch({ ok: true, total: 1, limit: 20, offset: 40, sessions: [META] });
    await searchSessions("x", { limit: 20, offset: 40, project_slug: "proj" });
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${API}/sessions/search?q=x&limit=20&offset=40&project_slug=proj`);
    expect(paramsOf(url)).toEqual({ q: "x", limit: "20", offset: "40", project_slug: "proj" });
  });

  it("limit=0 / offset=0 必须发送（!= null 判断，0 不是缺省）", async () => {
    const mock = stubFetch({ ok: true, total: 0, limit: 0, offset: 0, sessions: [] });
    await searchSessions("x", { limit: 0, offset: 0 });
    const { url } = onlyRequest(mock);
    expect(paramsOf(url)).toEqual({ q: "x", limit: "0", offset: "0" });
  });

  it("只传 offset：limit/project_slug 不出现", async () => {
    const mock = stubFetch({ ok: true, total: 0, limit: 20, offset: 5, sessions: [] });
    await searchSessions("x", { offset: 5 });
    const { url } = onlyRequest(mock);
    expect(paramsOf(url)).toEqual({ q: "x", offset: "5" });
  });

  it("project_slug 空串被 falsy 守卫跳过", async () => {
    const mock = stubFetch({ ok: true, total: 0, limit: 20, offset: 0, sessions: [] });
    await searchSessions("x", { project_slug: "" });
    const { url } = onlyRequest(mock);
    expect(paramsOf(url)).toEqual({ q: "x" });
  });
});

describe("bulkSessions", () => {
  it.each([["archive"], ["unarchive"], ["delete"]] as const)(
    "action=%s：POST /sessions/bulk，body {action, ids}",
    async (action) => {
      const payload = { ok: true, done: ["s1", "s2"], skipped: [] };
      const mock = stubFetch(payload);
      await expect(bulkSessions(action, ["s1", "s2"])).resolves.toEqual(payload);
      expectWrite(mock, "POST", `${API}/sessions/bulk`, { body: { action, ids: ["s1", "s2"] } });
    },
  );

  it("空 ids 数组照常发送", async () => {
    const mock = stubFetch({ ok: true, done: [], skipped: [] });
    await bulkSessions("delete", []);
    expectWrite(mock, "POST", `${API}/sessions/bulk`, { body: { action: "delete", ids: [] } });
  });

  it("delete 被引用会话 → skipped 带 references 透传（不抛错）", async () => {
    const payload = {
      ok: true,
      done: [],
      skipped: [{ id: "s1", reason: "referenced", references: ["team:t1"] }],
    };
    const mock = stubFetch(payload);
    await expect(bulkSessions("delete", ["s1"])).resolves.toEqual(payload);
  });
});

describe("searchAll 统一搜索", () => {
  it("不传 opts：仅 q", async () => {
    const payload = {
      ok: true, q: "kw", scope: "all",
      index: { indexing: false, last_build: null, indexed: 0 },
      sessions: [], messages: [],
    };
    const mock = stubFetch(payload);
    await expect(searchAll("kw")).resolves.toEqual(payload);
    expectGet(mock, `${API}/search?q=kw`);
  });

  it("scope/limit/project_slug 全传（limit=0 仍发送）", async () => {
    const mock = stubFetch({ ok: true, q: "kw", scope: "messages", index: { indexing: false, last_build: 1, indexed: 3 }, sessions: [], messages: [] });
    await searchAll("kw", { scope: "messages", limit: 0, project_slug: "p" });
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${API}/search?q=kw&scope=messages&limit=0&project_slug=p`);
    expect(paramsOf(url)).toEqual({ q: "kw", scope: "messages", limit: "0", project_slug: "p" });
  });

  it("scope=archived 单独传", async () => {
    const mock = stubFetch({ ok: true, q: "kw", scope: "archived", index: { indexing: false, last_build: null, indexed: 0 }, sessions: [], messages: [] });
    await searchAll("kw", { scope: "archived" });
    const { url } = onlyRequest(mock);
    expect(paramsOf(url)).toEqual({ q: "kw", scope: "archived" });
  });

  it("消息命中结构透传（SearchMessageHit 蛇形键）", async () => {
    const hit = {
      session_id: "s1", message_id: "m1", role: "user",
      snippet: "…覆盖率…", session_title: "测试", archived: false,
    };
    const payload = { ok: true, q: "覆盖率", scope: "all", index: { indexing: true, last_build: null, indexed: 0 }, sessions: [], messages: [hit] };
    const mock = stubFetch(payload);
    await expect(searchAll("覆盖率")).resolves.toEqual(payload);
  });
});

// ---- 导出 / 建议 / 蒸馏 ----

describe("exportSessionUrl（同步，不发请求）", () => {
  it("拼接 format=md 下载 URL", () => {
    expect(exportSessionUrl("s1")).toBe(`${API}/export/session/s1?format=md`);
  });

  it("id 特殊字符 encodeURIComponent", () => {
    expect(exportSessionUrl("a b/c")).toBe(`${API}/export/session/a%20b%2Fc?format=md`);
  });
});

describe("exportProject", () => {
  it("缺省 project_slug=default", async () => {
    const mock = stubFetch({ ok: true });
    await expect(exportProject()).resolves.toEqual({ ok: true });
    expectWrite(mock, "POST", `${API}/export/project`, { body: { project_slug: "default" } });
  });

  it("自定义 slug + 错误响应透传", async () => {
    const mock = stubFetch({ ok: false, error: "no sessions" });
    await expect(exportProject("proj")).resolves.toEqual({ ok: false, error: "no sessions" });
    expectWrite(mock, "POST", `${API}/export/project`, { body: { project_slug: "proj" } });
  });
});

describe("getSuggestions", () => {
  it("GET /sessions/{id}/suggestions，建议列表透传", async () => {
    const payload = {
      ok: true,
      suggestions: [{ id: "tip1", severity: "high", title: "t", detail: "d", action: { type: "open_settings" } }],
    };
    const mock = stubFetch(payload);
    await expect(getSuggestions("s1")).resolves.toEqual(payload);
    expectGet(mock, `${API}/sessions/s1/suggestions`);
  });

  it("自动化会话被排除 → excluded 透传", async () => {
    const mock = stubFetch({ ok: true, excluded: "automation", suggestions: [] });
    await expect(getSuggestions("s2")).resolves.toEqual({ ok: true, excluded: "automation", suggestions: [] });
    expectGet(mock, `${API}/sessions/s2/suggestions`);
  });
});

describe("distillSession", () => {
  it.each([["skill"], ["kb"], ["workflow"]] as const)("kind=%s：POST distill", async (kind) => {
    const payload = { ok: true, kind, name: "n", path: "/p" };
    const mock = stubFetch(payload);
    await expect(distillSession("s1", kind)).resolves.toEqual(payload);
    expectWrite(mock, "POST", `${API}/sessions/s1/distill`, { body: { kind } });
  });

  it("session_id 编码 + 失败 detail 透传", async () => {
    const mock = stubFetch({ ok: false, detail: { error: "too short" } });
    await expect(distillSession("s/1", "skill")).resolves.toEqual({ ok: false, detail: { error: "too short" } });
    expectWrite(mock, "POST", `${API}/sessions/s%2F1/distill`, { body: { kind: "skill" } });
  });
});

// ---- 团队生成 / 诊断 ----

describe("generateTeam", () => {
  it("缺省 project_slug=default", async () => {
    const payload = { ok: true, spec: SPEC };
    const mock = stubFetch(payload);
    await expect(generateTeam("组一个调研队")).resolves.toEqual(payload);
    expectWrite(mock, "POST", `${API}/teams/generate`, { body: { prompt: "组一个调研队", project_slug: "default" } });
  });

  it("自定义 slug + 生成失败 detail.raw 透传", async () => {
    const mock = stubFetch({ ok: false, detail: { error: "llm down", raw: "…" } });
    await generateTeam("p", "proj");
    expectWrite(mock, "POST", `${API}/teams/generate`, { body: { prompt: "p", project_slug: "proj" } });
  });
});

describe("createTeamFromSpec", () => {
  it("spec 原样作为 body（含可选 project_slug/workspace）", async () => {
    const spec = { ...SPEC, project_slug: "proj", workspace: "/w" };
    const payload = { ok: true, team: { id: "t1" }, member_errors: [], first_task_delivered: true };
    const mock = stubFetch(payload);
    await expect(createTeamFromSpec(spec)).resolves.toEqual(payload);
    expectWrite(mock, "POST", `${API}/teams/from_spec`, { body: spec });
  });

  it("纯 spec（无可选字段）：body 不含 project_slug/workspace", async () => {
    const mock = stubFetch({ ok: true });
    await createTeamFromSpec(SPEC);
    expectWrite(mock, "POST", `${API}/teams/from_spec`, { body: SPEC });
  });
});

describe("getDiagnostics", () => {
  it("GET /diagnostics，报告透传", async () => {
    const payload = { ok: true, generated_at: 1, warnings: 0, issues: [], sections: {} };
    const mock = stubFetch(payload);
    await expect(getDiagnostics()).resolves.toEqual(payload);
    expectGet(mock, `${API}/diagnostics`);
  });
});

describe("fixWebSearchSearxng", () => {
  it("POST fix 端点，body 蛇形 session_id", async () => {
    const payload = { ok: true, session_id: "s-new" };
    const mock = stubFetch(payload);
    await expect(fixWebSearchSearxng("s-new")).resolves.toEqual(payload);
    expectWrite(mock, "POST", `${API}/diagnostics/fix/web-search-searxng`, { body: { session_id: "s-new" } });
  });

  it("失败 error 透传", async () => {
    const mock = stubFetch({ ok: false, error: "install failed" });
    await expect(fixWebSearchSearxng("s1")).resolves.toEqual({ ok: false, error: "install failed" });
  });
});

// ---- 会话生命周期 ----

describe("markSessionSeen", () => {
  it("POST seen：无 body、仅 locale 头（fire-and-forget）", async () => {
    const mock = stubFetch({ ok: true });
    await expect(markSessionSeen("s1")).resolves.toEqual({ ok: true });
    expectWrite(mock, "POST", `${API}/sessions/s1/seen`);
  });

  it("session_id 编码", async () => {
    const mock = stubFetch({ ok: true });
    await markSessionSeen("s/1");
    expectWrite(mock, "POST", `${API}/sessions/s%2F1/seen`);
  });
});

describe("lastwords 恢复确认（resume / dismiss 同端点双语义）", () => {
  it("resumeLastwords：POST /lastwords/resume，body 只带 session_id（无 dismiss）", async () => {
    const mock = stubFetch({ ok: true });
    await expect(resumeLastwords("s1")).resolves.toEqual({ ok: true });
    expectWrite(mock, "POST", `${API}/lastwords/resume`, { body: { session_id: "s1" } });
  });

  it("dismissLastwords：同一端点，body 带 dismiss:true", async () => {
    const mock = stubFetch({ ok: true, had: true });
    await expect(dismissLastwords("s1")).resolves.toEqual({ ok: true, had: true });
    expectWrite(mock, "POST", `${API}/lastwords/resume`, {
      body: { session_id: "s1", dismiss: true },
    });
  });

  it("resume 的 no_pending 语义原样透传（调用方据此提示而非重试）", async () => {
    const mock = stubFetch({ ok: false, error: "no_pending" });
    await expect(resumeLastwords("gone")).resolves.toEqual({ ok: false, error: "no_pending" });
    expectWrite(mock, "POST", `${API}/lastwords/resume`, { body: { session_id: "gone" } });
  });

  it("dismiss 的 had:false（本来就没有条目）幂等透传", async () => {
    const mock = stubFetch({ ok: true, had: false });
    await expect(dismissLastwords("none")).resolves.toEqual({ ok: true, had: false });
  });

  it("网络异常 reject（不吞错，交由调用方 catch 出错误通知）", async () => {
    const mock = vi.fn().mockImplementation(async () => {
      throw new Error("ECONNREFUSED");
    });
    vi.stubGlobal("fetch", mock);
    await expect(resumeLastwords("s1")).rejects.toThrow("ECONNREFUSED");
    await expect(dismissLastwords("s1")).rejects.toThrow("ECONNREFUSED");
  });
});

describe("forkSession", () => {
  it("成功：new_id/copied 透传", async () => {
    const payload = { ok: true, new_id: "s2", copied: 12 };
    const mock = stubFetch(payload);
    await expect(forkSession("s1")).resolves.toEqual(payload);
    expectWrite(mock, "POST", `${API}/sessions/s1/fork`);
  });

  it("已有副本 → existing:true 语义透传（不抛错）", async () => {
    const mock = stubFetch({ ok: true, existing: true, new_id: "s2" });
    await expect(forkSession("s1")).resolves.toEqual({ ok: true, existing: true, new_id: "s2" });
  });

  it("busy → ok:false + error 透传", async () => {
    const mock = stubFetch({ ok: false, error: "busy" });
    await expect(forkSession("s1")).resolves.toEqual({ ok: false, error: "busy" });
  });
});

describe("createSession", () => {
  it("最小请求：注入缺省 project_slug=default", async () => {
    const mock = stubFetch({ ...META, ok: true });
    await expect(createSession({ workspace: "/w" })).resolves.toEqual({ ...META, ok: true });
    expectWrite(mock, "POST", `${API}/sessions`, { body: { project_slug: "default", workspace: "/w" } });
  });

  it("req.project_slug 覆盖缺省值", async () => {
    const mock = stubFetch(META);
    await createSession({ workspace: "/w", project_slug: "proj" });
    expectWrite(mock, "POST", `${API}/sessions`, { body: { project_slug: "proj", workspace: "/w" } });
  });

  it("全量可选字段：蛇形键 + primary_folder:null 保留", async () => {
    const req = {
      project_slug: "p", workspace: "/w", agent_id: "dev", title: "t", icon: "🤖",
      provider: "glm", model: "glm-5.3", thinking_effort: "high", workflow_id: "wf1",
      context_folders: ["/c1", "/c2"], primary_folder: null, type: "quick",
    };
    const mock = stubFetch(META);
    await createSession(req);
    expectWrite(mock, "POST", `${API}/sessions`, { body: req });
  });

  it("remote_peer 透传（remote-session-design.md R3：远端执行方建会话）", async () => {
    const mock = stubFetch(META);
    await createSession({ workspace: "/w", remote_peer: "expense-bot" });
    expectWrite(mock, "POST", `${API}/sessions`, {
      body: { project_slug: "default", workspace: "/w", remote_peer: "expense-bot" },
    });
  });

  it("不传 remote_peer 时 body 不含该键（本地会话契约不变）", async () => {
    const mock = stubFetch(META);
    await createSession({ workspace: "/w", agent_id: "dev" });
    const [, init] = mock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).not.toHaveProperty("remote_peer");
  });
});

describe("patchSession", () => {
  it("PATCH /sessions/{id}，patch 原样 body", async () => {
    const payload = { ok: true, session: { ...META, title: "新标题" } };
    const mock = stubFetch(payload);
    await expect(patchSession("s1", { title: "新标题", pinned: true })).resolves.toEqual(payload);
    expectWrite(mock, "PATCH", `${API}/sessions/s1`, { body: { title: "新标题", pinned: true } });
  });

  it("空 patch → body {}", async () => {
    const mock = stubFetch({ ok: true, session: META });
    await patchSession("s1", {});
    expectWrite(mock, "PATCH", `${API}/sessions/s1`, { body: {} });
  });
});

describe("setSessionTags", () => {
  it("PUT tags：icon+labels 全量覆盖", async () => {
    const payload = { ok: true, session: META };
    const mock = stubFetch(payload);
    await expect(setSessionTags("s1", "star", ["工作", "重要"])).resolves.toEqual(payload);
    expectWrite(mock, "PUT", `${API}/sessions/s1/tags`, { body: { icon: "star", labels: ["工作", "重要"] } });
  });

  it("清除语义：icon=null + labels=[] 原样发送", async () => {
    const mock = stubFetch({ ok: true, session: META });
    await setSessionTags("s1", null, []);
    expectWrite(mock, "PUT", `${API}/sessions/s1/tags`, { body: { icon: null, labels: [] } });
  });
});

describe("deleteSession", () => {
  it("缺省 cascade=false：URL 不带 query", async () => {
    const mock = stubFetch({ ok: true, removed: true });
    await expect(deleteSession("s1")).resolves.toEqual({ ok: true, removed: true });
    expectWrite(mock, "DELETE", `${API}/sessions/s1`);
  });

  it("cascade=true → ?cascade=1", async () => {
    const mock = stubFetch({ ok: true, removed: true });
    await deleteSession("s1", true);
    expectWrite(mock, "DELETE", `${API}/sessions/s1?cascade=1`);
  });

  it("409 被引用：ok:false + references 透传（core.json 不看 status）", async () => {
    const payload = { ok: false, error: "referenced", references: ["team:t1", "run:r1"] };
    const mock = stubFetch(payload);
    await expect(deleteSession("s1")).resolves.toEqual(payload);
  });
});

describe("getSessionHistory", () => {
  it("GET history：messages + last_error(i18n_key) 透传", async () => {
    const payload = {
      ok: true,
      messages: [{ id: "m1", role: "user", blocks: [{ type: "text", text: "hi" }], turnId: "t1", teamReply: null }],
      last_error: { turn_id: "t1", message: "boom", i18n_key: "err.boom", params: { code: 500 } },
    };
    const mock = stubFetch(payload);
    await expect(getSessionHistory("s1")).resolves.toEqual(payload);
    expectGet(mock, `${API}/sessions/s1/history`);
  });

  it("空会话：messages=[] + last_error=null", async () => {
    const mock = stubFetch({ ok: true, messages: [], last_error: null });
    await expect(getSessionHistory("s1")).resolves.toEqual({ ok: true, messages: [], last_error: null });
  });
});

describe("stopSession", () => {
  it("POST stop：body 为空对象 {}，带双头", async () => {
    const mock = stubFetch({ stopped: true });
    await expect(stopSession("s1")).resolves.toEqual({ stopped: true });
    expectWrite(mock, "POST", `${API}/sessions/s1/stop`, { body: {} });
  });

  it("idle no-op → stopped:false 透传", async () => {
    const mock = stubFetch({ stopped: false });
    await expect(stopSession("s1")).resolves.toEqual({ stopped: false });
  });
});

describe("getSessionUsage", () => {
  it("GET usage：累计用量 + cache_hit_ratio 透传", async () => {
    const payload = {
      ok: true,
      usage: { input_tokens: 100, output_tokens: 50, cache_read_tokens: 80, cache_creation_tokens: 10, calls: 3, cache_hit_ratio: 0.8 },
    };
    const mock = stubFetch(payload);
    await expect(getSessionUsage("s1")).resolves.toEqual(payload);
    expectGet(mock, `${API}/sessions/s1/usage`);
  });

  it("无用量记录 → usage:null", async () => {
    const mock = stubFetch({ ok: true, usage: null });
    await expect(getSessionUsage("s1")).resolves.toEqual({ ok: true, usage: null });
  });
});

// ---- 关联会话 ----

describe("linked sessions", () => {
  it("getLinkedSessions：GET linked，members/incoming/live 透传", async () => {
    const payload = {
      ok: true,
      members: [{ id: "s2", title: "子", type: "subagent", rel: "spawned", live_status: "working", updated: 1, terminal: false }],
      incoming: [],
      live: 1,
    };
    const mock = stubFetch(payload);
    await expect(getLinkedSessions("s1")).resolves.toEqual(payload);
    expectGet(mock, `${API}/sessions/s1/linked`);
  });

  it("mountLinkedSession：POST linked，body {target}", async () => {
    const mock = stubFetch({ ok: true });
    await expect(mountLinkedSession("s1", "s2")).resolves.toEqual({ ok: true });
    expectWrite(mock, "POST", `${API}/sessions/s1/linked`, { body: { target: "s2" } });
  });

  it("detachLinkedSession：DELETE linked/{target}，无 body", async () => {
    const mock = stubFetch({ ok: true });
    await expect(detachLinkedSession("s1", "s2")).resolves.toEqual({ ok: true });
    expectWrite(mock, "DELETE", `${API}/sessions/s1/linked/s2`);
  });

  it("旧 sidecar 无端点 → error 透传（前端据此隐藏 chip）", async () => {
    const mock = stubFetch({ ok: false, error: "not found" });
    await expect(getLinkedSessions("s1")).resolves.toEqual({ ok: false, error: "not found" });
  });
});

// ---- goal ----

describe("session goal", () => {
  it("getSessionGoal：GET goal，完整 goal 透传", async () => {
    const mock = stubFetch({ ok: true, goal: GOAL });
    await expect(getSessionGoal("s1")).resolves.toEqual({ ok: true, goal: GOAL });
    expectGet(mock, `${API}/sessions/s1/goal`);
  });

  it("getSessionGoal：无 goal → null", async () => {
    const mock = stubFetch({ ok: true, goal: null });
    await expect(getSessionGoal("s1")).resolves.toEqual({ ok: true, goal: null });
  });

  it("setSessionGoal：PUT goal，objective/status/confirm 原样 body", async () => {
    const body = { objective: "跑完全量测试", status: "active" as const, confirm: true };
    const payload = { ok: true, goal: GOAL, needs_confirm: false };
    const mock = stubFetch(payload);
    await expect(setSessionGoal("s1", body)).resolves.toEqual(payload);
    expectWrite(mock, "PUT", `${API}/sessions/s1/goal`, { body });
  });

  it("setSessionGoal：needs_confirm:true 语义透传", async () => {
    const mock = stubFetch({ ok: true, needs_confirm: true });
    await expect(setSessionGoal("s1", { status: "complete" })).resolves.toEqual({ ok: true, needs_confirm: true });
    expectWrite(mock, "PUT", `${API}/sessions/s1/goal`, { body: { status: "complete" } });
  });

  it("clearSessionGoal：DELETE goal，无 body", async () => {
    const mock = stubFetch({ ok: true, cleared: true });
    await expect(clearSessionGoal("s1")).resolves.toEqual({ ok: true, cleared: true });
    expectWrite(mock, "DELETE", `${API}/sessions/s1/goal`);
  });
});

// ---- WS 上行帧（同步纯函数，不发请求）----

describe("subagentPlanConfirmFrame", () => {
  it("不传 subtasks：帧不含 subtasks 键（原样采纳）", () => {
    expect(subagentPlanConfirmFrame("p1")).toEqual({ type: "subagent.plan.confirm", plan_id: "p1" });
  });

  it("空数组 subtasks：length=0 falsy → 同样省略", () => {
    expect(subagentPlanConfirmFrame("p1", [])).toEqual({ type: "subagent.plan.confirm", plan_id: "p1" });
  });

  it("非空 subtasks：携带用户编辑版", () => {
    const subtasks = [{ goal: "子任务1", constraints: "只读", acceptance: "有测试", reason: "拆分" }];
    expect(subagentPlanConfirmFrame("p1", subtasks)).toEqual({
      type: "subagent.plan.confirm", plan_id: "p1", subtasks,
    });
  });
});

describe("subagentPlanCancelFrame", () => {
  it("固定形状 {type, plan_id}", () => {
    expect(subagentPlanCancelFrame("p1")).toEqual({ type: "subagent.plan.cancel", plan_id: "p1" });
  });
});

// ---- 会话文件 ----

describe("session files", () => {
  it("listSessionFileDirs：GET dirs", async () => {
    const payload = { ok: true, sessions: [{ project_slug: "default", session_id: "s1", files: 2, bytes: 100 }] };
    const mock = stubFetch(payload);
    await expect(listSessionFileDirs()).resolves.toEqual(payload);
    expectGet(mock, `${API}/session-files/dirs`);
  });

  it("listSessionDirFiles：不传 sub → 无 &sub", async () => {
    const payload = { ok: true, path: "/p", entries: [] };
    const mock = stubFetch(payload);
    await expect(listSessionDirFiles("default", "s1")).resolves.toEqual(payload);
    expectGet(mock, `${API}/session-files/list?project_slug=default&session_id=s1`);
  });

  it("listSessionDirFiles：sub 空串被 falsy 守卫跳过", async () => {
    const mock = stubFetch({ ok: true, path: "/p", entries: [] });
    await listSessionDirFiles("default", "s1", "");
    expectGet(mock, `${API}/session-files/list?project_slug=default&session_id=s1`);
  });

  it("listSessionDirFiles：sub 编码拼接", async () => {
    const mock = stubFetch({ ok: true, path: "/p", entries: [] });
    await listSessionDirFiles("default", "s1", "a b/c");
    expectGet(mock, `${API}/session-files/list?project_slug=default&session_id=s1&sub=a%20b%2Fc`);
  });

  it("deleteSessionFile：DELETE file，三键 body", async () => {
    const mock = stubFetch({ ok: true });
    await expect(deleteSessionFile("default", "s1", "a.md")).resolves.toEqual({ ok: true });
    expectWrite(mock, "DELETE", `${API}/session-files/file`, { body: { project_slug: "default", session_id: "s1", path: "a.md" } });
  });

  it("deleteSessionFile：unregistered 语义透传", async () => {
    const mock = stubFetch({ ok: true, unregistered: true });
    await expect(deleteSessionFile("default", "s1", "a.md")).resolves.toEqual({ ok: true, unregistered: true });
  });

  it("deleteSessionDir：传 path → body 含 path", async () => {
    const mock = stubFetch({ ok: true, files_removed: 3 });
    await expect(deleteSessionDir("default", "s1", "sub")).resolves.toEqual({ ok: true, files_removed: 3 });
    expectWrite(mock, "DELETE", `${API}/session-files/dir`, { body: { project_slug: "default", session_id: "s1", path: "sub" } });
  });

  it("deleteSessionDir：不传 path → JSON 序列化省略 undefined 键", async () => {
    const mock = stubFetch({ ok: true, files_removed: 5 });
    await deleteSessionDir("default", "s1");
    expectWrite(mock, "DELETE", `${API}/session-files/dir`, { body: { project_slug: "default", session_id: "s1" } });
  });

  it("revealSessionFile：POST reveal，三键 body", async () => {
    const mock = stubFetch({ ok: true });
    await expect(revealSessionFile("default", "s1", "a.md")).resolves.toEqual({ ok: true });
    expectWrite(mock, "POST", `${API}/session-files/reveal`, { body: { project_slug: "default", session_id: "s1", path: "a.md" } });
  });

  it("cleanupOrphanSessionDirs：POST 无 body 但带 JSON 头（H）", async () => {
    const payload = { ok: true, removed: [{ project_slug: "default", session_id: "s9", bytes: 10 }], failed: [], freed_bytes: 10 };
    const mock = stubFetch(payload);
    await expect(cleanupOrphanSessionDirs()).resolves.toEqual(payload);
    expectWrite(mock, "POST", `${API}/session-files/cleanup-orphans`, { ct: true });
  });
});

// ---- plan 编排 ----

describe("session plans", () => {
  it("listSessionPlans：GET plans，空数组透传", async () => {
    const mock = stubFetch([]);
    await expect(listSessionPlans("s1")).resolves.toEqual([]);
    expectGet(mock, `${API}/sessions/s1/plans`);
  });

  it("confirmSessionPlan：POST confirm，无 body、仅 locale 头", async () => {
    const payload = { ok: true, plan: { id: "p1" } };
    const mock = stubFetch(payload);
    await expect(confirmSessionPlan("s1", "p1")).resolves.toEqual(payload);
    expectWrite(mock, "POST", `${API}/sessions/s1/plans/p1/confirm`);
  });

  it("cancelSessionPlan：POST cancel，无 body", async () => {
    const payload = { ok: true, plan: { id: "p1" } };
    const mock = stubFetch(payload);
    await expect(cancelSessionPlan("s1", "p1")).resolves.toEqual(payload);
    expectWrite(mock, "POST", `${API}/sessions/s1/plans/p1/cancel`);
  });

  it("confirm 失败 error 透传", async () => {
    const mock = stubFetch({ ok: false, error: "plan not pending" });
    await expect(confirmSessionPlan("s1", "p1")).resolves.toEqual({ ok: false, error: "plan not pending" });
  });
});

// ---- AgentTeam ----

describe("listTeams", () => {
  it("缺省：仅 client=main", async () => {
    const payload = { ok: true, teams: [] };
    const mock = stubFetch(payload);
    await expect(listTeams()).resolves.toEqual(payload);
    expectGet(mock, `${API}/teams?client=main`);
  });

  it("传 project_slug：client 在前 slug 在后", async () => {
    const mock = stubFetch({ ok: true, teams: [] });
    await listTeams("proj");
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${API}/teams?client=main&project_slug=proj`);
    expect(paramsOf(url)).toEqual({ client: "main", project_slug: "proj" });
  });

  it("自定义 client + 空串 slug 被跳过", async () => {
    const mock = stubFetch({ ok: true, teams: [] });
    await listTeams("", "float");
    const { url } = onlyRequest(mock);
    expect(paramsOf(url)).toEqual({ client: "float" });
  });
});

describe("getTeam", () => {
  it("缺省 client=main，teamId 编码", async () => {
    const payload = { ok: true, team: { id: "t/1" } };
    const mock = stubFetch(payload);
    await expect(getTeam("t/1")).resolves.toEqual(payload);
    expectGet(mock, `${API}/teams/t%2F1?client=main`);
  });

  it("自定义 client 编码 + error 透传", async () => {
    const mock = stubFetch({ ok: false, error: "not found" });
    await expect(getTeam("t1", "side bar")).resolves.toEqual({ ok: false, error: "not found" });
    expectGet(mock, `${API}/teams/t1?client=side%20bar`);
  });
});

describe("getTeamUsage", () => {
  it("缺省 days=30", async () => {
    const payload = { ok: true, days: 30, members: [], total: { input_tokens: 0, output_tokens: 0, total_tokens: 0, calls: 0 } };
    const mock = stubFetch(payload);
    await expect(getTeamUsage("t1")).resolves.toEqual(payload);
    expectGet(mock, `${API}/teams/t1/usage?days=30`);
  });

  it("自定义 days（含 0）原样拼接", async () => {
    const mock = stubFetch({ ok: true, days: 7, members: [] });
    await getTeamUsage("t/1", 7);
    expectGet(mock, `${API}/teams/t%2F1/usage?days=7`);
  });

  it("days=0 不被缺省吞掉", async () => {
    const mock = stubFetch({ ok: true, days: 0, members: [] });
    await getTeamUsage("t1", 0);
    expectGet(mock, `${API}/teams/t1/usage?days=0`);
  });
});

describe("createTeam", () => {
  it("POST /teams，req 原样 body（含挂载字段）", async () => {
    const req = { name: "队", project_slug: "p", agent_id: "dev", workspace: "/w", context_folders: ["/c"], primary_folder: null };
    const payload = { ok: true, team: { id: "t1" } };
    const mock = stubFetch(payload);
    await expect(createTeam(req)).resolves.toEqual(payload);
    expectWrite(mock, "POST", `${API}/teams`, { body: req });
  });

  it("最小 req：仅 name", async () => {
    const mock = stubFetch({ ok: true });
    await createTeam({ name: "队" });
    expectWrite(mock, "POST", `${API}/teams`, { body: { name: "队" } });
  });
});

describe("patchTeam", () => {
  it("PATCH /teams/{enc}，patch 原样 body", async () => {
    const mock = stubFetch({ ok: true, team: { id: "t1" } });
    await expect(patchTeam("t/1", { name: "新名", charter_md: "# c" })).resolves.toEqual({ ok: true, team: { id: "t1" } });
    expectWrite(mock, "PATCH", `${API}/teams/t%2F1`, { body: { name: "新名", charter_md: "# c" } });
  });
});

describe("deleteTeam", () => {
  it("缺省 force=false：无 query", async () => {
    const mock = stubFetch({ ok: true });
    await expect(deleteTeam("t1")).resolves.toEqual({ ok: true });
    expectWrite(mock, "DELETE", `${API}/teams/t1`);
  });

  it("force=true → ?force=1", async () => {
    const mock = stubFetch({ ok: true });
    await deleteTeam("t/1", true);
    expectWrite(mock, "DELETE", `${API}/teams/t%2F1?force=1`);
  });

  it("有活 turn 被拒 → live 点名透传（不抛错）", async () => {
    const payload = { ok: false, error: "live turns", live: ["i1"] };
    const mock = stubFetch(payload);
    await expect(deleteTeam("t1")).resolves.toEqual(payload);
  });
});

describe("团队成员 CRUD", () => {
  it("addTeamMember：POST members，req 原样 body", async () => {
    const req = { agent_id: "dev", display_name: "开发", role: "leader" as const };
    const payload = { ok: true, member: { instance_id: "i1", session_id: "s1" } };
    const mock = stubFetch(payload);
    await expect(addTeamMember("t1", req)).resolves.toEqual(payload);
    expectWrite(mock, "POST", `${API}/teams/t1/members`, { body: req });
  });

  it("addTeamMember：最小 req 仅 agent_id", async () => {
    const mock = stubFetch({ ok: true });
    await addTeamMember("t1", { agent_id: "dev" });
    expectWrite(mock, "POST", `${API}/teams/t1/members`, { body: { agent_id: "dev" } });
  });

  it("patchTeamMember：PATCH members/{enc}，双段编码", async () => {
    const mock = stubFetch({ ok: true, member: { instance_id: "i 2" } });
    await expect(patchTeamMember("t/1", "i 2", { display_name: "新", role: "member" })).resolves.toEqual({ ok: true, member: { instance_id: "i 2" } });
    expectWrite(mock, "PATCH", `${API}/teams/t%2F1/members/i%202`, { body: { display_name: "新", role: "member" } });
  });

  it("removeTeamMember：DELETE members/{enc}，无 body", async () => {
    const mock = stubFetch({ ok: true });
    await expect(removeTeamMember("t1", "i1")).resolves.toEqual({ ok: true });
    expectWrite(mock, "DELETE", `${API}/teams/t1/members/i1`);
  });
});

describe("markTeamMemberSeen", () => {
  it("缺省 client=main：POST seen?client=main，无 body", async () => {
    const mock = stubFetch({ ok: true });
    await expect(markTeamMemberSeen("t1", "i1")).resolves.toEqual({ ok: true });
    expectWrite(mock, "POST", `${API}/teams/t1/members/i1/seen?client=main`);
  });

  it("自定义 client 编码", async () => {
    const mock = stubFetch({ ok: true });
    await markTeamMemberSeen("t/1", "i 2", "side bar");
    expectWrite(mock, "POST", `${API}/teams/t%2F1/members/i%202/seen?client=side%20bar`);
  });
});

describe("deliverToMember", () => {
  it("缺省 client=main：POST deliver，req 原样 body（instance_id 定向）", async () => {
    const req = { instance_id: "i1", text: "继续", wait: true };
    const receipt = { status: "delivered", instance_id: "i1" };
    const mock = stubFetch(receipt);
    await expect(deliverToMember("t1", req)).resolves.toEqual(receipt);
    expectWrite(mock, "POST", `${API}/teams/t1/deliver?client=main`, { body: req });
  });

  it("agent_id 广播 + 自定义 client 编码 + stalled 回执透传", async () => {
    const req = { agent_id: "dev", text: "@全体" };
    const receipt = { status: "stalled", reason: "busy" };
    const mock = stubFetch(receipt);
    await expect(deliverToMember("t/1", req, "side bar")).resolves.toEqual(receipt);
    expectWrite(mock, "POST", `${API}/teams/t%2F1/deliver?client=side%20bar`, { body: req });
  });
});

describe("团队记忆分层", () => {
  it("getTeamMd：GET team-md", async () => {
    const mock = stubFetch({ ok: true, text: "# TEAM" });
    await expect(getTeamMd("t1")).resolves.toEqual({ ok: true, text: "# TEAM" });
    expectGet(mock, `${API}/teams/t1/team-md`);
  });

  it("putTeamMd：PUT team-md，body {text}", async () => {
    const mock = stubFetch({ ok: true });
    await expect(putTeamMd("t/1", "# 新章程")).resolves.toEqual({ ok: true });
    expectWrite(mock, "PUT", `${API}/teams/t%2F1/team-md`, { body: { text: "# 新章程" } });
  });

  it("getMemberNotes：GET members/{enc}/notes", async () => {
    const mock = stubFetch({ ok: true, text: "notes" });
    await expect(getMemberNotes("t1", "i 2")).resolves.toEqual({ ok: true, text: "notes" });
    expectGet(mock, `${API}/teams/t1/members/i%202/notes`);
  });

  it("putMemberNotes：PUT notes，body {text}", async () => {
    const mock = stubFetch({ ok: true });
    await expect(putMemberNotes("t1", "i1", "n")).resolves.toEqual({ ok: true });
    expectWrite(mock, "PUT", `${API}/teams/t1/members/i1/notes`, { body: { text: "n" } });
  });

  it("空文本记忆：text 缺省透传", async () => {
    const mock = stubFetch({ ok: true });
    await expect(getTeamMd("t1")).resolves.toEqual({ ok: true });
    expectGet(mock, `${API}/teams/t1/team-md`);
  });
});

describe("成员多会话", () => {
  it("addTeamMemberSession：POST sessions，无 body", async () => {
    const mock = stubFetch({ ok: true, session_id: "s-new" });
    await expect(addTeamMemberSession("t1", "i1")).resolves.toEqual({ ok: true, session_id: "s-new" });
    expectWrite(mock, "POST", `${API}/teams/t1/members/i1/sessions`);
  });

  it("deleteTeamMemberSession：DELETE sessions/{enc}，三段编码", async () => {
    const mock = stubFetch({ ok: true });
    await expect(deleteTeamMemberSession("t/1", "i 2", "s/3")).resolves.toEqual({ ok: true });
    expectWrite(mock, "DELETE", `${API}/teams/t%2F1/members/i%202/sessions/s%2F3`);
  });
});

// ---- running ----

describe("listRunning", () => {
  it("GET /running：sessions/tasks/waiting/count 透传", async () => {
    const payload = {
      ok: true,
      sessions: [{ id: "s1", title: "跑着" }],
      tasks: [{ id: "t1", title: "后台", command: "sleep 1" }],
      count: 2,
      waiting: [{ id: "s2", kind: "ask_user" }],
    };
    const mock = stubFetch(payload);
    await expect(listRunning()).resolves.toEqual(payload);
    expectGet(mock, `${API}/running`);
  });

  it("空闲：空数组 + count=0", async () => {
    const mock = stubFetch({ ok: true, sessions: [], tasks: [], count: 0 });
    await expect(listRunning()).resolves.toEqual({ ok: true, sessions: [], tasks: [], count: 0 });
  });
});

// ---- 错误传播 ----

describe("错误传播", () => {
  it("fetch 网络异常 → Promise reject", async () => {
    const mock = vi.fn().mockImplementation(async () => {
      throw new Error("sidecar unreachable");
    });
    vi.stubGlobal("fetch", mock);
    await expect(listSessions()).rejects.toThrow("sidecar unreachable");
    expect(mock).toHaveBeenCalledTimes(1);
  });

  it("写请求 fetch 异常同样 reject（不吞错）", async () => {
    const mock = vi.fn().mockImplementation(async () => {
      throw new Error("ECONNREFUSED");
    });
    vi.stubGlobal("fetch", mock);
    await expect(createSession({ workspace: "/w" })).rejects.toThrow("ECONNREFUSED");
  });

  it("响应 body 非法 JSON → r.json() 异常传播", async () => {
    const mock = vi.fn().mockImplementation(async () => ({
      json: async () => {
        throw new SyntaxError("Unexpected end of JSON input");
      },
    }));
    vi.stubGlobal("fetch", mock);
    await expect(getSessionHistory("s1")).rejects.toThrow("Unexpected end of JSON input");
  });
});
