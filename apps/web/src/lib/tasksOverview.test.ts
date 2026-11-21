// tasksOverview 纯逻辑层 + hub 单测（node 环境，无 DOM）：
// 分组/计数/归一化/停止调用/乐观移除与失败回滚/轮询节流——组件渲染层的
// 真实性由 packaged UI e2e 覆盖（vitest.config 注释的既定分工）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  emptyTasksOverview,
  stopTasksOverviewItem,
  type TaskHandle,
  type TasksOverview,
} from "./api/tasksOverview";
import {
  __resetTasksOverviewHubForTests,
  handleJumpTarget,
  normalizeTasksOverview,
  optimisticRemoveHandle,
  overviewGroups,
  overviewTotal,
  refreshTasksOverview,
  stopConfirmVariant,
  stopHandleWithRollback,
  subscribeTasksOverview,
  TASKS_OVERVIEW_POLL_MS,
  tasksOverviewSnapshot,
} from "./tasksOverview";

/** 方案 §2.1 的 JSONC 形状样例（it_1 后端未合入，前端按此 mock 联调）。 */
const PLAN_21_SAMPLE: unknown = {
  turns: [
    {
      kind: "turn",
      id: "sid-1",
      session_title: "ginno 开发区",
      started_at: 1732147200,
      stoppable: true,
      detail: "第 3 轮 · 已 12 分钟",
    },
    {
      kind: "turn",
      id: "sid-2",
      session_title: "SOPFlow 调研",
      stoppable: false,
      stop_hint: "等待你回答权限确认，直接停止会丢弃这次交互",
      detail: "等待权限确认",
    },
  ],
  scheduled: [
    {
      kind: "schedule",
      id: "task-9",
      title: "每日 GitHub 趋势分析",
      next_fire_at: "明天 08:00",
      enabled: true,
      stoppable: true,
      stop_hint: "暂停调度",
    },
  ],
  subagents: [
    {
      kind: "plan",
      id: "plan-batch8",
      title: "重构批次 8",
      session_id: "sid-5",
      items: "3 done / 1 running",
      stoppable: true,
    },
  ],
  runs: [
    { kind: "run", id: "run-77", title: "周报流水线", node: "step 2/5", stoppable: true },
  ],
  goals: [
    { kind: "goal", id: "sid-6", title: "完成发版", progress: "turn 7", stoppable: true },
  ],
  delegations: [
    { kind: "delegation", id: "did-1", title: "codex: 修复 X", elapsed_s: 340, stoppable: true },
  ],
};

function lastFetchCall() {
  const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
  return calls[calls.length - 1]!;
}

describe("normalizeTasksOverview（方案 §2.1 形状 → 统一句柄）", () => {
  it("六组各归各位：turn 用 session_title 兜底标题、id 即 sid", () => {
    const d = normalizeTasksOverview(PLAN_21_SAMPLE);
    expect(d.turns).toHaveLength(2);
    expect(d.turns[0]).toMatchObject({
      kind: "turn",
      id: "sid-1",
      title: "ginno 开发区",
      detail: "第 3 轮 · 已 12 分钟",
      session_id: "sid-1",
      stoppable: true,
    });
    expect(d.scheduled[0]).toMatchObject({
      kind: "schedule",
      id: "task-9",
      next_fire_at: "明天 08:00",
      // schedule 无 session_id → 不可跳会话
      session_id: undefined,
    });
    expect(d.subagents[0]).toMatchObject({ kind: "plan", session_id: "sid-5" });
    expect(d.runs).toHaveLength(1);
    expect(d.goals[0]!.session_id).toBe("sid-6");
    expect(d.delegations[0]!.session_id).toBeUndefined();
  });

  it("stoppable 只认显式 false；stop_hint 透传", () => {
    const d = normalizeTasksOverview(PLAN_21_SAMPLE);
    expect(d.turns[1]).toMatchObject({ stoppable: false, stop_hint: expect.stringContaining("权限") });
    expect(d.turns[0]!.stoppable).toBe(true);
  });

  it("旧 sidecar 404 体（{detail:...}）/ 空对象 / 非对象 → 全空不炸", () => {
    for (const raw of [{ detail: "Not Found" }, {}, null, "x", 42]) {
      const d = normalizeTasksOverview(raw);
      expect(overviewTotal(d)).toBe(0);
    }
  });

  it("组内坏元素（缺 id / 非对象）被丢弃，不影响同组其余", () => {
    const d = normalizeTasksOverview({
      turns: [{ id: "ok", title: "t" }, { title: "no-id" }, "junk", null],
    });
    expect(d.turns.map((h) => h.id)).toEqual(["ok"]);
  });

  it("detail 兜底：方案 §2.1 原生字段 items/node/progress/elapsed_s 可直接渲染", () => {
    const d = normalizeTasksOverview({
      subagents: [{ id: "p1", title: "批次", items: "3 done / 1 running" }],
      runs: [{ id: "r1", title: "流水线", node: "step 2/5" }],
      goals: [{ id: "g1", title: "发版", progress: "turn 7" }],
      delegations: [{ id: "d1", title: "codex", elapsed_s: 340 }],
    });
    expect(d.subagents[0]!.detail).toBe("3 done / 1 running");
    expect(d.runs[0]!.detail).toBe("step 2/5");
    expect(d.goals[0]!.detail).toBe("turn 7");
    expect(d.delegations[0]!.detail).toBe("elapsed 340s");
  });
});

