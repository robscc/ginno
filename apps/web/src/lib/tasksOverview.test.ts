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
  formatNextFireAt,
  handleJumpTarget,
  handleKey,
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

/** 后端 GET /api/tasks/overview 的真实返回形状（tasks_overview.py 的
 * _collect_* 直出字段；2026-11-21 契约：next_fire_at = epoch 秒，plan 带
 * session_id/session_title，delegation.session_id 可能为 null）。 */
const REAL_BACKEND_SAMPLE: unknown = {
  ok: true,
  counts: { turns: 2, scheduled: 1, subagents: 1, runs: 1, goals: 1, delegations: 1, total: 7 },
  turns: [
    {
      kind: "turn",
      id: "sid-1",
      title: "ginno 开发区",
      session_id: "sid-1",
      session_title: "ginno 开发区",
      started_at: 1732147200,
      stoppable: true,
      detail: "已运行 720s",
    },
    {
      kind: "turn",
      id: "sid-2",
      title: "SOPFlow 调研",
      session_id: "sid-2",
      session_title: "SOPFlow 调研",
      stoppable: false,
      stop_hint: "等待你回答，请到会话中处理",
      detail: "等待你回答，请到会话中处理",
    },
  ],
  scheduled: [
    {
      kind: "schedule",
      id: "task-9",
      title: "每日 GitHub 趋势分析",
      next_fire_at: 1732156800,
      detail: "每天 08:00",
      stoppable: true,
      stop_hint: "暂停调度（可随时重新启用）",
    },
  ],
  subagents: [
    {
      kind: "plan",
      id: "plan-batch8",
      title: "重构批次 8",
      session_id: "sid-5",
      session_title: "重构会话",
      started_at: 1732140000,
      detail: "3 done / 1 running / 4 items",
      stoppable: true,
      stop_hint: "取消清单（未启动条目直接 stopped）",
    },
  ],
  runs: [
    {
      kind: "run",
      id: "run-77",
      title: "周报流水线",
      session_id: "sid-7",
      detail: "step 2/5",
      started_at: 1732141000,
      stoppable: true,
    },
  ],
  goals: [
    {
      kind: "goal",
      id: "sid-6",
      title: "完成发版",
      session_id: "sid-6",
      session_title: "发版会话",
      started_at: 1732130000,
      detail: "turn 7 · 42min · 3 分钟前推进",
      stoppable: true,
      stop_hint: "清除目标（含用量记录，不可恢复）",
    },
  ],
  delegations: [
    {
      kind: "delegation",
      id: "did-1",
      title: "codex: 修复 X",
      session_id: null,
      detail: "外部 CLI · 已运行 340s",
      started_at: 1732146900,
      stoppable: true,
      stop_hint: "终止外部代理进程（结果作废）",
    },
  ],
};

function lastFetchCall() {
  const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
  return calls[calls.length - 1]!;
}

