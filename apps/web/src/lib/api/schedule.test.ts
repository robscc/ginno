/**
 * api/schedule.ts（定时任务 API 客户端）单元测试。
 *
 * 打桩策略：桩 globalThis.fetch，走真实 core.json()，**不 mock ./core**；
 * i18n provider 桩成固定 zh-CN。node 无 window → BASE/ws 回落 127.0.0.1:8787。
 *
 * 重点分支：listScheduleRuns 八字段的 falsy 守卫（page=0 / 空串被跳过）、
 * encodeURIComponent、写操作双头 vs 无 body 操作的 locale 单头、
 * 写操作 4xx/5xx 抛 Error(detail)（jsonStrict，schedule-integrity）、
 * 桩无 status 时不抛（测试桩兼容）、wsScheduleUrl 的 window/https/OVERRIDE_PORT。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../i18n/provider", () => ({ currentLocale: () => "zh-CN" }));

import {
  createScheduleTask,
  deleteScheduleTask,
  forkScheduleRun,
  getScheduleTimeline,
  listSchedule,
  listScheduleRuns,
  markScheduleRunsSeen,
  patchScheduleTask,
  putSchedule,
  runScheduleTaskNow,
  wsScheduleUrl,
} from "./schedule";
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

/** 断言唯一一次 GET：精确 URL + locale 单头 + 无 method/body。 */
function expectGet(url: string) {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [got, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(got).toBe(url);
  expect(init.method ?? "GET").toBe("GET");
  expect(init.body).toBeUndefined();
  expect(init.headers).toEqual({ "X-Ginno-Language": "zh-CN" });
}

/** 断言唯一一次写操作：URL、method、双头；body 缺省时必须真的无 body。 */
function expectWrite(method: "POST" | "PUT" | "PATCH" | "DELETE", url: string, body?: unknown) {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [got, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(got).toBe(url);
  expect(init.method).toBe(method);
  if (body === undefined) {
    expect(init.body).toBeUndefined();
  } else {
    expect(typeof init.body).toBe("string");
    expect(JSON.parse(init.body as string)).toEqual(body);
  }
  expect(init.headers).toEqual({
    "X-Ginno-Language": "zh-CN",
    "Content-Type": "application/json",
  });
}

/** 无 H 头的写操作（delete/run）：只有 locale 头。 */
function expectWriteNoContentType(method: "POST" | "DELETE", url: string) {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [got, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(got).toBe(url);
  expect(init.method).toBe(method);
  expect(init.body).toBeUndefined();
  expect(init.headers).toEqual({ "X-Ginno-Language": "zh-CN" });
}

/** query 深比较：精确参数集（多一个少一个都炸）。 */
function expectQuery(url: string, params: Record<string, string>) {
  expect(Object.fromEntries(new URL(url).searchParams)).toEqual(params);
}

const CONFIG = { enabled: true, keep_awake: false, tasks: [] };

describe("BASE 回落（node 无 window）", () => {
  it("BASE 为 127.0.0.1:8787/api", () => {
    expect(BASE).toBe(API);
  });
});

describe("listSchedule / putSchedule（全局开关）", () => {
  it("listSchedule：GET /schedule，配置原样透传", async () => {
    reply(CONFIG);
    await expect(listSchedule()).resolves.toEqual(CONFIG);
    expectGet(`${API}/schedule`);
  });

  // ---- listSchedule 严格化（schedule-integrity 收口复核①）------------------
  // 旧版走 json()：500 的 JSON 错误体 {"detail":...} 会被原样当配置返回，
  // 页面 setCfg(错误体) 后 tasks 是 undefined，渲染层直接崩且 loadedRef=true
  // 后不再重试。以下用**真实 Response 对象**（undici 全局，非裸桩）钉住：
  // 非 2xx 抛错、畸形 200 抛错、shape 不符抛错——三条路都必须交给调用方
  // 的重试/退避，不得伪装成成功。
  it("listSchedule：500 JSON 错误体 → 抛 Error(detail)，不把错误体当配置", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "scheduler store corrupted" }), {
        status: 500,
        headers: { "content-type": "application/json" },
      }),
    );
    await expect(listSchedule()).rejects.toThrow("scheduler store corrupted");
    expectGet(`${API}/schedule`);
  });

  it("listSchedule：200 但 body 非 JSON（畸形/代理错误页）→ r.json() 抛错传播", async () => {
    fetchMock.mockResolvedValue(
      new Response("<html>502 Bad Gateway</html>", {
        status: 200,
        headers: { "content-type": "text/html" },
      }),
    );
    await expect(listSchedule()).rejects.toThrow();
  });

  it("listSchedule：200 JSON 但 shape 不符（无 tasks 数组）→ 抛 shape 错误", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "not a schedule config" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    await expect(listSchedule()).rejects.toThrow("tasks[] missing");
  });

  it("listSchedule：200 JSON tasks 非数组 → 同样抛 shape 错误", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ enabled: true, tasks: "nope" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    await expect(listSchedule()).rejects.toThrow("tasks[] missing");
  });

  it("putSchedule：PUT /schedule，enabled+keep_awake 双字段", async () => {
    const body = { enabled: false, keep_awake: true };
    reply({ ...CONFIG, ...body, ok: true });
    await expect(putSchedule(body)).resolves.toMatchObject({ ok: true, keep_awake: true });
    expectWrite("PUT", `${API}/schedule`, body);
  });

  it("putSchedule：只传 enabled（keep_awake 缺省不发送）", async () => {
    reply({ ok: true });
    await putSchedule({ enabled: true });
    expectWrite("PUT", `${API}/schedule`, { enabled: true });
  });

  it("putSchedule：只传 keep_awake", async () => {
    reply({ ok: true });
    await putSchedule({ keep_awake: false });
    expectWrite("PUT", `${API}/schedule`, { keep_awake: false });
  });

  it("putSchedule：空对象 → body {}（合法形态）", async () => {
    reply({ ok: true });
    await putSchedule({});
    expectWrite("PUT", `${API}/schedule`, {});
  });
});

