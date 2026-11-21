// tasksCounts 纯逻辑层 + hub 单测（node 环境，无 DOM）：
// 归一化（total = 六组之和）/ 轮询启停 / 失败保旧快照 / 保引用 bailout /
// 轮询间隔切换（面板打开 5s / 关闭 30s）。组件渲染层的真实性由 packaged UI
// e2e 覆盖（vitest.config 注释的既定分工）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  emptyTasksCounts,
  getTasksCounts,
  normalizeTasksCounts,
  type TasksCounts,
} from "./api/tasksCounts";
import {
  __resetTasksCountsHubForTests,
  refreshTasksCounts,
  setTasksCountsPollMs,
  subscribeTasksCounts,
  TASKS_COUNTS_OPEN_PANEL_POLL_MS,
  TASKS_COUNTS_POLL_MS,
  tasksCountsSnapshot,
} from "./tasksCounts";

const SAMPLE = {
  ok: true,
  counts: {
    turns: 2,
    scheduled: 1,
    subagents: 1,
    runs: 1,
    goals: 1,
    delegations: 1,
    total: 7,
  },
};

function fetchMock() {
  return globalThis.fetch as ReturnType<typeof vi.fn>;
}

// ---- normalizeTasksCounts -----------------------------------------------------

describe("normalizeTasksCounts", () => {
  it("正常态：六组原样保留", () => {
    const d = normalizeTasksCounts(SAMPLE);
    expect(d).toEqual({
      turns: 2,
      scheduled: 1,
      subagents: 1,
      runs: 1,
      goals: 1,
      delegations: 1,
      total: 7,
    });
  });

  it("total 以六组之和重算——服务端 total 与明细漂移时以明细为准", () => {
    const d = normalizeTasksCounts({ counts: { ...SAMPLE.counts, total: 999 } });
    expect(d.total).toBe(7);
  });

  it("负数/NaN/缺字段归 0；顶层形状（无 counts 包裹）也认", () => {
    expect(
      normalizeTasksCounts({ turns: -3, scheduled: Number.NaN, runs: 2.9 }),
    ).toEqual<TasksCounts>({ ...emptyTasksCounts(), runs: 2, total: 2 });
    expect(normalizeTasksCounts(null)).toEqual(emptyTasksCounts());
    expect(normalizeTasksCounts(undefined)).toEqual(emptyTasksCounts());
    expect(normalizeTasksCounts({})).toEqual(emptyTasksCounts());
  });

  it("phases 三项齐全合法 → 透传（新 sidecar 三档 breakdown）", () => {
    const d = normalizeTasksCounts({
      counts: {
        ...SAMPLE.counts,
        phases: { running: 5, waiting: 1, scheduled: 1 },
      },
    });
    expect(d.phases).toEqual({ running: 5, waiting: 1, scheduled: 1 });
  });

  it("phases 缺失/部分缺失/非法值 → 不产出该字段（UI 退回纯总数文案）", () => {
    // 旧 sidecar：无 phases
    expect(normalizeTasksCounts(SAMPLE).phases).toBeUndefined();
    // 部分缺失
    expect(
      normalizeTasksCounts({
        counts: { ...SAMPLE.counts, phases: { running: 5, waiting: 1 } },
      }).phases,
    ).toBeUndefined();
    // 非法值（负数/非数字）
    expect(
      normalizeTasksCounts({
        counts: { ...SAMPLE.counts, phases: { running: -1, waiting: 1, scheduled: 0 } },
      }).phases,
    ).toBeUndefined();
    expect(
      normalizeTasksCounts({
        counts: { ...SAMPLE.counts, phases: { running: "x", waiting: 1, scheduled: 0 } },
      }).phases,
    ).toBeUndefined();
  });

  it("phases 数值变化触发通知（bailout 按值比较 phases，不吃掉更新）", async () => {
    __resetTasksCountsHubForTests();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () =>
        new Response(JSON.stringify(SAMPLE), { status: 200 }),
      ),
    );
    let notifications = 0;
    const unsub = subscribeTasksCounts(() => {
      notifications += 1;
    });
    await vi.waitFor(() => expect(tasksCountsSnapshot().ok).toBe(true));
    const baseline = notifications;
    await refreshTasksCounts(); // 同载荷（phases 引用每次新建但值相同）→ bailout
    expect(notifications).toBe(baseline);
    // phases 值变化 → 必须通知（waiting 出现 parked turn）
    fetchMock().mockImplementation(async () =>
      new Response(
        JSON.stringify({
          counts: {
            ...SAMPLE.counts,
            phases: { running: 5, waiting: 1, scheduled: 1 },
          },
        }),
        { status: 200 },
      ),
    );
    await refreshTasksCounts();
    expect(notifications).toBe(baseline + 1);
    expect(tasksCountsSnapshot().counts.phases).toEqual({
      running: 5,
      waiting: 1,
      scheduled: 1,
    });
    unsub();
    __resetTasksCountsHubForTests();
    vi.unstubAllGlobals();
  });

  it("getTasksCounts 打到 /api/tasks/counts", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify(SAMPLE), { status: 200 })),
    );
    const d = await getTasksCounts();
    expect(d.total).toBe(7);
    const url = String(fetchMock().mock.calls[0]![0]);
    expect(url).toContain("/api/tasks/counts");
    vi.unstubAllGlobals();
  });
});

