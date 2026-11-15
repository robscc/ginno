// 工作流失败诊断报告（lib/errorReport.ts）单测。
//
// 仅 type import 的纯逻辑模块，无需 mock 依赖。时间断言用「本地构造的
// Date」反推期望值，避免 CI UTC runner 与本机时区差异（CLAUDE.md 时间敏感
// 测试红线）；toLocaleString 期望值在测试内同式计算，不硬编码 locale 输出。
// copyText 的 navigator.clipboard 用 vi.stubGlobal 打桩，afterEach 复原。

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  buildRunErrorReport,
  copyText,
  failedStep,
  formatEventLine,
} from "@/lib/errorReport";
import type { WorkflowRun, WorkflowRunEvent } from "@/lib/types";

/** 最小合法 run 记录；started/updated 给 0 → fmtISO 走「—」分支。 */
function mkRun(over: Partial<WorkflowRun> = {}): WorkflowRun {
  return {
    id: "run-abcdef12-3456",
    workflow_id: "wf-1",
    name: "测试工作流",
    status: "failed",
    steps: [],
    started: 0,
    updated: 0,
    ...over,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("formatEventLine", () => {
  it("tool_call：列出工具名，缺名补 ?，缺 calls 为空列表", () => {
    expect(formatEventLine({ kind: "tool_call", calls: [{ name: "read_file" }, { name: "bash" }] })).toBe(
      "calls: read_file, bash",
    );
    expect(formatEventLine({ kind: "tool_call", calls: [{}] })).toBe("calls: ?");
    expect(formatEventLine({ kind: "tool_call" })).toBe("calls: ");
  });

  it("tool_result：名称 + 内容，空白折叠为单空格", () => {
    expect(formatEventLine({ kind: "tool_result", name: "bash", content: "ok" })).toBe("bash: ok");
    expect(formatEventLine({ kind: "tool_result", name: "bash", content: "a\n  b\tc" })).toBe("bash: a b c");
  });

  it("tool_result：超 200 字符截断加省略号；恰 200 不截断", () => {
    const long = "x".repeat(250);
    expect(formatEventLine({ kind: "tool_result", name: "t", content: long })).toBe(
      `t: ${"x".repeat(200)}…`,
    );
    expect(formatEventLine({ kind: "tool_result", content: "y".repeat(200) })).toBe(
      `: ${"y".repeat(200)}`,
    );
  });

  it("tool_result：content/name 缺失的兜底", () => {
    expect(formatEventLine({ kind: "tool_result", name: "t" })).toBe("t: ");
    expect(formatEventLine({ kind: "tool_result" })).toBe(": ");
  });

  it("context_write / loop_iter / branch_decision", () => {
    expect(formatEventLine({ kind: "context_write", keys: ["a", "b"] })).toBe("keys: a, b");
    expect(formatEventLine({ kind: "context_write" })).toBe("keys: ");
    expect(formatEventLine({ kind: "loop_iter", index: 2, of: 5 })).toBe("iter 2/5");
    expect(formatEventLine({ kind: "loop_iter" })).toBe("iter ?/?");
    expect(formatEventLine({ kind: "branch_decision", chosen: "left" })).toBe("→ left");
    expect(formatEventLine({ kind: "branch_decision" })).toBe("→ ");
  });

  it("error / interrupt / node_exit", () => {
    expect(formatEventLine({ kind: "error", error: "boom" })).toBe("boom");
    expect(formatEventLine({ kind: "error" })).toBe("");
    expect(formatEventLine({ kind: "interrupt", question: "继续?" })).toBe("继续?");
    expect(formatEventLine({ kind: "interrupt" })).toBe("");
    expect(formatEventLine({ kind: "node_exit", status: "done" })).toBe("status=done");
    expect(formatEventLine({ kind: "node_exit" })).toBe("");
  });

  it("未知 kind：剥掉 ts/run_id/kind/node_id 后紧凑 JSON", () => {
    expect(formatEventLine({ kind: "mystery", ts: 1, run_id: "r", node_id: "n", foo: "bar" })).toBe(
      '{"foo":"bar"}',
    );
    expect(formatEventLine({ kind: "mystery" })).toBe("{}");
    expect(formatEventLine({ foo: 1 })).toBe('{"foo":1}'); // kind 缺失同样走 fallback
  });

  it("fallback JSON 超 200 截断；循环引用 stringify 抛错 → 空串", () => {
    const longEv: WorkflowRunEvent = { kind: "mystery", blob: "z".repeat(300) };
    expect(formatEventLine(longEv)).toBe(JSON.stringify({ blob: "z".repeat(300) }).slice(0, 200) + "…");
    const circular: WorkflowRunEvent = { kind: "mystery" };
    circular.self = circular;
    expect(formatEventLine(circular)).toBe("");
  });
});

describe("failedStep", () => {
  const steps = [
    { id: "s1", title: "步骤一", status: "done" },
    { id: "s2", title: "步骤二", status: "failed" },
  ];

  it("error_detail.node_id 优先于 failed 状态", () => {
    expect(failedStep(mkRun({ steps, error_detail: { node_id: "s1" } }))).toEqual({
      id: "s1",
      title: "步骤一",
    });
  });

  it("无 node_id → 取第一个 failed 步骤", () => {
    expect(failedStep(mkRun({ steps }))).toEqual({ id: "s2", title: "步骤二" });
    expect(failedStep(mkRun({ steps, error_detail: null }))).toEqual({ id: "s2", title: "步骤二" });
  });

  it("node_id 指向不存在的步骤 → 回落 failed 状态查找", () => {
    expect(failedStep(mkRun({ steps, error_detail: { node_id: "ghost" } }))).toEqual({
      id: "s2",
      title: "步骤二",
    });
  });

  it("title 为空 → 用 id 兜底", () => {
    expect(failedStep(mkRun({ steps: [{ id: "s1", title: "", status: "failed" }] }))).toEqual({
      id: "s1",
      title: "s1",
    });
  });

  it("没有任何失败步骤 → null", () => {
    expect(failedStep(mkRun({ steps: [{ id: "s1", title: "ok", status: "done" }] }))).toBeNull();
    expect(failedStep(mkRun())).toBeNull();
  });
});

describe("buildRunErrorReport", () => {
  // 本地时间构造 → 期望值与时区无关（CI UTC runner 同样成立）
  const START_TS = new Date(2026, 0, 2, 3, 4, 5).getTime() / 1000;
  const END_TS = new Date(2026, 0, 2, 3, 6, 7).getTime() / 1000;

  it("完整报告：概览/错误/traceback/诊断指针齐备", () => {
    const run = mkRun({
      steps: [{ id: "s2", title: "抓取", status: "failed" }],
      error: "HTTP 500",
      error_detail: { node_id: "s2", traceback: "Traceback: line1\nline2" },
      dsl_version: 3,
      started: START_TS,
      finished: END_TS,
    });
    const md = buildRunErrorReport(run);
    expect(md).toContain("# Ginno Workflow Error Report");
    expect(md).toContain("- Workflow: 测试工作流 (`wf-1`)");
    expect(md).toContain("- Run ID: `run-abcdef12-3456`");
    expect(md).toContain("- DSL version: v3");
    expect(md).toContain("- Status: failed");
    expect(md).toContain(`- Started: ${new Date(START_TS * 1000).toLocaleString()}`);
    expect(md).toContain(`- Finished: ${new Date(END_TS * 1000).toLocaleString()}`);
    expect(md).toContain("- Failed step: 抓取 (`s2`)");
    expect(md).toContain("HTTP 500");
    expect(md).toContain("Traceback: line1\nline2");
    expect(md).toContain("grep `workflow_run_failed run=run-abcd`"); // run.id 前 8 位
    expect(md).toContain("~/.ginno/workflow_runs/run-abcdef12-3456.events.jsonl");
    expect(md).not.toContain("Recent events"); // 未传 events → 无事件尾段
  });

  it("缺省字段兜底：name/?、v?、时间 —、无错误信息、无 traceback", () => {
    const md = buildRunErrorReport(mkRun({ name: "" }));
    expect(md).toContain("- Workflow: ? (`wf-1`)");
    expect(md).toContain("- DSL version: v?");
    expect(md).toContain("- Started: —");
    expect(md).toContain("- Finished: —");
    expect(md).toContain("(no error message)");
    expect(md).toContain("(unavailable");
    expect(md).not.toContain("Failed step");
  });

  it("error_detail 无 traceback → 取最后一个带 traceback 的 error 事件", () => {
    const events: WorkflowRunEvent[] = [
      { kind: "error", traceback: "tb-old" },
      { kind: "error" }, // 无 traceback，被 filter(Boolean) 掉
      { kind: "error", traceback: "tb-new" },
      { kind: "node_exit", status: "failed" },
    ];
    const md = buildRunErrorReport(mkRun(), events);
    expect(md).toContain("tb-new");
    expect(md).not.toContain("tb-old");
    expect(md).toContain("## Recent events (last 4)");
  });

  it("error_detail.traceback 优先于事件里的 traceback", () => {
    const md = buildRunErrorReport(mkRun({ error_detail: { traceback: "detail-tb" } }), [
      { kind: "error", traceback: "event-tb" },
    ]);
    expect(md).toContain("detail-tb");
    expect(md).not.toContain("event-tb");
  });

  it("事件尾段只保留最后 15 条", () => {
    const events: WorkflowRunEvent[] = Array.from({ length: 20 }, (_, i) => ({
      kind: "loop_iter",
      index: i,
      of: 20,
    }));
    const md = buildRunErrorReport(mkRun(), events);
    expect(md).toContain("## Recent events (last 15)");
    expect(md).toContain("iter 5/20");
    expect(md).toContain("iter 19/20");
    expect(md).not.toContain("iter 4/20"); // 前 5 条被截掉
  });

  it("事件行：本地时钟 + kind + [node_id] + 摘要", () => {
    const md = buildRunErrorReport(mkRun(), [
      { ts: START_TS, kind: "error", node_id: "n1", error: "boom" },
    ]);
    expect(md).toContain("03:04:05 error [n1] boom");
  });

  it("事件缺 ts/node_id/kind → --:--:-- 与 ? 兜底", () => {
    const md = buildRunErrorReport(mkRun(), [{}]);
    expect(md).toContain("--:--:-- ? {}");
  });

  it("events 为空数组 → 不渲染事件尾段", () => {
    expect(buildRunErrorReport(mkRun(), [])).not.toContain("Recent events");
  });
});

describe("copyText", () => {
  it("clipboard 写入成功 → true，文本原样传递", async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    await expect(copyText("hello")).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith("hello");
  });

  it("写入被拒（权限/焦点）→ false，不抛异常", async () => {
    const writeText = vi.fn(async () => {
      throw new Error("denied");
    });
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    await expect(copyText("hello")).resolves.toBe(false);
  });

  it("环境无 clipboard（node/旧 webview）→ false", async () => {
    vi.stubGlobal("navigator", {});
    await expect(copyText("hello")).resolves.toBe(false);
  });
});
