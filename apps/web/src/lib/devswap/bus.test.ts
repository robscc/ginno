// devswap/bus 单测：热切换进度总线（模块级单例）。
//
// 打桩口径：
// - ../api/core 的 BASE 桩成定值——既固定 fetch URL 断言，也避免拉进
//   api/core → i18n/provider 的 React 模块图（node 环境约定）；
// - ./types 的 normalizeDevSwapRun 用真实实现（零依赖纯函数，测真实规范化）；
// - fetch 用 vi.stubGlobal 造桩，afterEach 统一 unstub；
// - latest/stageStart/lastKey/deliveredResults/pendingConfirm 全是模块私有态
//   且无重置导出 → 每个用例 vi.resetModules() + 动态 import 拿全新实例。

import { afterEach, describe, expect, it, vi } from "vitest";
import type { DevSwapRun } from "@/lib/devswap/types";

const { BASE } = vi.hoisted(() => ({ BASE: "http://127.0.0.1:8787/api" }));
vi.mock("../api/core", () => ({ BASE }));

async function freshBus() {
  vi.resetModules();
  return import("@/lib/devswap/bus");
}

function mkRun(over: Partial<DevSwapRun> = {}): DevSwapRun {
  return { run_id: "r1", stage: "unit", state: "running", ts: 1000, ...over };
}

/** 桩 fetch：返回 {ok, json} 最小 Response 形状。 */
function stubFetchJson(body: unknown, ok = true) {
  const json = vi.fn(async () => body);
  const fetchMock = vi.fn(async () => ({ ok, json }));
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, json };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("pushDevSwapRun / getDevSwapRun / subscribeDevSwap", () => {
  it("初始无 run", async () => {
    const bus = await freshBus();
    expect(bus.getDevSwapRun()).toBeNull();
  });

  it("推送后订阅者收到补好 stageStartedAt 的 run，getDevSwapRun 同步", async () => {
    const bus = await freshBus();
    const seen: (DevSwapRun | null)[] = [];
    bus.subscribeDevSwap((r) => seen.push(r));
    bus.pushDevSwapRun(mkRun({ pct: 10 }));
    expect(seen).toHaveLength(1);
    expect(seen[0]).toEqual({ ...mkRun({ pct: 10 }), stageStartedAt: 1000 });
    expect(bus.getDevSwapRun()).toEqual(seen[0]);
  });

  it("同一阶段的后续事件复用首个 ts 作计时锚点（pct 递增不重置 elapsed）", async () => {
    const bus = await freshBus();
    bus.pushDevSwapRun(mkRun({ stage: "build_runtime", pct: 10, ts: 1000 }));
    bus.pushDevSwapRun(mkRun({ stage: "build_runtime", pct: 55, ts: 1040 }));
    expect(bus.getDevSwapRun()?.stageStartedAt).toBe(1000);
    expect(bus.getDevSwapRun()?.pct).toBe(55);
  });

  it("run_id+stage+state+pct 全同的事件去重；pct 缺省与 null 等价", async () => {
    const bus = await freshBus();
    const fn = vi.fn();
    bus.subscribeDevSwap(fn);
    bus.pushDevSwapRun(mkRun());
    bus.pushDevSwapRun(mkRun());
    bus.pushDevSwapRun(mkRun({ pct: null }));
    bus.pushDevSwapRun(mkRun({ pct: undefined }));
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("进入新阶段 → 用新事件的 ts 建新锚点", async () => {
    const bus = await freshBus();
    bus.pushDevSwapRun(mkRun({ stage: "unit", ts: 1000 }));
    bus.pushDevSwapRun(mkRun({ stage: "build_runtime", ts: 2000 }));
    expect(bus.getDevSwapRun()?.stageStartedAt).toBe(2000);
  });

  it("事件缺 ts → 用当前墙钟（秒）补锚点", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_700_000_000_000);
    const bus = await freshBus();
    bus.pushDevSwapRun({ run_id: "r1", stage: "unit", state: "running" });
    expect(bus.getDevSwapRun()?.stageStartedAt).toBe(1_700_000_000);
  });

  it("stageStart 滑动窗口只留 16 键：最旧锚点被逐出后按新 ts 重建", async () => {
    const bus = await freshBus();
    for (let i = 0; i < 17; i++) {
      bus.pushDevSwapRun(mkRun({ run_id: `run${i}`, ts: 100 + i }));
    }
    // run1 仍在窗口内 → 锚点保持首次的 101
    bus.pushDevSwapRun(mkRun({ run_id: "run1", ts: 8888, pct: 60 }));
    expect(bus.getDevSwapRun()?.stageStartedAt).toBe(101);
    // run0 已被逐出 → 用新 ts 重建
    bus.pushDevSwapRun(mkRun({ run_id: "run0", ts: 9999, pct: 70 }));
    expect(bus.getDevSwapRun()?.stageStartedAt).toBe(9999);
  });

  it("新 run 启动清掉上一轮的待切换确认态（2026-11-11 双 pill 实案）", async () => {
    const bus = await freshBus();
    bus.pushDevSwapRun(mkRun({ run_id: "old" }));
    bus.setDevSwapPendingConfirm("pending");
    const confirmFn = vi.fn();
    bus.subscribeDevSwapConfirm(confirmFn);
    bus.pushDevSwapRun(mkRun({ run_id: "new" }));
    expect(bus.isDevSwapConfirmPending()).toBeNull();
    expect(confirmFn).toHaveBeenCalledWith(null);
  });

  it("同一 run 的阶段推进不清确认态", async () => {
    const bus = await freshBus();
    bus.pushDevSwapRun(mkRun({ stage: "quiesce" }));
    bus.setDevSwapPendingConfirm("pending");
    bus.pushDevSwapRun(mkRun({ stage: "unit", ts: 1001 }));
    expect(bus.isDevSwapConfirmPending()).toBe("pending");
  });

  it("首个 run 推送（无上一轮 lastKey）不清确认态", async () => {
    const bus = await freshBus();
    bus.setDevSwapPendingConfirm("pending");
    bus.pushDevSwapRun(mkRun());
    expect(bus.isDevSwapConfirmPending()).toBe("pending");
  });

  it("clearDevSwapRun 清空并广播 null；已空再清不广播；清后同事件可重推", async () => {
    const bus = await freshBus();
    const fn = vi.fn();
    bus.subscribeDevSwap(fn);
    bus.pushDevSwapRun(mkRun());
    bus.clearDevSwapRun();
    expect(bus.getDevSwapRun()).toBeNull();
    expect(fn).toHaveBeenLastCalledWith(null);
    expect(fn).toHaveBeenCalledTimes(2);
    bus.clearDevSwapRun();
    expect(fn).toHaveBeenCalledTimes(2);
    bus.pushDevSwapRun(mkRun()); // lastKey 已重置 → 不再被去重吞掉
    expect(fn).toHaveBeenCalledTimes(3);
  });

  it("unsubscribe 后不再收到事件；多订阅者各自收到", async () => {
    const bus = await freshBus();
    const a = vi.fn();
    const b = vi.fn();
    const offA = bus.subscribeDevSwap(a);
    bus.subscribeDevSwap(b);
    bus.pushDevSwapRun(mkRun());
    offA();
    bus.pushDevSwapRun(mkRun({ pct: 50, ts: 1001 }));
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(2);
  });
});