describe("normalizeTasksOverview（后端真实形状 → 统一句柄）", () => {
  it("六组各归各位：turn 的 id 即 sid、plan 带 session_id、delegation null → undefined", () => {
    const d = normalizeTasksOverview(REAL_BACKEND_SAMPLE);
    expect(d.turns).toHaveLength(2);
    expect(d.turns[0]).toMatchObject({
      kind: "turn",
      id: "sid-1",
      title: "ginno 开发区",
      detail: "已运行 720s",
      session_id: "sid-1",
      started_at: 1732147200,
      stoppable: true,
    });
    expect(d.scheduled[0]).toMatchObject({
      kind: "schedule",
      id: "task-9",
      // 契约：epoch 秒数字原样保留
      next_fire_at: 1732156800,
      // schedule 无 session_id → 不可跳会话
      session_id: undefined,
    });
    expect(d.subagents[0]).toMatchObject({ kind: "plan", session_id: "sid-5" });
    expect(d.runs).toHaveLength(1);
    expect(d.goals[0]!.session_id).toBe("sid-6");
    expect(d.delegations[0]!.session_id).toBeUndefined(); // 后端 null → 丢弃
  });

  it("stoppable 只认显式 false；stop_hint 透传", () => {
    const d = normalizeTasksOverview(REAL_BACKEND_SAMPLE);
    expect(d.turns[1]).toMatchObject({ stoppable: false, stop_hint: expect.stringContaining("回答") });
    expect(d.turns[0]!.stoppable).toBe(true);
  });

  it("next_fire_at 契约：非数字（旧字符串/NaN/负数）一律丢弃", () => {
    const d = normalizeTasksOverview({
      scheduled: [
        { id: "a", title: "旧串", next_fire_at: "明天 08:00" },
        { id: "b", title: "NaN", next_fire_at: Number.NaN },
        { id: "c", title: "负数", next_fire_at: -5 },
        { id: "d", title: "合法", next_fire_at: 1732156800 },
      ],
    });
    expect(d.scheduled.map((h) => h.next_fire_at)).toEqual([
      undefined,
      undefined,
      undefined,
      1732156800,
    ]);
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
    const d = normalizeTasksOverview(REAL_BACKEND_SAMPLE);
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
    expect(overviewTotal(normalizeTasksOverview(REAL_BACKEND_SAMPLE))).toBe(7);
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
    const d = normalizeTasksOverview(REAL_BACKEND_SAMPLE);
    expect(handleJumpTarget(d.runs[0]!)).toEqual({ type: "workflow" });
    expect(handleJumpTarget(d.scheduled[0]!)).toEqual({ type: "schedule" });
    expect(handleJumpTarget(d.turns[0]!)).toEqual({ type: "session", sid: "sid-1" });
    expect(handleJumpTarget(d.subagents[0]!)).toEqual({ type: "session", sid: "sid-5" });
    expect(handleJumpTarget(d.delegations[0]!)).toBeNull();
  });
});

describe("handleKey：完整身份（kind+id+session_id）", () => {
  it("plan_id 全局不唯一——同 id 不同 session_id 是两行、两个独立的停止目标", () => {
    const a = { kind: "plan", id: "plan-1", session_id: "sid-a" } as TaskHandle;
    const b = { kind: "plan", id: "plan-1", session_id: "sid-b" } as TaskHandle;
    const c = { kind: "plan", id: "plan-1" } as TaskHandle;
    expect(handleKey(a)).not.toBe(handleKey(b));
    expect(handleKey(a)).toBe("plan:plan-1:sid-a");
    expect(handleKey(c)).toBe("plan:plan-1"); // session_id 缺失退化为 kind:id
  });

  it("turn/goal 的 id 即 sid：缺失时派生补齐（与 normalize 后快照键恒等）", () => {
    // 调用方句柄往往不带 session_id，而快照 normalize 后带 session_id=id
    // ——两侧键必须一致，否则在途/已停过滤永远不命中
    expect(handleKey({ kind: "turn", id: "s1" } as TaskHandle)).toBe("turn:s1:s1");
    expect(handleKey({ kind: "goal", id: "g1" } as TaskHandle)).toBe("goal:g1:g1");
    expect(handleKey({ kind: "turn", id: "s1", session_id: "s1" } as TaskHandle)).toBe(
      "turn:s1:s1",
    );
  });

  it("无 session_id 的 kind（schedule/run）稳定为 kind:id", () => {
    expect(handleKey({ kind: "schedule", id: "task-9" } as TaskHandle)).toBe("schedule:task-9");
    expect(handleKey({ kind: "run", id: "run-77", session_id: "sid-7" } as TaskHandle)).toBe(
      "run:run-77:sid-7",
    );
  });
});

