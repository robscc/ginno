/**
 * workflows API 客户端单测。
 *
 * 打桩策略：桩 globalThis.fetch，走真实 core.json() 请求构造
 * （locale 头 / H 头 / body 序列化全部被断言），不 mock ./core。
 * node 环境无 window → BASE 回落 http://127.0.0.1:8787/api。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cancelWorkflowRun,
  cleanupWorkflowRuns,
  createWorkflow,
  decideWorkflowRun,
  deleteWorkflow,
  deleteWorkflowRun,
  diffWorkflowVersions,
  doctorWorkflow,
  dryRunWorkflow,
  getSummarizeTrace,
  getSynthesisCase,
  getSynthesisStats,
  getWorkflow,
  getWorkflowRun,
  getWorkflowRunEvents,
  listSynthesisCases,
  listWorkflowRuns,
  listWorkflowVersions,
  listWorkflows,
  pauseWorkflowRun,
  presentWorkflowRun,
  replaySynthesis,
  rerunWorkflowRunFrom,
  resumeWorkflowRun,
  retryWorkflowRun,
  retryWorkflowRunFromCheckpoint,
  rollbackWorkflow,
  summarizeSessionToDsl,
  triggerWorkflowRun,
  updateWorkflow,
} from "./workflows";

const BASE = "http://127.0.0.1:8787/api";

/** 桩 fetch：返回 { json: async () => payload }，记录全部调用。 */
function stubFetch(payload: unknown = { ok: true }) {
  const mock = vi.fn().mockImplementation(async () => ({ json: async () => payload }));
  vi.stubGlobal("fetch", mock);
  return mock;
}

/** 取唯一一次请求，并断言 core.json 注入的 locale 头（真实 currentLocale()=en）。 */
function onlyRequest(mock: ReturnType<typeof vi.fn>) {
  expect(mock).toHaveBeenCalledTimes(1);
  const [url, init] = mock.mock.calls[0] as [string, RequestInit | undefined];
  expect(init?.headers).toMatchObject({ "X-Ginno-Language": "en" });
  return { url, init };
}