describe("markDevSwapResultDelivered", () => {
  it("同一 run 只允许落块一次", async () => {
    const bus = await freshBus();
    expect(bus.markDevSwapResultDelivered("r1")).toBe(true);
    expect(bus.markDevSwapResultDelivered("r1")).toBe(false);
  });

  it("超过 8 个 run 逐出最旧：被逐出的可再次落块，新近的仍去重", async () => {
    const bus = await freshBus();
    for (let i = 0; i < 9; i++) {
      expect(bus.markDevSwapResultDelivered(`r${i}`)).toBe(true);
    }
    expect(bus.markDevSwapResultDelivered("r0")).toBe(true); // 已逐出
    expect(bus.markDevSwapResultDelivered("r8")).toBe(false); // 仍在窗口
  });
});

describe("fetchDevSwapSnapshot", () => {
  it("平铺 run 对象 → 真实 normalize 规范化，URL 指向快照端点", async () => {
    const bus = await freshBus();
    const { fetchMock } = stubFetchJson({
      run_id: "r9",
      stage: "build_runtime",
      state: "running",
      detail: "PyInstaller",
      pct: 42,
      ts: 1234,
      started_at: 1000,
      sid: "s1",
    });
    const run = await bus.fetchDevSwapSnapshot();
    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/devswap/progress`);
    expect(run).toEqual({
      run_id: "r9",
      stage: "build_runtime",
      state: "running",
      detail: "PyInstaller",
      pct: 42,
      log_tail: null,
      ts: 1234,
      startedAt: 1000,
      sid: "s1",
    });
  });

  it("{run:{...}} 包装形态解包", async () => {
    const bus = await freshBus();
    stubFetchJson({ ok: true, run: { run_id: "r2", stage: "switch", state: "ok", ts: 5 } });
    const run = await bus.fetchDevSwapSnapshot();
    expect(run?.run_id).toBe("r2");
    expect(run?.stage).toBe("switch");
    expect(run?.state).toBe("ok");
  });

  it("{run:null} → 回落外层对象，无 run_id → null", async () => {
    const bus = await freshBus();
    stubFetchJson({ ok: true, run: null });
    await expect(bus.fetchDevSwapSnapshot()).resolves.toBeNull();
  });

  it("404/非 ok → null 且不读 body", async () => {
    const bus = await freshBus();
    const { json } = stubFetchJson({ run_id: "r1", stage: "unit", state: "running" }, false);
    await expect(bus.fetchDevSwapSnapshot()).resolves.toBeNull();
    expect(json).not.toHaveBeenCalled();
  });

  it("body 为 null / 非对象 → null", async () => {
    const bus = await freshBus();
    stubFetchJson(null);
    await expect(bus.fetchDevSwapSnapshot()).resolves.toBeNull();
    stubFetchJson(42);
    await expect(bus.fetchDevSwapSnapshot()).resolves.toBeNull();
  });

  it("非法 stage / state → normalize 拒绝 → null", async () => {
    const bus = await freshBus();
    stubFetchJson({ run_id: "r1", stage: "bogus", state: "running" });
    await expect(bus.fetchDevSwapSnapshot()).resolves.toBeNull();
    stubFetchJson({ run_id: "r1", stage: "unit", state: "weird" });
    await expect(bus.fetchDevSwapSnapshot()).resolves.toBeNull();
  });

  it("fetch 抛错 / json 抛错 → 静默 null", async () => {
    const bus = await freshBus();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("offline");
      }),
    );
    await expect(bus.fetchDevSwapSnapshot()).resolves.toBeNull();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => {
          throw new Error("bad json");
        },
      })),
    );
    await expect(bus.fetchDevSwapSnapshot()).resolves.toBeNull();
  });
});

describe("resyncDevSwapRun", () => {
  it("快照有 run → 推送到总线并返回", async () => {
    const bus = await freshBus();
    stubFetchJson({ run_id: "r5", stage: "preflight", state: "running", ts: 77 });
    const fn = vi.fn();
    bus.subscribeDevSwap(fn);
    const run = await bus.resyncDevSwapRun();
    expect(run?.run_id).toBe("r5");
    expect(bus.getDevSwapRun()?.run_id).toBe("r5");
    expect(bus.getDevSwapRun()?.stageStartedAt).toBe(77);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("finish 仍 running 且无确认态 → 自动重开确认框（断连兜底）", async () => {
    const bus = await freshBus();
    stubFetchJson({ run: { run_id: "rf", stage: "finish", state: "running", ts: 10 } });
    const confirmFn = vi.fn();
    bus.subscribeDevSwapConfirm(confirmFn);
    await bus.resyncDevSwapRun();
    expect(bus.isDevSwapConfirmPending()).toBe("pending");
    expect(confirmFn).toHaveBeenCalledWith("pending");
  });

  it("已是 postponed → 不覆盖用户选择", async () => {
    const bus = await freshBus();
    bus.setDevSwapPendingConfirm("postponed");
    stubFetchJson({ run_id: "rf", stage: "finish", state: "running", ts: 10 });
    await bus.resyncDevSwapRun();
    expect(bus.isDevSwapConfirmPending()).toBe("postponed");
  });

  it("finish 已 ok → 不弹确认", async () => {
    const bus = await freshBus();
    stubFetchJson({ run_id: "rf", stage: "finish", state: "ok", ts: 10 });
    await bus.resyncDevSwapRun();
    expect(bus.isDevSwapConfirmPending()).toBeNull();
  });

  it("快照无 run → 清空本地态并广播 null", async () => {
    const bus = await freshBus();
    bus.pushDevSwapRun(mkRun());
    const fn = vi.fn();
    bus.subscribeDevSwap(fn);
    stubFetchJson({}, false);
    await expect(bus.resyncDevSwapRun()).resolves.toBeNull();
    expect(bus.getDevSwapRun()).toBeNull();
    expect(fn).toHaveBeenCalledWith(null);
  });
});

describe("切换确认门三态", () => {
  it("null→pending→postponed→null 逐态广播；同值重复设置不广播；退订生效", async () => {
    const bus = await freshBus();
    const fn = vi.fn();
    const off = bus.subscribeDevSwapConfirm(fn);
    expect(bus.isDevSwapConfirmPending()).toBeNull();
    bus.setDevSwapPendingConfirm("pending");
    bus.setDevSwapPendingConfirm("pending"); // 同值 → 不广播
    bus.setDevSwapPendingConfirm("postponed");
    bus.setDevSwapPendingConfirm(null);
    expect(fn.mock.calls.map(([v]) => v)).toEqual(["pending", "postponed", null]);
    off();
    bus.setDevSwapPendingConfirm("pending");
    expect(fn).toHaveBeenCalledTimes(3);
    expect(bus.isDevSwapConfirmPending()).toBe("pending");
  });
});