describe("formatNextFireAt：epoch 秒 → 本地语言/时区", () => {
  it("按注入 locale+timeZone 格式化（不依赖测试机时区）", () => {
    // 2026-11-21 08:00 UTC = 上海 16:00（short dateStyle 的年份位数随 locale 走）
    expect(formatNextFireAt(1795248000, "zh-CN", "Asia/Shanghai")).toContain("16:00");
    expect(formatNextFireAt(1795248000, "en-US", "UTC")).toMatch(/11\/21\/\d{2,4}/);
    expect(formatNextFireAt(1795248000, "en-US", "UTC")).toContain("8:00");
  });

  it("非法值（<=0/NaN）返回空串，不抛异常", () => {
    expect(formatNextFireAt(0, "zh-CN")).toBe("");
    expect(formatNextFireAt(-1, "zh-CN")).toBe("");
    expect(formatNextFireAt(Number.NaN, "zh-CN")).toBe("");
  });

  it("不传 timeZone 用系统本地时区（返回含年份/时间的人读串）", () => {
    const s = formatNextFireAt(1795248000, "zh-CN");
    expect(s).toMatch(/2026/);
    expect(s.length).toBeGreaterThan(6);
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
      new Response(JSON.stringify(REAL_BACKEND_SAMPLE), { status: 200 }),
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
      .mockResolvedValueOnce(new Response(JSON.stringify(REAL_BACKEND_SAMPLE), { status: 200 }))
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
      new Response(JSON.stringify(REAL_BACKEND_SAMPLE), { status: 200 }),
    );
    const unsub = subscribeTasksOverview(() => {});
    await refreshTasksOverview();
    const ref1 = tasksOverviewSnapshot().data;
    await refreshTasksOverview();
    expect(tasksOverviewSnapshot().data).toBe(ref1);
    unsub();
  });

  it("等值判定覆盖所有展示/跳转字段：session_id、started_at、next_fire_at 变化必须换引用", async () => {
    const base = {
      scheduled: [
        {
          kind: "schedule",
          id: "t9",
          title: "T",
          detail: "每天 08:00",
          stoppable: true,
          next_fire_at: 1795248000,
        },
      ],
      subagents: [{ kind: "plan", id: "p1", title: "P", session_id: "sid-a", stoppable: true }],
      runs: [{ kind: "run", id: "r1", title: "R", stoppable: true, started_at: 100 }],
    };
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(base), { status: 200 }),
    );
    const unsub = subscribeTasksOverview(() => {});
    await refreshTasksOverview();
    const ref1 = tasksOverviewSnapshot().data;
    // 仅改 next_fire_at（schedule 下次触发推进）
    const t1 = structuredClone(base);
    (t1.scheduled![0] as { next_fire_at: number }).next_fire_at = 1795334400;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(t1), { status: 200 }),
    );
    await refreshTasksOverview();
    expect(tasksOverviewSnapshot().data).not.toBe(ref1);
    const ref2 = tasksOverviewSnapshot().data;
    // 仅改 session_id（plan 跳转目标变了）
    const t2 = structuredClone(base);
    (t2.subagents![0] as { session_id: string }).session_id = "sid-b";
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(t2), { status: 200 }),
    );
    await refreshTasksOverview();
    expect(tasksOverviewSnapshot().data).not.toBe(ref2);
    const ref3 = tasksOverviewSnapshot().data;
    // 仅改 started_at（相对时间展示变了）
    const t3 = structuredClone(base);
    (t3.runs![0] as { started_at: number }).started_at = 200;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(t3), { status: 200 }),
    );
    await refreshTasksOverview();
    expect(tasksOverviewSnapshot().data).not.toBe(ref3);
    unsub();
  });

  it("loading 三态：首拉在途 → loading；成功 → loaded；首拉失败 → never-loaded 失败态", async () => {
    let release!: (r: Response) => void;
    const gate = new Promise<Response>((r) => {
      release = (resp) => r(resp);
    });
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      () => gate,
    );
    const unsub = subscribeTasksOverview(() => {});
    // 在途：loading=true、未加载
    expect(tasksOverviewSnapshot()).toMatchObject({ loading: true, loaded: false, ok: false });
    release(new Response(JSON.stringify(REAL_BACKEND_SAMPLE), { status: 200 }));
    await vi.waitFor(() =>
      expect(tasksOverviewSnapshot()).toMatchObject({ loading: false, loaded: true, ok: true }),
    );
    unsub();
  });

  it("首次拉取就失败：loaded=false、ok=false（面板据此显示失败+重试，而非空态🎉）", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response("Not Found", { status: 404 }),
    );
    const ok = await refreshTasksOverview();
    expect(ok).toBe(false);
    expect(tasksOverviewSnapshot()).toMatchObject({ loading: false, loaded: false, ok: false });
    expect(overviewTotal(tasksOverviewSnapshot().data)).toBe(0);
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

  it("stopTasksOverviewItem → POST /api/tasks/overview/stop；plan 必带 session_id（后端 400 契约）", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response("{}", { status: 200 }),
    );
    await stopTasksOverviewItem({ kind: "plan", id: "plan-batch8", session_id: "sid-5" });
    const [url, init] = lastFetchCall() as [string, RequestInit];
    expect(String(url)).toContain("/api/tasks/overview/stop");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      kind: "plan",
      id: "plan-batch8",
      session_id: "sid-5",
    });
  });

  it("缺少会话身份的 plan 不发无效 POST，也不触发回滚 GET", async () => {
    await expect(stopHandleWithRollback({ kind: "plan", id: "p", title: "p" })).rejects.toThrow("session_id");
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("非 plan kind：session_id 可选透传；缺省不带字段", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response("{}", { status: 200 }),
    );
    await stopTasksOverviewItem({ kind: "schedule", id: "task-9" });
    expect(JSON.parse((lastFetchCall() as [string, RequestInit])[1].body as string)).toEqual({
      kind: "schedule",
      id: "task-9",
    });
    await stopTasksOverviewItem({ kind: "goal", id: "sid-6", session_id: "sid-6" });
    expect(JSON.parse((lastFetchCall() as [string, RequestInit])[1].body as string)).toEqual({
      kind: "goal",
      id: "sid-6",
      session_id: "sid-6",
    });
  });

  it("类型契约：plan 停止漏 session_id 编译期即报错（tsc 门禁）", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response("{}", { status: 200 }),
    );
    // @ts-expect-error plan 缺 session_id 必须编译失败（StopOverviewItemRequest 判别联合）
    await stopTasksOverviewItem({ kind: "plan", id: "plan-batch8" });
  });

  it("乐观移除：立即从快照消失并通知订阅者（badge 同步掉数）", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(REAL_BACKEND_SAMPLE), { status: 200 }),
    );
    let seen = -1;
    const unsub = subscribeTasksOverview((s) => (seen = overviewTotal(s.data)));
    await refreshTasksOverview();
    expect(seen).toBe(7);
    optimisticRemoveHandle({ kind: "turn", id: "sid-1", session_id: "sid-1" });
    expect(overviewTotal(tasksOverviewSnapshot().data)).toBe(6);
    expect(seen).toBe(6);
    unsub();
  });

  it("stopHandleWithRollback：POST 成功 → 行保持移除", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/stop")) return new Response("{}", { status: 200 });
      return new Response(JSON.stringify(REAL_BACKEND_SAMPLE), { status: 200 });
    });
    const unsub = subscribeTasksOverview(() => {});
    await refreshTasksOverview();
    await stopHandleWithRollback({
      kind: "turn",
      id: "sid-1",
      title: "ginno 开发区",
      session_id: "sid-1",
    } as TaskHandle);
    const d = tasksOverviewSnapshot().data;
    expect(d.turns.map((h) => h.id)).toEqual(["sid-2"]);
    // POST body 带了完整身份（turn 也透传 session_id）
    const stopCall = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.find(
      ([u]) => String(u).includes("/stop"),
    ) as unknown[] | undefined;
    expect(JSON.parse((stopCall![1] as RequestInit).body as string)).toEqual({
      kind: "turn",
      id: "sid-1",
      session_id: "sid-1",
    });
    unsub();
  });

  it("同毫秒内停止后发起的新 GET 可以展示同一会话的新 turn", async () => {
    vi.spyOn(Date, "now").mockReturnValue(123456);
    try {
      vi.mocked(fetch).mockImplementation(async (input) => new Response(
        String(input).includes("/stop") ? "{}" : JSON.stringify(REAL_BACKEND_SAMPLE),
      ));
      await refreshTasksOverview();
      await stopHandleWithRollback(tasksOverviewSnapshot().data.turns[0]);
      await refreshTasksOverview();
      expect(tasksOverviewSnapshot().data.turns.map(h => h.id)).toContain("sid-1");
    } finally { vi.restoreAllMocks(); }
  });

  it("失败回滚：POST 500 → 重拉恢复真实列表并向上抛错", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/stop")) return new Response("boom", { status: 500 });
      return new Response(JSON.stringify(REAL_BACKEND_SAMPLE), { status: 200 });
    });
    const unsub = subscribeTasksOverview(() => {});
    await refreshTasksOverview();
    await expect(
      stopHandleWithRollback({
        kind: "plan",
        id: "plan-batch8",
        title: "重构批次 8",
        session_id: "sid-5",
      } as TaskHandle),
    ).rejects.toThrow();
    // 回滚 = 重拉真值：plan 回来了
    expect(tasksOverviewSnapshot().data.subagents.map((h) => h.id)).toEqual(["plan-batch8"]);
    unsub();
  });
});