describe("overviewGroups / overviewTotal", () => {
  it("分组顺序固定：turn→schedule→plan→run→goal→delegation", () => {
    const d = normalizeTasksOverview(PLAN_21_SAMPLE);
    expect(overviewGroups(d).map((g) => g.kind)).toEqual([
      "turn",
      "schedule",
      "plan",
      "run",
      "goal",
      "delegation",
    ]);
  });

  it("空组被过滤；空态 → 零组零总数", () => {
    const d: TasksOverview = {
      ...emptyTasksOverview(),
      turns: [{ kind: "turn", id: "a", title: "A" }],
    };
    expect(overviewGroups(d).map((g) => g.kind)).toEqual(["turn"]);
    expect(overviewTotal(d)).toBe(1);
    expect(overviewGroups(emptyTasksOverview())).toEqual([]);
    expect(overviewTotal(emptyTasksOverview())).toBe(0);
  });

  it("tab 总徽标 = 六组合计（样例 7 = 2+1+1+1+1+1）", () => {
    expect(overviewTotal(normalizeTasksOverview(PLAN_21_SAMPLE))).toBe(7);
  });
});

describe("停止语义分支与跳转目标", () => {
  it("schedule → pause（可恢复），其余五类 → irreversible", () => {
    expect(stopConfirmVariant("schedule")).toBe("pause");
    for (const k of ["turn", "plan", "run", "goal", "delegation"] as const) {
      expect(stopConfirmVariant(k)).toBe("irreversible");
    }
  });

  it("run → workflow 面板；schedule → /scheduled；带 sid → 会话；delegation → null", () => {
    const d = normalizeTasksOverview(PLAN_21_SAMPLE);
    expect(handleJumpTarget(d.runs[0]!)).toEqual({ type: "workflow" });
    expect(handleJumpTarget(d.scheduled[0]!)).toEqual({ type: "schedule" });
    expect(handleJumpTarget(d.turns[0]!)).toEqual({ type: "session", sid: "sid-1" });
    expect(handleJumpTarget(d.subagents[0]!)).toEqual({ type: "session", sid: "sid-5" });
    expect(handleJumpTarget(d.delegations[0]!)).toBeNull();
  });
});

// ---- hub：轮询 / 订阅 / 乐观移除 / 回滚 --------------------------------------