/** 解析 body 并断言 JSON Content-Type 头（H）。 */
function bodyOf(init: RequestInit | undefined) {
  expect(init?.headers).toMatchObject({ "Content-Type": "application/json" });
  return JSON.parse(init?.body as string);
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("workflow 定义 CRUD", () => {
  it("listWorkflows：GET /workflows，空数组原样返回", async () => {
    const mock = stubFetch([]);
    await expect(listWorkflows()).resolves.toEqual([]);
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflows`);
    expect(init?.method ?? "GET").toBe("GET");
  });

  it("createWorkflow：POST + body 原样序列化（含 synthesis_id/workflow_id 导入参数）", async () => {
    const payload = { ok: true, workflow: { id: "wf1" } };
    const mock = stubFetch(payload);
    const data = { name: "demo", synthesis_id: "syn1", workflow_id: "wf0" };
    await expect(createWorkflow(data)).resolves.toEqual(payload);
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflows`);
    expect(init?.method).toBe("POST");
    expect(bodyOf(init)).toEqual(data);
  });

  it("getWorkflow：GET /workflows/{id}", async () => {
    const payload = { ok: true, workflow: { id: "wf1" } };
    const mock = stubFetch(payload);
    await expect(getWorkflow("wf1")).resolves.toEqual(payload);
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflows/wf1`);
    expect(init?.method ?? "GET").toBe("GET");
  });

  it("updateWorkflow：PUT /workflows/{id} + body（含 commit）", async () => {
    const mock = stubFetch({ ok: true });
    const data = { name: "renamed", commit: "调整节点" };
    await expect(updateWorkflow("wf1", data)).resolves.toEqual({ ok: true });
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflows/wf1`);
    expect(init?.method).toBe("PUT");
    expect(bodyOf(init)).toEqual(data);
  });

  it("updateWorkflow：非 2xx 抛错并保留 body.detail + ginnoStatus（Q6，走 jsonStrict）", async () => {
    // 服务端 400 的典型形态：{"detail": "<DSL 校验原因>"}。jsonStrict 必须抛错
    // 而不是把错误体当业务文档返回——否则调用方只见 ok:false，丢「为什么被拒」。
    const mock = vi.fn().mockImplementation(async () => ({
      ok: false,
      status: 400,
      json: async () => ({ detail: "node s2: 'prompt' must be a non-empty string" }),
    }));
    vi.stubGlobal("fetch", mock);
    const err = await updateWorkflow("wf1", { dsl: {} }).then(
      () => { throw new Error("expected reject"); },
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(Error);
    expect((err as Error).message).toBe("node s2: 'prompt' must be a non-empty string");
    expect((err as Error & { ginnoStatus?: number }).ginnoStatus).toBe(400);
  });

  it("updateWorkflow：409 并发冲突同样抛错带状态（将来 store 加 version CAS 时的分型依据）", async () => {
    const mock = vi.fn().mockImplementation(async () => ({
      ok: false,
      status: 409,
      json: async () => ({ detail: "version conflict: 3 != 2" }),
    }));
    vi.stubGlobal("fetch", mock);
    const err = await updateWorkflow("wf1", { dsl: {} }).then(
      () => { throw new Error("expected reject"); },
      (e: unknown) => e,
    );
    expect((err as Error & { ginnoStatus?: number }).ginnoStatus).toBe(409);
    expect((err as Error).message).toBe("version conflict: 3 != 2");
  });

  it("deleteWorkflow：DELETE /workflows/{id}", async () => {
    const mock = stubFetch({ ok: true });
    await expect(deleteWorkflow("wf1")).resolves.toEqual({ ok: true });
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflows/wf1`);
    expect(init?.method).toBe("DELETE");
  });
});

describe("版本管理", () => {
  it("listWorkflowVersions：GET /workflows/{id}/versions", async () => {
    const payload = { ok: true, versions: [{ version: 2, current: true, ts: 1 }] };
    const mock = stubFetch(payload);
    await expect(listWorkflowVersions("wf1")).resolves.toEqual(payload);
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflows/wf1/versions`);
  });

  it("diffWorkflowVersions：query 携带 a/b 版本号", async () => {
    const payload = { ok: true, a: 1, b: 2, diff: "--- a\n+++ b" };
    const mock = stubFetch(payload);
    await expect(diffWorkflowVersions("wf1", 1, 2)).resolves.toEqual(payload);
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflows/wf1/versions/diff?a=1&b=2`);
  });

  it("rollbackWorkflow：不传 commit 时默认空串仍进 body", async () => {
    const mock = stubFetch({ ok: true, workflow: { id: "wf1" } });
    await rollbackWorkflow("wf1", 3);
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflows/wf1/rollback`);
    expect(init?.method).toBe("POST");
    expect(bodyOf(init)).toEqual({ to: 3, commit: "" });
  });

  it("rollbackWorkflow：显式 commit 原样透传", async () => {
    const mock = stubFetch({ ok: true });
    await rollbackWorkflow("wf1", 2, "回滚原因");
    const { init } = onlyRequest(mock);
    expect(bodyOf(init)).toEqual({ to: 2, commit: "回滚原因" });
  });
});