describe("任务 CRUD", () => {
  const TASK = {
    id: "t1",
    name: "每日晨报",
    target: { type: "prompt" as const, prompt: "hi" },
    schedule: { kind: "interval" as const, minutes: 30 },
    enabled: true,
    created: 1_700_000_000,
    updated: 1_700_000_000,
  };

  it("createScheduleTask：POST /schedule/tasks，task 原样序列化", async () => {
    reply({ ...TASK, ok: true });
    await expect(createScheduleTask(TASK)).resolves.toMatchObject({ id: "t1", ok: true });
    expectWrite("POST", `${API}/schedule/tasks`, TASK);
  });

  it("createScheduleTask：400 → jsonStrict 抛 Error(detail)（schedule-integrity：错误体不再伪装成业务文档）", async () => {
    fetchMock.mockResolvedValue({ status: 400, json: async () => ({ detail: "间隔不得小于 5 分钟" }) });
    await expect(createScheduleTask({ name: "x" })).rejects.toThrow("间隔不得小于 5 分钟");
    expectWrite("POST", `${API}/schedule/tasks`, { name: "x" });
  });

  it("patchScheduleTask：PATCH /schedule/tasks/{id}，id encodeURIComponent", async () => {
    reply({ ok: true });
    await patchScheduleTask("t/1 每日&x", { enabled: false });
    expectWrite("PATCH", `${API}/schedule/tasks/t%2F1%20%E6%AF%8F%E6%97%A5%26x`, {
      enabled: false,
    });
  });

  it("patchScheduleTask：空 patch → body {}", async () => {
    reply({ ok: true });
    await patchScheduleTask("t1", {});
    expectWrite("PATCH", `${API}/schedule/tasks/t1`, {});
  });

  it("patchScheduleTask：500 → 抛错（行内反馈的信号源；不再把 {detail} 当任务返回）", async () => {
    fetchMock.mockResolvedValue({ status: 500, json: async () => ({ detail: "Internal Server Error" }) });
    await expect(patchScheduleTask("t1", { enabled: false })).rejects.toThrow("Internal Server Error");
  });

  it("deleteScheduleTask：DELETE，无 body、locale 单头（不带 Content-Type）", async () => {
    reply({ ok: true, removed: true });
    await expect(deleteScheduleTask("t1")).resolves.toEqual({ ok: true, removed: true });
    expectWriteNoContentType("DELETE", `${API}/schedule/tasks/t1`);
  });

  it("deleteScheduleTask：id 特殊字符编码", async () => {
    reply({ ok: true, removed: false });
    await deleteScheduleTask("a b&c");
    expectWriteNoContentType("DELETE", `${API}/schedule/tasks/a%20b%26c`);
  });

  it("deleteScheduleTask：500 → 抛错（删除失败必须可见，行保留可对账）", async () => {
    fetchMock.mockResolvedValue({ status: 500, json: async () => ({ detail: "boom" }) });
    await expect(deleteScheduleTask("t1")).rejects.toThrow("boom");
  });

  it("runScheduleTaskNow：POST .../run，无 body，返回 run_id", async () => {
    reply({ ok: true, run_id: "r-42" });
    await expect(runScheduleTaskNow("t1")).resolves.toEqual({ ok: true, run_id: "r-42" });
    expectWriteNoContentType("POST", `${API}/schedule/tasks/t1/run`);
  });

  it("putSchedule：500 → 抛错（全局开关写失败的反馈信号）", async () => {
    fetchMock.mockResolvedValue({ status: 502, json: async () => ({ detail: "Bad Gateway" }) });
    await expect(putSchedule({ enabled: true })).rejects.toThrow("Bad Gateway");
  });
});