describe("tasksOverview hub", () => {
  beforeEach(() => {
    __resetTasksOverviewHubForTests();
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    __resetTasksOverviewHubForTests();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("首个订阅 → 立即拉一次；快照 ok/loaded 置位并回放给订阅者", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(PLAN_21_SAMPLE), { status: 200 }),
    );
    const seen: number[] = [];
    const unsub = subscribeTasksOverview((s) => seen.push(overviewTotal(s.data)));
    await vi.waitFor(() => expect(seen).toContain(7));
    expect(tasksOverviewSnapshot()).toMatchObject({ ok: true, loaded: true });
    unsub();
  });

  it("30s 轮询：interval 到点重拉（节流范式，不做 WS）", async () => {
    vi.useFakeTimers();
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify({ turns: [{ id: "t1", title: "T" }] }), { status: 200 }),
    );
    const unsub = subscribeTasksOverview(() => {});
    await vi.advanceTimersByTimeAsync(0);
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThanOrEqual(1);
    const afterFirst = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length;
    await vi.advanceTimersByTimeAsync(TASKS_OVERVIEW_POLL_MS);
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(afterFirst + 1);
    unsub();
  });

  it("最后一个退订 → 停表（不再轮询）", async () => {
    vi.useFakeTimers();
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response("{}", { status: 200 }),
    );
    const unsub = subscribeTasksOverview(() => {});
    await vi.advanceTimersByTimeAsync(0);
    unsub();
    const n = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length;
    await vi.advanceTimersByTimeAsync(TASKS_OVERVIEW_POLL_MS * 3);
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(n);
  });

  it("拉取失败（旧 sidecar 404）→ 保旧数据、ok=false、不抛", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(new Response(JSON.stringify(PLAN_21_SAMPLE), { status: 200 }))
      .mockResolvedValueOnce(new Response("Not Found", { status: 404 }));
    const unsub = subscribeTasksOverview(() => {});
    await refreshTasksOverview(); // 订阅触发的首次拉取（200，样例落库）
    await refreshTasksOverview(); // 第二次 → 404
    const snap = tasksOverviewSnapshot();
    expect(snap.ok).toBe(false);
    expect(overviewTotal(snap.data)).toBe(7); // 旧快照保留
    unsub();
  });

  it("内容不变的 30s 重拉不换 data 引用（防全量重渲染）", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(PLAN_21_SAMPLE), { status: 200 }),
    );
    const unsub = subscribeTasksOverview(() => {});
    await refreshTasksOverview();
    const ref1 = tasksOverviewSnapshot().data;
    await refreshTasksOverview();
    expect(tasksOverviewSnapshot().data).toBe(ref1);
    unsub();
  });
});

describe("停止门面调用与乐观回滚", () => {
  beforeEach(() => {
    __resetTasksOverviewHubForTests();
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    __resetTasksOverviewHubForTests();
    vi.unstubAllGlobals();
  });

  it("stopTasksOverviewItem → POST /api/tasks/overview/stop，body 只带 {kind,id}", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response("{}", { status: 200 }),
    );
    await stopTasksOverviewItem("turn", "sid-1");
    const [url, init] = lastFetchCall() as [string, RequestInit];
    expect(String(url)).toContain("/api/tasks/overview/stop");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ kind: "turn", id: "sid-1" });
  });

  it("乐观移除：立即从快照消失并通知订阅者（badge 同步掉数）", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(PLAN_21_SAMPLE), { status: 200 }),
    );
    let seen = -1;
    const unsub = subscribeTasksOverview((s) => (seen = overviewTotal(s.data)));
    await refreshTasksOverview();
    expect(seen).toBe(7);
    optimisticRemoveHandle("turn", "sid-1");
    expect(overviewTotal(tasksOverviewSnapshot().data)).toBe(6);
    expect(seen).toBe(6);
    unsub();
  });

  it("stopHandleWithRollback：POST 成功 → 行保持移除", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/stop")) return new Response("{}", { status: 200 });
      return new Response(JSON.stringify(PLAN_21_SAMPLE), { status: 200 });
    });
    const unsub = subscribeTasksOverview(() => {});
    await refreshTasksOverview();
    await stopHandleWithRollback({
      kind: "turn",
      id: "sid-1",
      title: "ginno 开发区",
    } as TaskHandle);
    const d = tasksOverviewSnapshot().data;
    expect(d.turns.map((h) => h.id)).toEqual(["sid-2"]);
    unsub();
  });

  it("失败回滚：POST 500 → 重拉恢复真实列表并向上抛错", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/stop")) return new Response("boom", { status: 500 });
      return new Response(JSON.stringify(PLAN_21_SAMPLE), { status: 200 });
    });
    const unsub = subscribeTasksOverview(() => {});
    await refreshTasksOverview();
    await expect(
      stopHandleWithRollback({ kind: "run", id: "run-77", title: "周报流水线" } as TaskHandle),
    ).rejects.toThrow();
    // 回滚 = 重拉真值：run 回来了
    expect(tasksOverviewSnapshot().data.runs.map((h) => h.id)).toEqual(["run-77"]);
    unsub();
  });
});