describe("run 触发与生命周期", () => {
  it("triggerWorkflowRun：全参时 session_id 同时写入 present_in_session_id", async () => {
    const payload = { ok: true, run: { run_id: "r1" } };
    const mock = stubFetch(payload);
    await expect(
      triggerWorkflowRun("wf1", { k: "v" }, "sess1"),
    ).resolves.toEqual(payload);
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflow_runs`);
    expect(init?.method).toBe("POST");
    expect(bodyOf(init)).toEqual({
      workflow_id: "wf1",
      context_override: { k: "v" },
      session_id: "sess1",
      present_in_session_id: "sess1",
    });
  });

  it("triggerWorkflowRun：仅必填参数时 undefined 键被 JSON.stringify 丢弃", async () => {
    const mock = stubFetch({ ok: true });
    await triggerWorkflowRun("wf1");
    const { init } = onlyRequest(mock);
    expect(bodyOf(init)).toEqual({ workflow_id: "wf1" });
  });

  it("presentWorkflowRun：POST /workflow_runs/{id}/present + {session_id}", async () => {
    const mock = stubFetch({ ok: true });
    await presentWorkflowRun("r1", "sess2");
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflow_runs/r1/present`);
    expect(init?.method).toBe("POST");
    expect(bodyOf(init)).toEqual({ session_id: "sess2" });
  });

  it("cancelWorkflowRun：POST cancel，body 为空对象", async () => {
    const mock = stubFetch({ ok: true, status: "cancelled" });
    await expect(cancelWorkflowRun("r1")).resolves.toEqual({ ok: true, status: "cancelled" });
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflow_runs/r1/cancel`);
    expect(init?.method).toBe("POST");
    expect(bodyOf(init)).toEqual({});
  });

  it("pauseWorkflowRun：POST pause，body 为空对象", async () => {
    const mock = stubFetch({ ok: true, status: "paused" });
    await pauseWorkflowRun("r1");
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflow_runs/r1/pause`);
    expect(init?.method).toBe("POST");
    expect(bodyOf(init)).toEqual({});
  });

  it("resumeWorkflowRun：不传 value 默认空对象", async () => {
    const mock = stubFetch({ ok: true, status: "running" });
    await resumeWorkflowRun("r1");
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflow_runs/r1/resume`);
    expect(bodyOf(init)).toEqual({});
  });

  it("resumeWorkflowRun：value 原样作为 body", async () => {
    const mock = stubFetch({ ok: true });
    await resumeWorkflowRun("r1", { answer: 42 });
    const { init } = onlyRequest(mock);
    expect(bodyOf(init)).toEqual({ answer: 42 });
  });

  it("decideWorkflowRun：带 context_patch", async () => {
    const mock = stubFetch({ ok: true, status: "running" });
    await decideWorkflowRun("r1", "approve", { note: "ok" });
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflow_runs/r1/decide`);
    expect(bodyOf(init)).toEqual({ decision: "approve", context_patch: { note: "ok" } });
  });

  it("decideWorkflowRun：不传 patch 时 body 只有 decision", async () => {
    const mock = stubFetch({ ok: true });
    await decideWorkflowRun("r1", "reject");
    const { init } = onlyRequest(mock);
    expect(bodyOf(init)).toEqual({ decision: "reject" });
  });

  it("retryWorkflowRun：POST retry，body 空对象，响应透传", async () => {
    const payload = { ok: true, run: { run_id: "r2" }, source_run_id: "r1" };
    const mock = stubFetch(payload);
    await expect(retryWorkflowRun("r1")).resolves.toEqual(payload);
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflow_runs/r1/retry`);
    expect(init?.method).toBe("POST");
    expect(bodyOf(init)).toEqual({});
  });

  it("retryWorkflowRunFromCheckpoint：POST retry_from_checkpoint", async () => {
    const mock = stubFetch({ ok: true, source_run_id: "r1" });
    await retryWorkflowRunFromCheckpoint("r1");
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflow_runs/r1/retry_from_checkpoint`);
    expect(init?.method).toBe("POST");
    expect(bodyOf(init)).toEqual({});
  });

  it("rerunWorkflowRunFrom：POST rerun_from + {node_id}", async () => {
    const mock = stubFetch({ ok: true });
    await rerunWorkflowRunFrom("r1", "n3");
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflow_runs/r1/rerun_from`);
    expect(bodyOf(init)).toEqual({ node_id: "n3" });
  });

  it("deleteWorkflowRun：DELETE /workflow_runs/{id}", async () => {
    const mock = stubFetch({ ok: true });
    await expect(deleteWorkflowRun("r1")).resolves.toEqual({ ok: true });
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflow_runs/r1`);
    expect(init?.method).toBe("DELETE");
  });
});