describe("停止在途的轮询竞态防御（2026-11-21 数据同步审查）", () => {
  // 挂起句柄存模块级（不挂 mock 函数上，保住 tsc 类型）
  let held: { promise: Promise<Response>; release: () => void } | null = null;

  beforeEach(() => {
    held = null;
    __resetTasksOverviewHubForTests();
    vi.restoreAllMocks();
    const calls: unknown[][] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
      calls.push([input, init]);
      const url = String(input);
      const isStop = url.endsWith("/api/tasks/overview/stop");
      if (isStop && held) return held.promise;
      return new Response(JSON.stringify({
        ok: true,
        counts: { turns: 1, total: 1 },
        turns: [{ kind: "turn", id: "s1", title: "跑着", stoppable: true, detail: "12s" }],
      }), { status: 200 });
    }));
  });

  it("POST /stop 在途时，轮询拉回的旧快照不会把已摘句柄闪回", async () => {
    await refreshTasksOverview();
    expect(tasksOverviewSnapshot().data.turns).toHaveLength(1);

    // 挂起 stop 请求（先构造再赋 held，executor 内不触 held）
    let rel!: () => void;
    const stopRes = new Promise<Response>((r) => {
      rel = () => r(new Response("{}", { status: 200 }));
    });
    held = { promise: stopRes, release: () => rel() };
    const p = stopHandleWithRollback(
      ({ kind: "turn", id: "s1", title: "跑着", stoppable: true } as TaskHandle)
    );
    await vi.waitFor(() => {
      expect(tasksOverviewSnapshot().data.turns).toHaveLength(0); // 乐观移除
    });

    // 在途期间轮询再拉同一份旧快照 —— 已摘句柄不得回屏（GET 不挂起）
    await refreshTasksOverview();
    expect(tasksOverviewSnapshot().data.turns).toHaveLength(0);

    held!.release();
    await p;
    expect(tasksOverviewSnapshot().data.turns).toHaveLength(0);
  });
});