describe("listScheduleRuns query 分支", () => {
  const PAGE = { runs: [], total: 0, page: 1, page_size: 50 };

  it("不传 opts → URL 不带问号", async () => {
    reply(PAGE);
    await expect(listScheduleRuns()).resolves.toEqual(PAGE);
    expectGet(`${API}/schedule/runs`);
  });

  it("空 opts 对象 → 同样不带问号", async () => {
    reply(PAGE);
    await listScheduleRuns({});
    expectGet(`${API}/schedule/runs`);
  });

  it("八字段全传 → 全部进 query，page 转字符串，顺序固定", async () => {
    reply(PAGE);
    await listScheduleRuns({
      date: "2026-11-15",
      task_id: "t1",
      target_type: "prompt",
      status: "ok",
      trigger: "schedule",
      seen: "unseen",
      sort: "desc",
      page: 2,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toBe(
      `${API}/schedule/runs?date=2026-11-15&task_id=t1&target_type=prompt&status=ok&trigger=schedule&seen=unseen&sort=desc&page=2`,
    );
    expectQuery(url, {
      date: "2026-11-15",
      task_id: "t1",
      target_type: "prompt",
      status: "ok",
      trigger: "schedule",
      seen: "unseen",
      sort: "desc",
      page: "2",
    });
  });

  it("部分参数（date+status+seen）→ 只拼传了的", async () => {
    reply(PAGE);
    await listScheduleRuns({ date: "2026-11-15", status: "error", seen: "seen" });
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toBe(`${API}/schedule/runs?date=2026-11-15&status=error&seen=seen`);
    expectQuery(url, { date: "2026-11-15", status: "error", seen: "seen" });
  });

  it("page=0 被 falsy 守卫跳过（源码 if(opts.page)，0 不发送）", async () => {
    reply(PAGE);
    await listScheduleRuns({ page: 0, sort: "asc" });
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toBe(`${API}/schedule/runs?sort=asc`);
    expectQuery(url, { sort: "asc" });
  });

  it("空串字段被 falsy 守卫跳过（date='' / task_id='' 不发送）", async () => {
    reply(PAGE);
    await listScheduleRuns({ date: "", task_id: "", status: "missed" });
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toBe(`${API}/schedule/runs?status=missed`);
    expectQuery(url, { status: "missed" });
  });

  it("sort=asc 与 desc 均原样下发", async () => {
    reply(PAGE);
    await listScheduleRuns({ sort: "asc" });
    expectQuery(fetchMock.mock.calls[0][0] as string, { sort: "asc" });
  });
});

describe("forkScheduleRun / markScheduleRunsSeen / timeline", () => {
  it("forkScheduleRun：POST .../fork，带 JSON 头但无 body，runId 编码", async () => {
    reply({ ok: true, session_id: "s-new" });
    await expect(forkScheduleRun("r/1 x")).resolves.toEqual({ ok: true, session_id: "s-new" });
    expectWrite("POST", `${API}/schedule/runs/r%2F1%20x/fork`);
  });

  it("forkScheduleRun：失败信封 {ok:false, error} 透传", async () => {
    reply({ ok: false, error: "run not found" });
    await expect(forkScheduleRun("r-x")).resolves.toEqual({ ok: false, error: "run not found" });
  });

  it("markScheduleRunsSeen：单条 {run_id}", async () => {
    const unseen = { total: 2, error: 1, missed: 0 };
    reply({ ok: true, unseen });
    await expect(markScheduleRunsSeen({ run_id: "r-1" })).resolves.toEqual({ ok: true, unseen });
    expectWrite("POST", `${API}/schedule/runs/seen`, { run_id: "r-1" });
  });

  it("markScheduleRunsSeen：一键 {all:true}", async () => {
    reply({ ok: true, unseen: { total: 0, error: 0, missed: 0 } });
    await markScheduleRunsSeen({ all: true });
    expectWrite("POST", `${API}/schedule/runs/seen`, { all: true });
  });

  it("getScheduleTimeline：GET /schedule/timeline?date=...，date 编码", async () => {
    const timeline = { date: "2026-11-15", slots: [] };
    reply(timeline);
    await expect(getScheduleTimeline("2026-11-15")).resolves.toEqual(timeline);
    expectGet(`${API}/schedule/timeline?date=2026-11-15`);
  });

  it("getScheduleTimeline：特殊字符 date 不破坏 query", async () => {
    reply({ date: "x", slots: [] });
    await getScheduleTimeline("2026-11-15&x=1");
    expectGet(`${API}/schedule/timeline?date=2026-11-15%26x%3D1`);
  });
});

describe("wsScheduleUrl 三态", () => {
  it("node 无 window → ws://127.0.0.1:8787/api/ws/schedule", () => {
    expect(wsScheduleUrl()).toBe(`ws://127.0.0.1:${OVERRIDE_PORT ?? 8787}/api/ws/schedule`);
  });

  it("window http → ws://location.host（无端口覆盖时用 host）", () => {
    vi.stubGlobal("window", {
      location: { protocol: "http:", hostname: "desk.local", host: "desk.local:3000" },
    });
    const host = OVERRIDE_PORT ? `desk.local:${OVERRIDE_PORT}` : "desk.local:3000";
    expect(wsScheduleUrl()).toBe(`ws://${host}/api/ws/schedule`);
  });

  it("window https → wss://", () => {
    vi.stubGlobal("window", {
      location: { protocol: "https:", hostname: "app.example.com", host: "app.example.com" },
    });
    const host = OVERRIDE_PORT ? `app.example.com:${OVERRIDE_PORT}` : "app.example.com";
    expect(wsScheduleUrl()).toBe(`wss://${host}/api/ws/schedule`);
  });

  it("OVERRIDE_PORT 显式覆盖宿主端口（resetModules 重读 env）", async () => {
    const prev = process.env.NEXT_PUBLIC_RUNTIME_PORT;
    process.env.NEXT_PUBLIC_RUNTIME_PORT = "9999";
    vi.resetModules();
    try {
      const mod = await import("./schedule");
      vi.stubGlobal("window", {
        location: { protocol: "https:", hostname: "app.example.com", host: "app.example.com" },
      });
      expect(mod.wsScheduleUrl()).toBe("wss://app.example.com:9999/api/ws/schedule");
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
    await expect(listSchedule()).rejects.toThrow("sidecar unreachable");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("响应 body 非法 JSON → r.json() 异常传播", async () => {
    fetchMock.mockResolvedValue({
      json: async () => {
        throw new SyntaxError("Unexpected end of JSON input");
      },
    });
    await expect(listScheduleRuns({ page: 1 })).rejects.toThrow("Unexpected end of JSON input");
  });
});
