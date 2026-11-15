/**
 * tasks API 客户端单测（后台任务中心）。
 *
 * 重点：TASK_TERMINAL 终态集合语义 + taskLogs 的 tail/offset query 分支
 * （含 0 值——源码用 !== undefined 判断，0 必须发送）。
 * 打桩策略：桩 globalThis.fetch，走真实 core.json()，不 mock ./core。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  TASK_TERMINAL,
  getTask,
  listTasks,
  removeTask,
  stopTask,
  taskLogs,
  type TaskRecord,
} from "./tasks";

const BASE = "http://127.0.0.1:8787/api";

/** 满足 TaskRecord 必填字段的最小 fixture。 */
const TASK: TaskRecord = {
  id: "t1",
  command: "sleep 1",
  title: "测试任务",
  cwd: "/tmp",
  status: "running",
  exit_code: null,
  pid: 1234,
  origin_session: "sess1",
  notify: true,
  mode: "watch",
  timeout_s: null,
  detached: false,
  created_at: 1_700_000_000,
  started_at: 1_700_000_001,
  ended_at: null,
};

function stubFetch(payload: unknown = { ok: true }) {
  const mock = vi.fn().mockImplementation(async () => ({ json: async () => payload }));
  vi.stubGlobal("fetch", mock);
  return mock;
}

function onlyRequest(mock: ReturnType<typeof vi.fn>) {
  expect(mock).toHaveBeenCalledTimes(1);
  const [url, init] = mock.mock.calls[0] as [string, RequestInit | undefined];
  expect(init?.headers).toMatchObject({ "X-Ginno-Language": "en" });
  return { url, init };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("TASK_TERMINAL 终态集合", () => {
  it("恰好包含 5 个终态：finished/stopped/timeout/failed_to_start/interrupted", () => {
    expect(TASK_TERMINAL.size).toBe(5);
    for (const s of ["finished", "stopped", "timeout", "failed_to_start", "interrupted"]) {
      expect(TASK_TERMINAL.has(s)).toBe(true);
    }
  });

  it("running 不是终态（轮询继续的依据）", () => {
    expect(TASK_TERMINAL.has("running")).toBe(false);
  });

  it("未知/拼写错误的 status 不算终态（fail-safe）", () => {
    expect(TASK_TERMINAL.has("")).toBe(false);
    expect(TASK_TERMINAL.has("Finished")).toBe(false); // 大小写敏感
    expect(TASK_TERMINAL.has("cancelled")).toBe(false);
  });

  it("是只读 Set 实例，可安全用于 has() 判定", () => {
    expect(TASK_TERMINAL).toBeInstanceOf(Set);
  });
});

describe("任务查询", () => {
  it("listTasks：GET /tasks，空数组透传", async () => {
    const mock = stubFetch([]);
    await expect(listTasks()).resolves.toEqual([]);
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/tasks`);
    expect(init?.method ?? "GET").toBe("GET");
  });

  it("getTask：GET /tasks/{id}，完整记录透传", async () => {
    const mock = stubFetch(TASK);
    await expect(getTask("t1")).resolves.toEqual(TASK);
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/tasks/t1`);
  });
});

describe("taskLogs query 参数分支", () => {
  it("不传 opts：query 为空但 URL 仍带 '?'（源码固定拼接）", async () => {
    const payload = { status: "running", lines: [], total: 0 };
    const mock = stubFetch(payload);
    await expect(taskLogs("t1")).resolves.toEqual(payload);
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/tasks/t1/logs?`);
  });

  it("空 opts 对象等价于不传", async () => {
    const mock = stubFetch({ status: "finished", lines: [], total: 0 });
    await taskLogs("t1", {});
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/tasks/t1/logs?`);
  });

  it("只传 tail → ?tail=N", async () => {
    const mock = stubFetch({ status: "running", lines: ["a"], total: 1 });
    await taskLogs("t1", { tail: 200 });
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/tasks/t1/logs?tail=200`);
  });

  it("只传 offset → ?offset=N", async () => {
    const mock = stubFetch({ status: "running", lines: [], total: 0 });
    await taskLogs("t1", { offset: 1024 });
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/tasks/t1/logs?offset=1024`);
  });

  it("tail + offset 同传：tail 在前 offset 在后", async () => {
    const mock = stubFetch({ status: "running", lines: [], total: 0 });
    await taskLogs("t1", { tail: 50, offset: 4096 });
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/tasks/t1/logs?tail=50&offset=4096`);
  });

  it("tail=0 必须发送（!== undefined 判断，0 不是缺省）", async () => {
    const mock = stubFetch({ status: "running", lines: [], total: 0 });
    await taskLogs("t1", { tail: 0 });
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/tasks/t1/logs?tail=0`);
  });

  it("offset=0 必须发送（从头读日志的合法语义）", async () => {
    const mock = stubFetch({ status: "running", lines: [], total: 0 });
    await taskLogs("t1", { offset: 0 });
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/tasks/t1/logs?offset=0`);
  });

  it("tail=0 + offset=0 双零组合", async () => {
    const mock = stubFetch({ status: "running", lines: [], total: 0 });
    await taskLogs("t1", { tail: 0, offset: 0 });
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/tasks/t1/logs?tail=0&offset=0`);
  });
});

describe("任务操作", () => {
  it("stopTask：POST /tasks/{id}/stop，无 body，带 JSON 头", async () => {
    const stopped = { ...TASK, status: "stopped" as const, ended_at: 1_700_000_002 };
    const payload = { ok: true, task: stopped };
    const mock = stubFetch(payload);
    await expect(stopTask("t1")).resolves.toEqual(payload);
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/tasks/t1/stop`);
    expect(init?.method).toBe("POST");
    expect(init?.body).toBeUndefined();
    expect(init?.headers).toMatchObject({ "Content-Type": "application/json" });
  });

  it("removeTask：DELETE /tasks/{id}，带 JSON 头", async () => {
    const mock = stubFetch({ ok: true });
    await expect(removeTask("t1")).resolves.toEqual({ ok: true });
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/tasks/t1`);
    expect(init?.method).toBe("DELETE");
    expect(init?.headers).toMatchObject({ "Content-Type": "application/json" });
  });
});

describe("错误传播", () => {
  it("fetch 网络异常 → Promise reject", async () => {
    const mock = vi.fn().mockImplementation(async () => {
      throw new Error("sidecar unreachable");
    });
    vi.stubGlobal("fetch", mock);
    await expect(listTasks()).rejects.toThrow("sidecar unreachable");
    expect(mock).toHaveBeenCalledTimes(1);
  });

  it("响应 body 非法 JSON → r.json() 异常传播", async () => {
    const mock = vi.fn().mockImplementation(async () => ({
      json: async () => {
        throw new SyntaxError("Unexpected end of JSON input");
      },
    }));
    vi.stubGlobal("fetch", mock);
    await expect(taskLogs("t1", { tail: 10 })).rejects.toThrow("Unexpected end of JSON input");
  });
});
