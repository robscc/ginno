// store/notifications.ts 单元测试（运行完成 / 定时任务 / 后台任务三组通知）。
//
// 纯模块（无 React），node 环境、无 DOM。六个依赖全部 vi.mock 打桩：
//   notifyNative / osGateOpen / isAppAttentive / uiText / schedText / tasksText
// 文案桩返回 `域:key:JSON(params)` 的确定性字符串，断言可以精确到文案 key 与
// 参数（duration 格式化、name 兜底、exit code 等都在参数里）。
//
// 两个关键坑的处理：
// 1. 模块级 _prevRunStatus / _prevTaskStatus 跨用例有状态 → 每个用例前
//    vi.resetModules() + 动态 import 重建被测模块（vi.mock 注册不受 reset 影响，
//    桩函数是 hoisted 的同一批实例，mockClear 后继续用）。末尾有隔离自检用例。
// 2. fmtRunElapsed 未导出 → 不 mock 被测模块自身，经由 done 跃迁的
//    uiText("notifyRun.completed", { duration }) 与 schedule 的
//    schedText("notify.completed", { duration }) 间接断言三档边界。
//
// 浏览器兜底分支（sent=false 之后）用 vi.stubGlobal 伪造 Notification / window，
// 与 desktopNotifyGate.test.ts 同一范式；afterEach 统一 unstub + 恢复真实时钟。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { notifyNative, osGateOpen, isAppAttentive, uiText, schedText, tasksText } = vi.hoisted(
  () => ({
    notifyNative: vi.fn(),
    osGateOpen: vi.fn(),
    isAppAttentive: vi.fn(),
    uiText: vi.fn(),
    schedText: vi.fn(),
    tasksText: vi.fn(),
  }),
);

vi.mock("../desktop", () => ({ notifyNative }));
vi.mock("../notifyPrefs", () => ({ osGateOpen }));
vi.mock("../notifyHub", () => ({ isAppAttentive }));
vi.mock("../../i18n/uiText", () => ({ uiText }));
vi.mock("../../i18n/schedText", () => ({ schedText }));
vi.mock("../../i18n/tasksText", () => ({ tasksText }));

type Mod = typeof import("./notifications");
type Run = Parameters<Mod["notifyRunTransitions"]>[0][number];
type Task = Parameters<Mod["notifyTaskTransitions"]>[0][number];
type Sched = Parameters<Mod["notifyScheduleRunFinished"]>[0];

let mod: Mod;

/** 确定性文案桩：`域:key:JSON(params)`，key 与参数都可被精确断言。 */
const textOf =
  (domain: string) =>
  (key: string, params?: Record<string, unknown>): string =>
    `${domain}:${key}:${JSON.stringify(params ?? {})}`;

/** 伪造浏览器 Notification（node 环境原生没有）。 */
class FakeNotification {
  static permission = "granted";
  static requestPermission = vi.fn(async () => "granted");
  static instances: FakeNotification[] = [];
  onclick: (() => void) | null = null;
  close = vi.fn();
  constructor(
    public title: string,
    public options?: { body?: string },
  ) {
    FakeNotification.instances.push(this);
  }
}

const mkRun = (o: Partial<Run> & { id: string; status: string }): Run => ({
  started: 1000,
  finished: 1042, // 默认 42s
  ...o,
});

const mkSched = (o: Partial<Sched> = {}): Sched => ({
  run_id: "sr1",
  task_name: "日报",
  target_type: "prompt",
  status: "ok",
  started_at: 1000,
  finished_at: 1042,
  summary: "",
  error: "",
  ...o,
});

/** 冲掉 notifyNative(...).then(...) 的微任务链（fake timers 不影响微任务）。 */
async function flush() {
  for (let i = 0; i < 6; i++) await Promise.resolve();
}

/** 取某文案 key 最近一次调用里的 duration 参数（fmtRunElapsed 的间接观测点）。 */
function durationArg(mock: typeof uiText, key: string): string {
  const hit = mock.mock.calls.filter((c: unknown[]) => c[0] === key).pop();
  return (hit![1] as { duration: string }).duration;
}

/** 先 running 播种 prev，再 done 触发跃迁，返回完成文案里的 duration。 */
function doneWithDuration(started: number, finished?: number | null, id = "fmt"): string {
  mod.notifyRunTransitions([{ id, status: "running", started, finished }]);
  mod.notifyRunTransitions([{ id, status: "done", started, finished }]);
  return durationArg(uiText, "notifyRun.completed");
}

