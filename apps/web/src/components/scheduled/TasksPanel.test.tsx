/* TasksPanel/ScheduledPage/schedule.ts 接线的源码断言测试——node 无 DOM，
 * 照 TasksOverviewPanel.test.tsx 范式；渲染真实性由 packaged UI e2e 覆盖。
 * 纯逻辑（once 生命周期/过期判定）在 ./shared.test.ts。
 *
 * 钉住 schedule-integrity 的关键接线，防止后续重构无声回退：
 * 1) once Done 只认按计划成功（onceOutcome），不从 enabled 推导；
 * 2) run-now 丢回执 = 「结果未知」分型（isNetworkError），不盲重发；
 * 3) 暂停/删除失败有行内反馈锚点（data-testid）；
 * 4) 配置未知（cfg=null）时全局开关锁死；
 * 5) 写操作走 jsonStrict（非 2xx 抛错，错误体不再伪装成功）。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import enSched from "../../../messages/en/sched.json";
import zhSched from "../../../messages/zh-CN/sched.json";

const read = (rel: string) =>
  readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

const PANEL = read("./TasksPanel.tsx");
const PAGE = read("./ScheduledPage.tsx");
const RUNS = read("./RunsPanel.tsx");
const API = read("../../lib/api/schedule.ts");

/** 双语 catalog 叶子点路径集合（sched 域）。 */
function leafPaths(node: unknown, prefix = ""): Set<string> {
  const out = new Set<string>();
  if (typeof node === "string") {
    out.add(prefix);
    return out;
  }
  for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
    for (const p of leafPaths(v, prefix ? `${prefix}.${k}` : k)) out.add(p);
  }
  return out;
}

// 本组接线引用的 key：en/zh 必须同时存在（拼错 key 运行时回退 key 本身）。
const REQUIRED_KEYS = [
  "tasks.done",
  "tasks.doneLegacy",
  "tasks.onceMissed",
  "tasks.onceCatchingUp",
  "tasks.onceUnknownSource",
  "tasks.netError",
  "tasks.toggleFailed",
  "tasks.runNowUnknown",
  "tasks.viewRuns",
  "tasks.runNowFailed",
  "tasks.deleteFailed",
  "tasks.stateOnceDone",
  "tasks.stateOnceMissed",
  "tasks.stateOnceFailed",
  "tasks.stateOnceSkipped",
  "tasks.stateOnceRunning",
  "tasks.stateOnceCatchingUp",
  "tasks.stateUnknownSource",
  "tasks.stateOverdue",
  "tasks.statePaused",
  "page.loadRetrying",
  "page.loadRetryStopped",
  "page.staleData",
  "page.retryNow",
  "page.globalWriteFailed",
  "runs.clearTask",
] as const;