describe("run 查询与清理", () => {
  it("listWorkflowRuns：无参数不带 query string", async () => {
    const mock = stubFetch([]);
    await expect(listWorkflowRuns()).resolves.toEqual([]);
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflow_runs`);
  });

  it("listWorkflowRuns：三个过滤参数全传", async () => {
    const mock = stubFetch([]);
    await listWorkflowRuns({ workflow_id: "wf1", status: "running", supervisor_pending: true });
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflow_runs?workflow_id=wf1&status=running&supervisor_pending=true`);
  });

  it("listWorkflowRuns：supervisor_pending=false 是 falsy，不进 query", async () => {
    const mock = stubFetch([]);
    await listWorkflowRuns({ workflow_id: "wf1", supervisor_pending: false });
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflow_runs?workflow_id=wf1`);
  });

  it("getWorkflowRun：GET 路径；run=null 原样透传", async () => {
    const mock = stubFetch({ ok: true, run: null });
    await expect(getWorkflowRun("missing")).resolves.toEqual({ ok: true, run: null });
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflow_runs/missing`);
  });

  it("getWorkflowRunEvents：无过滤参数不带 query", async () => {
    const mock = stubFetch({ ok: true, events: [] });
    await expect(getWorkflowRunEvents("r1")).resolves.toEqual({ ok: true, events: [] });
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflow_runs/r1/events`);
  });

  it("getWorkflowRunEvents：node_id + kind 双过滤", async () => {
    const mock = stubFetch({ ok: true, events: [] });
    await getWorkflowRunEvents("r1", { node_id: "n2", kind: "node_failed" });
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflow_runs/r1/events?node_id=n2&kind=node_failed`);
  });

  it("cleanupWorkflowRuns：不传 statuses → body 空对象", async () => {
    const mock = stubFetch({ ok: true, deleted: 0 });
    await expect(cleanupWorkflowRuns()).resolves.toEqual({ ok: true, deleted: 0 });
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflow_runs/cleanup`);
    expect(init?.method).toBe("POST");
    expect(bodyOf(init)).toEqual({});
  });

  it("cleanupWorkflowRuns：空数组也是 truthy → {statuses: []}", async () => {
    const mock = stubFetch({ ok: true, deleted: 0 });
    await cleanupWorkflowRuns([]);
    const { init } = onlyRequest(mock);
    expect(bodyOf(init)).toEqual({ statuses: [] });
  });

  it("cleanupWorkflowRuns：指定 statuses 列表", async () => {
    const mock = stubFetch({ ok: true, deleted: 2 });
    await cleanupWorkflowRuns(["finished", "failed"]);
    const { init } = onlyRequest(mock);
    expect(bodyOf(init)).toEqual({ statuses: ["finished", "failed"] });
  });
});

describe("doctor 与 dry-run", () => {
  it("doctorWorkflow：GET /workflows/{id}/doctor，errors/warnings 透传", async () => {
    const payload = {
      ok: false,
      errors: [{ rule: "dangling_ref", node_id: "n2", message: "未知引用" }],
      warnings: [],
    };
    const mock = stubFetch(payload);
    await expect(doctorWorkflow("wf1")).resolves.toEqual(payload);
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflows/wf1/doctor`);
  });

  it("dryRunWorkflow：POST /workflows/dry-run + {dsl} 包裹", async () => {
    const dsl = { nodes: [], edges: [] };
    const payload = { ok: true, errors: [], doctor_errors: [], warnings: [], unreachable: [] };
    const mock = stubFetch(payload);
    await expect(dryRunWorkflow(dsl)).resolves.toEqual(payload);
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflows/dry-run`);
    expect(init?.method).toBe("POST");
    expect(bodyOf(init)).toEqual({ dsl });
  });
});

describe("synthesis 案例与统计", () => {
  it("listSynthesisCases：默认 limit=100", async () => {
    const mock = stubFetch({ ok: true, cases: [] });
    await expect(listSynthesisCases()).resolves.toEqual({ ok: true, cases: [] });
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/synthesis/cases?limit=100`);
  });

  it("listSynthesisCases：自定义 limit", async () => {
    const mock = stubFetch({ ok: true, cases: [] });
    await listSynthesisCases(5);
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/synthesis/cases?limit=5`);
  });

  it("getSynthesisCase：GET /synthesis/cases/{id}", async () => {
    const payload = { ok: true, case: { synthesis_id: "syn1", output: { status: "ok" } } };
    const mock = stubFetch(payload);
    await expect(getSynthesisCase("syn1")).resolves.toEqual(payload);
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/synthesis/cases/syn1`);
  });

  it("getSynthesisStats：默认 days=30", async () => {
    const payload = {
      ok: true,
      total: 0,
      l1_generated: 0,
      l2_adopted: 0,
      l3_first_run_done: 0,
      avg_edit_distance: null,
      top_fail_labels: [],
    };
    const mock = stubFetch(payload);
    await expect(getSynthesisStats()).resolves.toEqual(payload);
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/synthesis/stats?days=30`);
  });

  it("getSynthesisStats：自定义 days", async () => {
    const mock = stubFetch({ ok: true });
    await getSynthesisStats(7);
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/synthesis/stats?days=7`);
  });

  it("replaySynthesis：不传 provider → body 空对象", async () => {
    const mock = stubFetch({ ok: true, dsl: {} });
    await replaySynthesis("syn1");
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/synthesis/replay/syn1`);
    expect(init?.method).toBe("POST");
    expect(bodyOf(init)).toEqual({});
  });

  it("replaySynthesis：传 provider → {provider}", async () => {
    const mock = stubFetch({ ok: false, errors: ["parse"], fail_stage: "parse" });
    await expect(replaySynthesis("syn1", "glm")).resolves.toEqual({
      ok: false,
      errors: ["parse"],
      fail_stage: "parse",
    });
    const { init } = onlyRequest(mock);
    expect(bodyOf(init)).toEqual({ provider: "glm" });
  });

  it("summarizeSessionToDsl：最小参数只发 session_id（undefined provider 被丢弃）", async () => {
    const payload = { ok: true, synthesis_id: "syn2", status: "started" };
    const mock = stubFetch(payload);
    await expect(summarizeSessionToDsl("sess1")).resolves.toEqual(payload);
    const { url, init } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflows/summarize-from-session`);
    expect(init?.method).toBe("POST");
    expect(bodyOf(init)).toEqual({ session_id: "sess1" });
  });

  it("summarizeSessionToDsl：last_n 与 range 同传（优先级由服务端裁决）", async () => {
    const mock = stubFetch({ ok: true });
    await summarizeSessionToDsl("sess1", "glm", 10, { start: 2, end: 8 });
    const { init } = onlyRequest(mock);
    expect(bodyOf(init)).toEqual({
      session_id: "sess1",
      provider: "glm",
      last_n: 10,
      range: { start: 2, end: 8 },
    });
  });

  it("summarizeSessionToDsl：last_n=0 是 falsy，不进 body", async () => {
    const mock = stubFetch({ ok: true });
    await summarizeSessionToDsl("sess1", undefined, 0);
    const { init } = onlyRequest(mock);
    expect(bodyOf(init)).toEqual({ session_id: "sess1" });
  });

  it("getSummarizeTrace：session_id 走 encodeURIComponent", async () => {
    const payload = { ok: true, rows: [], count: 0 };
    const mock = stubFetch(payload);
    await expect(getSummarizeTrace("sess/1 2&x")).resolves.toEqual(payload);
    const { url } = onlyRequest(mock);
    expect(url).toBe(`${BASE}/workflows/summarize-trace/sess%2F1%202%26x`);
  });
});

describe("错误路径（json 不检查 HTTP 状态，只解析 body）", () => {
  it("HTTP 错误响应体 {ok:false, detail} 原样返回不抛错", async () => {
    const payload = { ok: false, detail: "validation failed" };
    const mock = stubFetch(payload);
    await expect(summarizeSessionToDsl("sess1")).resolves.toEqual(payload);
    onlyRequest(mock);
  });

  it("fetch 网络异常 → Promise reject 传播", async () => {
    const mock = vi.fn().mockImplementation(async () => {
      throw new Error("network down");
    });
    vi.stubGlobal("fetch", mock);
    await expect(listWorkflows()).rejects.toThrow("network down");
    expect(mock).toHaveBeenCalledTimes(1);
  });

  it("响应 body 非法 JSON → r.json() 异常传播", async () => {
    const mock = vi.fn().mockImplementation(async () => ({
      json: async () => {
        throw new SyntaxError("Unexpected token");
      },
    }));
    vi.stubGlobal("fetch", mock);
    await expect(getWorkflow("wf1")).rejects.toThrow("Unexpected token");
  });
});