// ---- 停止 × 刷新竞态（2026-11-21 契约修订）：过期 GET / 双失败 / 并发停止 ----

describe("停止与刷新的竞态修订", () => {
  /** 可编程 fetch：GET 走 getQueue 逐个出队（返回 Response 的函数），POST 走
   * stopQueue。队列空时兜底 500（绝不静默成功）。 */
  function installQueueMock(opts: {
    getQueue: Array<() => Response | Promise<Response>>;
    stopQueue: Array<() => Response | Promise<Response>>;
  }): void {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: unknown) => {
        const url = String(input);
        const q = url.endsWith("/api/tasks/overview/stop") ? opts.stopQueue : opts.getQueue;
        const next = q.shift();
        if (!next) return new Response("unmocked", { status: 500 });
        return next();
      }),
    );
  }

  const twoTurns = (): Response =>
    new Response(
      JSON.stringify({
        ok: true,
        turns: [
          { kind: "turn", id: "s1", title: "甲", session_id: "s1", stoppable: true },
          { kind: "turn", id: "s2", title: "乙", session_id: "s2", stoppable: true },
        ],
      }),
      { status: 200 },
    );
  const onlyS2 = (): Response =>
    new Response(
      JSON.stringify({
        ok: true,
        turns: [{ kind: "turn", id: "s2", title: "乙", session_id: "s2", stoppable: true }],
      }),
      { status: 200 },
    );

  beforeEach(() => {
    __resetTasksOverviewHubForTests();
    vi.restoreAllMocks();
  });
  afterEach(() => {
    __resetTasksOverviewHubForTests();
    vi.unstubAllGlobals();
  });

  it("POST 成功后，先于成功时刻发起的挂起 GET（过期快照）不得把已停句柄带回", async () => {
    let releaseGet2!: (r: Response) => void;
    installQueueMock({
      getQueue: [
        twoTurns,
        () => new Promise<Response>((r) => (releaseGet2 = r)), // GET#2 挂起（旧快照）
        onlyS2, // GET#3：POST 成功之后才发起的新快照
      ],
      stopQueue: [async () => new Response("{}", { status: 200 })],
    });
    await refreshTasksOverview(); // GET#1
    expect(tasksOverviewSnapshot().data.turns).toHaveLength(2);

    // GET#2 在 POST 之前发起（startedAt 早于 stoppedAt），返回旧快照仍含 s1
    const staleGet = refreshTasksOverview();
    await stopHandleWithRollback({
      kind: "turn",
      id: "s1",
      title: "甲",
      session_id: "s1",
    } as TaskHandle);
    releaseGet2(twoTurns());
    await staleGet;
    // 过期快照里的 s1 必须被过滤——已成功停止的句柄不得闪回
    expect(tasksOverviewSnapshot().data.turns.map((h) => h.id)).toEqual(["s2"]);

    // POST 成功后发起的新 GET（真值只剩 s2）正常落库，且此后 stoppedAt 记录被 GC
    await refreshTasksOverview();
    expect(tasksOverviewSnapshot().data.turns.map((h) => h.id)).toEqual(["s2"]);
  });

  it("双失败（POST 500 + 回滚 GET 也 500）：只还原被停的那一项，不整表回写 prev", async () => {
    installQueueMock({
      getQueue: [
        twoTurns, // GET#1：初始 [s1,s2]
        async () => new Response("down", { status: 500 }), // GET#2：回滚刷新也失败
      ],
      stopQueue: [
        async () => new Response("boom", { status: 500 }), // 停 s1 失败
      ],
    });
    await refreshTasksOverview();
    // 先成功停掉 s2（POST 200，不触发任何刷新——乐观移除即终态）
    installQueueMock({
      getQueue: [],
      stopQueue: [async () => new Response("{}", { status: 200 })],
    });
    await stopHandleWithRollback({
      kind: "turn",
      id: "s2",
      title: "乙",
      session_id: "s2",
    } as TaskHandle);
    expect(tasksOverviewSnapshot().data.turns.map((h) => h.id)).toEqual(["s1"]);

    // 再停 s1：POST 500 → 回滚 GET 也 500（双失败）→ 只还原本项
    installQueueMock({
      getQueue: [async () => new Response("down", { status: 500 })],
      stopQueue: [async () => new Response("boom", { status: 500 })],
    });
    await expect(
      stopHandleWithRollback({
        kind: "turn",
        id: "s1",
        title: "甲",
        session_id: "s1",
      } as TaskHandle),
    ).rejects.toThrow();
    // 关键不变量：s1 被单项还原；s2（已成功停止）绝不因整表回写复活
    expect(tasksOverviewSnapshot().data.turns.map((h) => h.id)).toEqual(["s1"]);
  });

  it("并发停止：s1 成功、s2 失败+回滚成功——s2 的回滚不复活 s1、也不吞掉 s1 的乐观移除", async () => {
    installQueueMock({
      getQueue: [twoTurns], // GET#1
      stopQueue: [async () => new Response("{}", { status: 200 })], // s1 → 200
    });
    await refreshTasksOverview();
    const p1 = stopHandleWithRollback({
      kind: "turn",
      id: "s1",
      title: "甲",
      session_id: "s1",
    } as TaskHandle);

    let releaseStop2!: (r: Response) => void;
    const stop2Gate = new Promise<Response>((r) => (releaseStop2 = r));
    installQueueMock({
      getQueue: [onlyS2], // GET#2：s2 失败后的回滚真值（s1 已被服务端消化，缺席）
      stopQueue: [() => stop2Gate], // s2 → 挂起（保证 p1 先完成）
    });
    const p2 = stopHandleWithRollback({
      kind: "turn",
      id: "s2",
      title: "乙",
      session_id: "s2",
    } as TaskHandle);
    await p1; // s1 停止成功（stoppedAt[s1] 先落）
    releaseStop2(new Response("boom", { status: 500 }));
    await expect(p2).rejects.toThrow();
    // GET#2 真值：s1 已被服务端消化（停止成功）→ 缺席；s2 的 POST 被拒 → 仍在。
    expect(tasksOverviewSnapshot().data.turns.map((h) => h.id)).toEqual(["s2"]);
  });

  it("并发双失败：两个停止都失败且共享一次回滚 GET（也失败）→ 各自单项还原", async () => {
    installQueueMock({
      getQueue: [twoTurns],
      stopQueue: [
        async () => new Response("boom", { status: 500 }),
        async () => new Response("boom", { status: 500 }),
      ],
    });
    await refreshTasksOverview();
    const p1 = stopHandleWithRollback({
      kind: "turn",
      id: "s1",
      title: "甲",
      session_id: "s1",
    } as TaskHandle);
    const p2 = stopHandleWithRollback({
      kind: "turn",
      id: "s2",
      title: "乙",
      session_id: "s2",
    } as TaskHandle);
    await expect(p1).rejects.toThrow();
    await expect(p2).rejects.toThrow();
    // 两个失败项各自还原（共享的回滚 GET 返回 500，双失败兜底生效）
    expect(tasksOverviewSnapshot().data.turns.map((h) => h.id).sort()).toEqual(["s1", "s2"]);
  });

  it("并发停止按完整身份隔离：同 plan_id 不同 session_id 互不误伤", async () => {
    const plans = (): Response =>
      new Response(
        JSON.stringify({
          ok: true,
          subagents: [
            { kind: "plan", id: "plan-1", title: "P", session_id: "sid-a", stoppable: true },
            { kind: "plan", id: "plan-1", title: "P", session_id: "sid-b", stoppable: true },
          ],
        }),
        { status: 200 },
      );
    installQueueMock({ getQueue: [plans], stopQueue: [] });
    await refreshTasksOverview();
    // 只停 sid-a 的 plan-1：POST 带 session_id；GET 真值只剩 sid-b 的那行
    installQueueMock({
      getQueue: [
        () =>
          new Response(
            JSON.stringify({
              ok: true,
              subagents: [
                { kind: "plan", id: "plan-1", title: "P", session_id: "sid-b", stoppable: true },
              ],
            }),
            { status: 200 },
          ),
      ],
      stopQueue: [async () => new Response("{}", { status: 200 })],
    });
    await stopHandleWithRollback({
      kind: "plan",
      id: "plan-1",
      title: "P",
      session_id: "sid-a",
    } as TaskHandle);
    const stopCall = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.find(
      ([u]) => String(u).includes("/stop"),
    ) as unknown[] | undefined;
    expect(JSON.parse((stopCall![1] as RequestInit).body as string)).toEqual({
      kind: "plan",
      id: "plan-1",
      session_id: "sid-a",
    });
    expect(tasksOverviewSnapshot().data.subagents.map((h) => h.session_id)).toEqual(["sid-b"]);
  });
});