describe("schedule-integrity 接线", () => {
  it("once 生命周期：onceOutcome 驱动，旧版「once && !enabled 即 Done」的推导不得回归", () => {
    expect(PANEL).toContain("onceOutcome(t)");
    expect(PANEL).toContain('outcome === "done" || outcome === "legacyDone"');
    // 旧判定（从 enabled 推导 Done）若回归，这行会重新出现
    expect(PANEL).not.toContain('t.schedule.kind === "once" && !t.enabled');
    // manual/暂停未执行显示「已暂停」而非 Done
    expect(PANEL).toContain('tr("tasks.statePaused")');
    // 复核缺口②：caught_up（排队补跑）与 unknownSource（老数据无来源）
    // 各有独立态渲染，绝不进入 onceDone（绿色 Done）
    expect(PANEL).toContain('outcome === "catchingUp"');
    expect(PANEL).toContain('outcome === "unknownSource"');
    expect(PANEL).toContain('tr("tasks.stateOnceCatchingUp")');
    expect(PANEL).toContain('tr("tasks.stateUnknownSource")');
  });

  it("run-now 三分型（复核缺口④）：4xx 明确拒绝可安全重试；5xx/解析失败/断连 → 结果未知，绝不提示可安全重试", () => {
    // 分型函数：ginnoStatus 4xx → rejected4xx；其余（5xx/SyntaxError/
    // TypeError/无状态）→ unknown
    expect(PANEL).toContain("function runNowOutcome");
    expect(PANEL).toContain('runNowOutcome(e) === "rejected4xx"');
    expect(PANEL).toContain('tr("tasks.runNowUnknown")');
    expect(PANEL).toContain('tr("tasks.runNowFailed"');
    // unknown 分支必须带对账入口（runsTaskId → 「查看此任务运行记录」按钮）
    expect(PANEL).toContain("runsTaskId: t.id");
    expect(PANEL).toContain('data-testid={`task-view-runs-${t.id}`');
    expect(PANEL).toContain("onOpenTaskRuns(t.id, t.name)");
    // 不自动重发：catch 块里不允许出现对 runScheduleTaskNow 的二次调用
    const catchBlock = PANEL.slice(
      PANEL.indexOf("const runNow"),
      PANEL.indexOf("const remove"),
    );
    expect(catchBlock.match(/api\.runScheduleTaskNow/g)?.length).toBe(1);
    // 旧分型（带 message 的 Error 一律当「可安全重试」）不得回归：5xx 带
    // detail 也会走 jsonStrict 抛 Error，绝不能进 runNowFailed 分支。
    expect(PANEL).not.toContain("isNetworkError(e) || !(e instanceof Error)");
  });

  it("行内反馈锚点：暂停/删除/run-now 的反馈渲染在行内（可对账）", () => {
    expect(PANEL).toContain("task-action-feedback-");
    expect(PANEL).toContain("feedback?.taskId === t.id");
  });

  it("对账闭环（复核缺口④）：run-now unknown → 任务过滤跳进执行记录，chip 可清除", () => {
    // TasksPanel 暴露 onOpenTaskRuns；ScheduledPage 持有 runsTaskFilter 并
    // 跳 runs 页签（清掉单日过滤，看该任务全部记录）；RunsPanel 受控同步
    // taskId + 渲染可清除 chip。
    expect(PANEL).toContain("onOpenTaskRuns: (taskId: string, taskName: string) => void");
    expect(PAGE).toContain("setRunsTaskFilter({ id, name })");
    expect(PAGE).toContain('setTab("runs")');
    expect(RUNS).toContain("taskFilter: { id: string; name: string } | null");
    expect(RUNS).toContain('setTaskId(taskFilter?.id ?? "")');
    expect(RUNS).toContain('data-testid="runs-task-filter-chip"');
    expect(RUNS).toContain("onClearTaskFilter");
  });

  it("配置未知锁开关：cfg=null 时全局开关 disabled（不写猜测状态）", () => {
    expect(PAGE).toContain("disabled={busy || !cfg}");
    // 首载 GET 错误只重试：不渲染 unreachable 断言（key 已无引用）
    expect(PAGE).not.toContain('tr("page.unreachable")');
    expect(PAGE).toContain('tr("page.loadRetrying")');
    // 复核缺口③修正：退避由计数器驱动（旧布尔版 retrying 已 true 时第二次
    // 失败 setRetrying(true) 是 no-op，setTimeout 只排一次，「无限重试」
    // 从未成立）；间隔走 shared.nextRetryDelayMs（1s 起指数、30s 封顶、
    // MAX_AUTO_RETRIES 后停止），不再有硬编码 2000ms 的假无限循环。
    expect(PAGE).toContain("nextRetryDelayMs(retryAttempt)");
    expect(PAGE).toContain("setRetryAttempt((n) => n + 1)");
    expect(PAGE).not.toContain("setTimeout(() => void refresh(), 2000)");
    // 退避放弃后如实说明 + 手动重试仍在
    expect(PAGE).toContain('tr("page.loadRetryStopped")');
    // 已成功后刷新失败 → stale 横幅（不静默降级），带可点重试
    expect(PAGE).toContain('data-testid="schedule-stale-banner"');
    expect(PAGE).toContain('tr("page.staleData")');
    // GET 乱序守卫：过期响应丢弃（WS 事件/手动/退避并发时防旧覆盖新）
    expect(PAGE).toContain("reqSeq");
    expect(PAGE).toContain("seq !== reqSeq.current");
  });

  it("写操作走 jsonStrict：非 2xx 抛 Error(detail)，错误体不再伪装成业务文档", () => {
    expect(API).toContain("jsonStrict");
    // 写路径必须全部换成 jsonStrict（json( 只允许出现在读路径/注释里）
    const writeCalls = API.match(/jsonStrict\(/g)?.length ?? 0;
    expect(writeCalls).toBeGreaterThanOrEqual(5); // put/create/patch/delete/runNow
  });

  it("双语 catalog：接线引用的 key 在 en 与 zh-CN 同时存在", () => {
    const en = leafPaths(enSched.sched);
    const zh = leafPaths(zhSched.sched);
    for (const key of REQUIRED_KEYS) {
      expect(en.has(key), `en missing ${key}`).toBe(true);
      expect(zh.has(key), `zh-CN missing ${key}`).toBe(true);
    }
    // 双语 key 集合一致（防单边加 key）
    expect([...en].filter((k) => !zh.has(k))).toEqual([]);
    expect([...zh].filter((k) => !en.has(k))).toEqual([]);
  });
});