beforeEach(async () => {
  vi.resetModules(); // 重置模块级 _prevRunStatus / _prevTaskStatus
  vi.clearAllMocks();
  notifyNative.mockResolvedValue(true);
  osGateOpen.mockReturnValue(true);
  isAppAttentive.mockReturnValue(false);
  uiText.mockImplementation(textOf("ui"));
  schedText.mockImplementation(textOf("sched"));
  tasksText.mockImplementation(textOf("tasks"));
  FakeNotification.instances.length = 0;
  FakeNotification.permission = "granted";
  mod = await import("./notifications");
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// fmtRunElapsed（未导出，经完成通知的 duration 参数间接覆盖）
// ---------------------------------------------------------------------------
describe("fmtRunElapsed 三档边界（经 notifyRun.completed 的 duration）", () => {
  it("0s → '0s'", () => {
    expect(doneWithDuration(1000, 1000)).toBe("0s");
  });

  it("59s → '59s'（<60s 档上界）", () => {
    expect(doneWithDuration(1000, 1059)).toBe("59s");
  });

  it("60s → '1m 0s'（进入分钟档）", () => {
    expect(doneWithDuration(1000, 1060)).toBe("1m 0s");
  });

  it("3599s → '59m 59s'（分钟档上界）", () => {
    expect(doneWithDuration(1000, 4599)).toBe("59m 59s");
  });

  it("3600s → '1h 0m'（进入小时档）", () => {
    expect(doneWithDuration(1000, 4600)).toBe("1h 0m");
  });

  it("7325s → '2h 2m'（小时档丢弃秒）", () => {
    expect(doneWithDuration(1000, 8325)).toBe("2h 2m");
  });

  it("负数（finished 早于 started）→ Math.max 兜底 '0s'", () => {
    expect(doneWithDuration(1000, 990)).toBe("0s");
  });

  it("小数四舍五入：0.4s → '0s'，0.6s → '1s'", () => {
    expect(doneWithDuration(1000, 1000.4, "r1")).toBe("0s");
    expect(doneWithDuration(1000, 1000.6, "r2")).toBe("1s");
  });

  it("59.5s  rounds 到 60 → 跨档 '1m 0s'", () => {
    expect(doneWithDuration(1000, 1059.5)).toBe("1m 0s");
  });

  it("finished=null → Date.now()/1000 兜底", () => {
    vi.useFakeTimers();
    vi.setSystemTime(5_000_000); // 5000s
    expect(doneWithDuration(4940, null)).toBe("1m 0s");
  });

  it("finished=undefined → 同样走 Date.now() 兜底", () => {
    vi.useFakeTimers();
    vi.setSystemTime(5_000_000);
    expect(doneWithDuration(4940, undefined)).toBe("1m 0s");
  });
});

// ---------------------------------------------------------------------------
// notifyRunTransitions：状态 diff 与派发
// ---------------------------------------------------------------------------
describe("notifyRunTransitions 状态跃迁", () => {
  it("空 runs → 什么都不做", () => {
    mod.notifyRunTransitions([]);
    expect(notifyNative).not.toHaveBeenCalled();
    expect(uiText).not.toHaveBeenCalled();
  });

  it("首次出现即 done（无 prev）→ 不通知", () => {
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "done" })]);
    expect(notifyNative).not.toHaveBeenCalled();
  });

  it("running→done → 精确派发 workflow-run 载荷", () => {
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "running" })]);
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "done", name: "部署" })]);
    expect(notifyNative).toHaveBeenCalledTimes(1);
    expect(notifyNative).toHaveBeenCalledWith({
      kind: "workflow-run",
      notifyKind: "workflow",
      id: "r1",
      title: 'ui:notifyRun.titleDone:{"name":"部署"}',
      body: 'ui:notifyRun.completed:{"duration":"42s"}',
    });
  });

  it("done→done 不重复通知", () => {
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "running" })]);
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "done" })]);
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "done" })]);
    expect(notifyNative).toHaveBeenCalledTimes(1);
  });

  it("running→running 不通知", () => {
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "running" })]);
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "running" })]);
    expect(notifyNative).not.toHaveBeenCalled();
  });

  it("running→failed（有失败步骤标题）→ failedAt 文案带步骤名", () => {
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "running" })]);
    mod.notifyRunTransitions([
      mkRun({
        id: "r1",
        status: "failed",
        name: "部署",
        steps: [
          { id: "s1", title: "拉代码", status: "done" },
          { id: "s2", title: "构建", status: "failed" },
        ],
      }),
    ]);
    expect(notifyNative).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'ui:notifyRun.titleFailed:{"name":"部署"}',
        body: 'ui:notifyRun.failedAt:{"step":"构建"}',
      }),
    );
  });

  it("running→failed（steps 里无 failed 项）→ 泛化 failed 文案", () => {
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "running" })]);
    mod.notifyRunTransitions([
      mkRun({ id: "r1", status: "failed", steps: [{ id: "s1", status: "done" }] }),
    ]);
    expect(notifyNative).toHaveBeenCalledWith(
      expect.objectContaining({ body: "ui:notifyRun.failed:{}" }),
    );
  });

  it("running→failed（缺 steps）→ 泛化 failed 文案", () => {
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "running" })]);
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "failed" })]);
    expect(notifyNative).toHaveBeenCalledWith(
      expect.objectContaining({ body: "ui:notifyRun.failed:{}" }),
    );
  });

  it("失败步骤无 title → 同样退到泛化文案", () => {
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "running" })]);
    mod.notifyRunTransitions([
      mkRun({ id: "r1", status: "failed", steps: [{ id: "s2", status: "failed" }] }),
    ]);
    expect(notifyNative).toHaveBeenCalledWith(
      expect.objectContaining({ body: "ui:notifyRun.failed:{}" }),
    );
  });

  it("done→failed 也通知（终态之间的跃迁同样捕获）", () => {
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "done" })]);
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "failed" })]);
    expect(notifyNative).toHaveBeenCalledTimes(1);
    expect(notifyNative).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "workflow-run", id: "r1" }),
    );
  });

  it("running→queued 不通知，但 prev 已更新：queued→done 能捕获", () => {
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "running" })]);
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "queued" })]);
    expect(notifyNative).not.toHaveBeenCalled();
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "done" })]);
    expect(notifyNative).toHaveBeenCalledTimes(1);
  });

  it("缺 name → 标题用 defaultName 兜底", () => {
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "running" })]);
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "done" })]);
    expect(uiText).toHaveBeenCalledWith("notifyRun.defaultName");
    expect(notifyNative).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'ui:notifyRun.titleDone:{"name":"ui:notifyRun.defaultName:{}"}',
      }),
    );
  });

  it("app 专注（用户在看）→ 不派发，但 prev 照常更新", () => {
    isAppAttentive.mockReturnValue(true);
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "running" })]);
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "done" })]);
    expect(notifyNative).not.toHaveBeenCalled();
    // 恢复不专注后重发同一终态：prev 已更新，不应补弹
    isAppAttentive.mockReturnValue(false);
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "done" })]);
    expect(notifyNative).not.toHaveBeenCalled();
  });

  it("出口闸门拦下（sent=false）也更新 prev：重开后不补弹过期跃迁", async () => {
    notifyNative.mockResolvedValue(false);
    vi.stubGlobal("Notification", undefined); // 浏览器兜底也不弹
    mod.notifyRunTransitions([mkRun({ id: "g", status: "running" })]);
    mod.notifyRunTransitions([mkRun({ id: "g", status: "done" })]);
    await flush();
    notifyNative.mockResolvedValue(true); // 模拟重新开启
    mod.notifyRunTransitions([mkRun({ id: "g", status: "done" })]);
    await flush();
    expect(notifyNative).toHaveBeenCalledTimes(1);
  });

  it("一次调用多个 run 各自独立 diff", () => {
    mod.notifyRunTransitions([
      mkRun({ id: "a", status: "running" }),
      mkRun({ id: "b", status: "running" }),
    ]);
    mod.notifyRunTransitions([
      mkRun({ id: "a", status: "done", name: "A" }),
      mkRun({ id: "b", status: "failed", name: "B" }),
    ]);
    expect(notifyNative).toHaveBeenCalledTimes(2);
    expect(notifyNative).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({ id: "a", body: 'ui:notifyRun.completed:{"duration":"42s"}' }),
    );
    expect(notifyNative).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ id: "b", body: "ui:notifyRun.failed:{}" }),
    );
  });

  it("run 与 task 的 prev map 相互独立（同 id 不串）", () => {
    mod.notifyRunTransitions([mkRun({ id: "x", status: "running" })]);
    mod.notifyTaskTransitions([{ id: "x", status: "finished" }]); // task 首次出现
    expect(notifyNative).not.toHaveBeenCalled();
    mod.notifyRunTransitions([mkRun({ id: "x", status: "done" })]);
    expect(notifyNative).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// notifyRunTransitions：sent=false 后的浏览器兜底
// ---------------------------------------------------------------------------
describe("notifyRunTransitions 浏览器兜底（sent=false）", () => {
  beforeEach(() => {
    notifyNative.mockResolvedValue(false);
  });

  /** 播种 running→done 并冲掉微任务。 */
  async function fireDone(onOpenPanel?: () => void) {
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "running" })]);
    mod.notifyRunTransitions([mkRun({ id: "r1", status: "done", name: "部署" })], onOpenPanel);
    await flush();
  }

  it("osGateOpen('workflow')=false → 兜底也不弹（偏好闸门二次判定）", async () => {
    osGateOpen.mockReturnValue(false);
    vi.stubGlobal("Notification", FakeNotification);
    await fireDone();
    expect(osGateOpen).toHaveBeenCalledWith("workflow");
    expect(FakeNotification.instances).toHaveLength(0);
    expect(FakeNotification.requestPermission).not.toHaveBeenCalled();
  });

  it("无 Notification API（打包壳内）→ 静默 no-op", async () => {
    vi.stubGlobal("Notification", undefined);
    await expect(fireDone()).resolves.toBeUndefined();
    expect(osGateOpen).toHaveBeenCalledWith("workflow");
  });

  it("permission=default → 只请求一次授权，不弹通知", async () => {
    FakeNotification.permission = "default";
    vi.stubGlobal("Notification", FakeNotification);
    await fireDone();
    expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(1);
    expect(FakeNotification.instances).toHaveLength(0);
  });

  it("requestPermission 抛异常 → 被 catch 吞掉", async () => {
    FakeNotification.permission = "default";
    FakeNotification.requestPermission.mockImplementation(() => {
      throw new Error("unsupported");
    });
    vi.stubGlobal("Notification", FakeNotification);
    await expect(fireDone()).resolves.toBeUndefined();
    expect(FakeNotification.instances).toHaveLength(0);
  });

  it("permission=denied → 不弹也不请求授权", async () => {
    FakeNotification.permission = "denied";
    vi.stubGlobal("Notification", FakeNotification);
    await fireDone();
    expect(FakeNotification.requestPermission).not.toHaveBeenCalled();
    expect(FakeNotification.instances).toHaveLength(0);
  });

  it("permission=granted → 弹 HTML5 通知；点击 → focus + 打开面板 + close", async () => {
    const focus = vi.fn();
    vi.stubGlobal("window", { focus });
    vi.stubGlobal("Notification", FakeNotification);
    const onOpenPanel = vi.fn();
    await fireDone(onOpenPanel);
    expect(FakeNotification.instances).toHaveLength(1);
    const n = FakeNotification.instances[0];
    expect(n.title).toBe('ui:notifyRun.titleDone:{"name":"部署"}');
    expect(n.options).toEqual({ body: 'ui:notifyRun.completed:{"duration":"42s"}' });
    n.onclick?.();
    expect(focus).toHaveBeenCalledTimes(1);
    expect(onOpenPanel).toHaveBeenCalledTimes(1);
    expect(n.close).toHaveBeenCalledTimes(1);
  });

  it("未传 onOpenPanel 时点击也不崩（可选链）", async () => {
    const focus = vi.fn();
    vi.stubGlobal("window", { focus });
    vi.stubGlobal("Notification", FakeNotification);
    await fireDone();
    const n = FakeNotification.instances[0];
    expect(() => n.onclick?.()).not.toThrow();
    expect(focus).toHaveBeenCalledTimes(1);
    expect(n.close).toHaveBeenCalledTimes(1);
  });

  it("new Notification 抛异常（被系统拦截）→ catch 吞掉", async () => {
    vi.stubGlobal(
      "Notification",
      class {
        static permission = "granted";
        constructor() {
          throw new Error("blocked");
        }
      },
    );
    await expect(fireDone()).resolves.toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// notifyScheduleRunFinished
// ---------------------------------------------------------------------------
describe("notifyScheduleRunFinished", () => {
  it("app 专注 → 直接返回", () => {
    isAppAttentive.mockReturnValue(true);
    mod.notifyScheduleRunFinished(mkSched());
    expect(notifyNative).not.toHaveBeenCalled();
  });

  it("非终态（running/skipped_overlap/missed/caught_up）→ 不通知", () => {
    for (const status of ["running", "skipped_overlap", "missed", "caught_up"]) {
      mod.notifyScheduleRunFinished(mkSched({ status }));
    }
    expect(notifyNative).not.toHaveBeenCalled();
  });

  it("workflow 目标 → 不通知（避免与 run 轮询双发）", () => {
    mod.notifyScheduleRunFinished(mkSched({ target_type: "workflow" }));
    expect(notifyNative).not.toHaveBeenCalled();
  });

  it("ok + summary → 正文用 trim 后的摘要，载荷精确", () => {
    mod.notifyScheduleRunFinished(mkSched({ summary: "  生成完毕，共 3 节  " }));
    expect(notifyNative).toHaveBeenCalledTimes(1);
    expect(notifyNative).toHaveBeenCalledWith({
      kind: "schedule-run",
      notifyKind: "schedule",
      id: "sr1",
      title: 'sched:notify.titleDone:{"name":"日报"}',
      body: "生成完毕，共 3 节",
    });
  });

  it("ok + 空 summary → 退到时长文案", () => {
    mod.notifyScheduleRunFinished(mkSched({ summary: "" }));
    expect(notifyNative).toHaveBeenCalledWith(
      expect.objectContaining({ body: 'sched:notify.completed:{"duration":"42s"}' }),
    );
  });

  it("ok + 纯空白 summary → trim 后为空，同样退到时长文案", () => {
    mod.notifyScheduleRunFinished(mkSched({ summary: "   " }));
    expect(notifyNative).toHaveBeenCalledWith(
      expect.objectContaining({ body: 'sched:notify.completed:{"duration":"42s"}' }),
    );
  });

  it("error + error 原文 → 正文用 trim 后的错误，标题走 failed", () => {
    mod.notifyScheduleRunFinished(mkSched({ status: "error", error: "  超时  " }));
    expect(notifyNative).toHaveBeenCalledWith({
      kind: "schedule-run",
      notifyKind: "schedule",
      id: "sr1",
      title: 'sched:notify.titleFailed:{"name":"日报"}',
      body: "超时",
    });
  });

  it("error + 缺 error → 泛化失败文案", () => {
    mod.notifyScheduleRunFinished(mkSched({ status: "error", error: null }));
    expect(notifyNative).toHaveBeenCalledWith(
      expect.objectContaining({ body: "sched:notify.failed:{}" }),
    );
  });

  it("task_name 空白 → defaultName 兜底", () => {
    mod.notifyScheduleRunFinished(mkSched({ task_name: "   " }));
    expect(schedText).toHaveBeenCalledWith("notify.defaultName");
    expect(notifyNative).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'sched:notify.titleDone:{"name":"sched:notify.defaultName:{}"}',
      }),
    );
  });

  it("task_name 空串 → defaultName 兜底（|| 左支）", () => {
    mod.notifyScheduleRunFinished(mkSched({ task_name: "" }));
    expect(schedText).toHaveBeenCalledWith("notify.defaultName");
    expect(notifyNative).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'sched:notify.titleDone:{"name":"sched:notify.defaultName:{}"}',
      }),
    );
  });

  it("正文超 200 字符 → 截到 199 + 省略号", () => {
    mod.notifyScheduleRunFinished(mkSched({ summary: "x".repeat(250) }));
    const payload = notifyNative.mock.calls[0][0] as { body: string };
    expect(payload.body).toBe("x".repeat(199) + "…");
    expect(payload.body).toHaveLength(200);
  });

  it("正文恰好 200 字符 → 不截断", () => {
    mod.notifyScheduleRunFinished(mkSched({ summary: "y".repeat(200) }));
    const payload = notifyNative.mock.calls[0][0] as { body: string };
    expect(payload.body).toBe("y".repeat(200));
  });

  it("started_at/finished_at 均 null → 从 0 起算到 Date.now()", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(90_000); // 90s
    mod.notifyScheduleRunFinished(mkSched({ started_at: null, finished_at: null, summary: null }));
    await flush();
    expect(schedText).toHaveBeenCalledWith("notify.completed", { duration: "1m 30s" });
  });

  it("sent=false + osGateOpen('schedule')=false → 兜底不弹", async () => {
    notifyNative.mockResolvedValue(false);
    osGateOpen.mockReturnValue(false);
    vi.stubGlobal("Notification", FakeNotification);
    mod.notifyScheduleRunFinished(mkSched());
    await flush();
    expect(osGateOpen).toHaveBeenCalledWith("schedule");
    expect(FakeNotification.instances).toHaveLength(0);
  });

  it("sent=false + granted → 兜底弹出；点击 → focus + devOpen(run_id) + close", async () => {
    notifyNative.mockResolvedValue(false);
    const focus = vi.fn();
    vi.stubGlobal("window", { focus });
    vi.stubGlobal("Notification", FakeNotification);
    const devOpen = vi.fn();
    mod.notifyScheduleRunFinished(mkSched({ summary: "结果" }), devOpen);
    await flush();
    expect(FakeNotification.instances).toHaveLength(1);
    const n = FakeNotification.instances[0];
    expect(n.title).toBe('sched:notify.titleDone:{"name":"日报"}');
    expect(n.options).toEqual({ body: "结果" });
    n.onclick?.();
    expect(focus).toHaveBeenCalledTimes(1);
    expect(devOpen).toHaveBeenCalledWith("sr1");
    expect(n.close).toHaveBeenCalledTimes(1);
  });

  it("sent=false + 未传 devOpen 时点击也不崩（可选链）", async () => {
    notifyNative.mockResolvedValue(false);
    const focus = vi.fn();
    vi.stubGlobal("window", { focus });
    vi.stubGlobal("Notification", FakeNotification);
    mod.notifyScheduleRunFinished(mkSched());
    await flush();
    const n = FakeNotification.instances[0];
    expect(() => n.onclick?.()).not.toThrow();
    expect(focus).toHaveBeenCalledTimes(1);
    expect(n.close).toHaveBeenCalledTimes(1);
  });

  it("sent=false + new Notification 抛异常 → catch 吞掉", async () => {
    notifyNative.mockResolvedValue(false);
    vi.stubGlobal(
      "Notification",
      class {
        static permission = "granted";
        constructor() {
          throw new Error("blocked");
        }
      },
    );
    mod.notifyScheduleRunFinished(mkSched());
    await expect(flush()).resolves.toBeUndefined();
  });

  it("sent=false + 无 Notification API（打包壳内）→ 静默 no-op", async () => {
    notifyNative.mockResolvedValue(false);
    vi.stubGlobal("Notification", undefined);
    mod.notifyScheduleRunFinished(mkSched());
    await expect(flush()).resolves.toBeUndefined();
    expect(osGateOpen).toHaveBeenCalledWith("schedule");
  });

  it("sent=false + permission=default → 请求授权不弹；denied → 静默", async () => {
    notifyNative.mockResolvedValue(false);
    vi.stubGlobal("Notification", FakeNotification);
    FakeNotification.permission = "default";
    mod.notifyScheduleRunFinished(mkSched({ run_id: "a" }));
    await flush();
    expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(1);
    expect(FakeNotification.instances).toHaveLength(0);
    FakeNotification.permission = "denied";
    mod.notifyScheduleRunFinished(mkSched({ run_id: "b" }));
    await flush();
    expect(FakeNotification.instances).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// notifyTaskTransitions
// ---------------------------------------------------------------------------
describe("notifyTaskTransitions", () => {
  it("首次出现即 finished（无 prev）→ 不通知", () => {
    mod.notifyTaskTransitions([{ id: "t1", status: "finished", title: "构建" }]);
    expect(notifyNative).not.toHaveBeenCalled();
  });

  it("running→finished → 精确派发 bg-task 载荷（exit=0 不被 ?? 吞掉）", () => {
    mod.notifyTaskTransitions([{ id: "t1", status: "running" }]);
    mod.notifyTaskTransitions([
      { id: "t1", status: "finished", title: "构建", exit_code: 0, dur: "3s" },
    ]);
    expect(notifyNative).toHaveBeenCalledTimes(1);
    expect(notifyNative).toHaveBeenCalledWith({
      kind: "bg-task",
      notifyKind: "bgTask",
      id: "t1",
      title: "构建",
      body: 'tasks:notifyFinished:{"name":"构建","exit":"0","dur":"3s"}',
    });
  });

  it("exit_code=null / undefined → '?'", () => {
    mod.notifyTaskTransitions([
      { id: "a", status: "running" },
      { id: "b", status: "running" },
    ]);
    mod.notifyTaskTransitions([
      { id: "a", status: "finished", title: "A", exit_code: null },
      { id: "b", status: "finished", title: "B" },
    ]);
    expect(notifyNative).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({ body: 'tasks:notifyFinished:{"name":"A","exit":"?","dur":""}' }),
    );
    expect(notifyNative).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ body: 'tasks:notifyFinished:{"name":"B","exit":"?","dur":""}' }),
    );
  });

  it("缺 title → 用 id 当名字；缺 dur → 空串", () => {
    mod.notifyTaskTransitions([{ id: "t9", status: "running" }]);
    mod.notifyTaskTransitions([{ id: "t9", status: "finished", exit_code: 2 }]);
    expect(notifyNative).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "t9",
        body: 'tasks:notifyFinished:{"name":"t9","exit":"2","dur":""}',
      }),
    );
  });

  it("running→stopped / timeout → notifyStopped 文案", () => {
    mod.notifyTaskTransitions([
      { id: "a", status: "running" },
      { id: "b", status: "running" },
    ]);
    mod.notifyTaskTransitions([
      { id: "a", status: "stopped", title: "A", dur: "8s" },
      { id: "b", status: "timeout", title: "B" },
    ]);
    expect(notifyNative).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({ body: 'tasks:notifyStopped:{"name":"A","dur":"8s"}' }),
    );
    expect(notifyNative).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ body: 'tasks:notifyStopped:{"name":"B","dur":""}' }),
    );
  });

  it("finished→finished 不重复；running→running 不通知", () => {
    mod.notifyTaskTransitions([{ id: "t1", status: "running" }]);
    mod.notifyTaskTransitions([{ id: "t1", status: "running" }]);
    mod.notifyTaskTransitions([{ id: "t1", status: "finished", title: "T" }]);
    mod.notifyTaskTransitions([{ id: "t1", status: "finished", title: "T" }]);
    expect(notifyNative).toHaveBeenCalledTimes(1);
  });

  it("非终态跃迁（running→killed）不通知，但 prev 更新：killed→finished 能捕获", () => {
    mod.notifyTaskTransitions([{ id: "t1", status: "running" }]);
    mod.notifyTaskTransitions([{ id: "t1", status: "killed" }]);
    expect(notifyNative).not.toHaveBeenCalled();
    mod.notifyTaskTransitions([{ id: "t1", status: "finished", title: "T" }]);
    expect(notifyNative).toHaveBeenCalledTimes(1);
  });

  it("app 专注 → 不派发但 prev 更新，恢复后不补弹", () => {
    isAppAttentive.mockReturnValue(true);
    mod.notifyTaskTransitions([{ id: "t1", status: "running" }]);
    mod.notifyTaskTransitions([{ id: "t1", status: "finished", title: "T" }]);
    expect(notifyNative).not.toHaveBeenCalled();
    isAppAttentive.mockReturnValue(false);
    mod.notifyTaskTransitions([{ id: "t1", status: "finished", title: "T" }]);
    expect(notifyNative).not.toHaveBeenCalled();
  });

  it("notifyNative reject → .catch 吞掉，不产生未处理拒绝", async () => {
    notifyNative.mockRejectedValue(new Error("shell gone"));
    mod.notifyTaskTransitions([{ id: "t1", status: "running" }]);
    mod.notifyTaskTransitions([{ id: "t1", status: "finished", title: "T" }]);
    await flush();
    expect(notifyNative).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// 模块级状态隔离自检
// ---------------------------------------------------------------------------
describe("模块级 prev 状态隔离", () => {
  it("resetModules + 重新 import 后 prev 归零（旧模块的 running 不泄漏）", async () => {
    mod.notifyRunTransitions([mkRun({ id: "iso", status: "running" })]);
    vi.resetModules();
    const fresh = await import("./notifications");
    // 若状态泄漏：fresh 会看到 prev=running → done 跃迁 → 派发；
    // 隔离干净：首次出现 → 不派发。
    fresh.notifyRunTransitions([mkRun({ id: "iso", status: "done" })]);
    expect(notifyNative).not.toHaveBeenCalled();
  });
});