// ---- hub：轮询 / 订阅 / 失败兜底 / 间隔切换 ------------------------------------

describe("tasksCounts hub", () => {
  beforeEach(() => {
    __resetTasksCountsHubForTests();
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    __resetTasksCountsHubForTests();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("首个订阅 → 立即拉一次；快照 ok/loaded 置位并回放给订阅者", async () => {
    fetchMock().mockResolvedValue(
      new Response(JSON.stringify(SAMPLE), { status: 200 }),
    );
    const seen: number[] = [];
    const unsub = subscribeTasksCounts((s) => seen.push(s.counts.total));
    await vi.waitFor(() => expect(seen).toContain(7));
    expect(tasksCountsSnapshot()).toMatchObject({ ok: true, loaded: true });
    unsub();
  });

  it("30s 常驻轮询：interval 到点重拉", async () => {
    vi.useFakeTimers();
    fetchMock().mockResolvedValue(
      new Response(JSON.stringify(SAMPLE), { status: 200 }),
    );
    const unsub = subscribeTasksCounts(() => {});
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock().mock.calls.length).toBeGreaterThanOrEqual(1);
    const afterFirst = fetchMock().mock.calls.length;
    await vi.advanceTimersByTimeAsync(TASKS_COUNTS_POLL_MS);
    expect(fetchMock().mock.calls.length).toBe(afterFirst + 1);
    unsub();
  });

  it("最后一个退订 → 停表（不再轮询）", async () => {
    vi.useFakeTimers();
    fetchMock().mockResolvedValue(new Response(JSON.stringify(SAMPLE), { status: 200 }));
    const unsub = subscribeTasksCounts(() => {});
    await vi.advanceTimersByTimeAsync(0);
    unsub();
    const n = fetchMock().mock.calls.length;
    await vi.advanceTimersByTimeAsync(TASKS_COUNTS_POLL_MS * 3);
    expect(fetchMock().mock.calls.length).toBe(n);
  });

  it("拉取失败（旧 sidecar 404）→ 保旧计数、ok=false、不抛", async () => {
    fetchMock().mockResolvedValueOnce(
      new Response(JSON.stringify(SAMPLE), { status: 200 }),
    );
    const seen: TasksCounts[] = [];
    const unsub = subscribeTasksCounts((s) => seen.push(s.counts));
    await vi.waitFor(() => expect(tasksCountsSnapshot().ok).toBe(true));
    fetchMock().mockResolvedValue(new Response("not found", { status: 404 }));
    await refreshTasksCounts();
    expect(tasksCountsSnapshot().ok).toBe(false);
    expect(tasksCountsSnapshot().loaded).toBe(true);
    expect(tasksCountsSnapshot().counts.total).toBe(7); // 旧快照保留
    expect(seen.at(-1)!.total).toBe(7);
    unsub();
  });

  it("数值不变 → 保引用 bailout（订阅者不被无效重渲染打扰）", async () => {
    fetchMock().mockImplementation(async () =>
      new Response(JSON.stringify(SAMPLE), { status: 200 }),
    );
    let notifications = 0;
    const unsub = subscribeTasksCounts(() => {
      notifications += 1;
    });
    await vi.waitFor(() => expect(tasksCountsSnapshot().ok).toBe(true));
    const baseline = notifications;
    await refreshTasksCounts(); // 同一载荷再来一遍
    expect(notifications).toBe(baseline);
    expect(tasksCountsSnapshot().counts).toBe(tasksCountsSnapshot().counts);
    // 数值变了 → 通知
    fetchMock().mockResolvedValue(
      new Response(JSON.stringify({ counts: { ...SAMPLE.counts, turns: 3, total: 8 } }), {
        status: 200,
      }),
    );
    await refreshTasksCounts();
    expect(notifications).toBe(baseline + 1);
    expect(tasksCountsSnapshot().counts.turns).toBe(3);
    unsub();
  });

  it("setTasksCountsPollMs：轮询激活中原地换表（5s 快轮询生效）", async () => {
    vi.useFakeTimers();
    fetchMock().mockResolvedValue(new Response(JSON.stringify(SAMPLE), { status: 200 }));
    const unsub = subscribeTasksCounts(() => {});
    await vi.advanceTimersByTimeAsync(0);
    setTasksCountsPollMs(TASKS_COUNTS_OPEN_PANEL_POLL_MS);
    const afterFirst = fetchMock().mock.calls.length;
    await vi.advanceTimersByTimeAsync(TASKS_COUNTS_OPEN_PANEL_POLL_MS);
    expect(fetchMock().mock.calls.length).toBe(afterFirst + 1);
    // 恢复 30s：5s 网格上不再跳
    setTasksCountsPollMs(TASKS_COUNTS_POLL_MS);
    const n = fetchMock().mock.calls.length;
    await vi.advanceTimersByTimeAsync(TASKS_COUNTS_OPEN_PANEL_POLL_MS);
    expect(fetchMock().mock.calls.length).toBe(n);
    unsub();
  });

  it("并发 refresh 共享同一 promise（不叠加请求）", async () => {
    fetchMock().mockResolvedValue(new Response(JSON.stringify(SAMPLE), { status: 200 }));
    const p1 = refreshTasksCounts();
    const p2 = refreshTasksCounts();
    await Promise.all([p1, p2]);
    expect(fetchMock().mock.calls.length).toBe(1);
  });
});
