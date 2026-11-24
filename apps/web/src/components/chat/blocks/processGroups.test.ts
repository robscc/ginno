import { describe, expect, it } from "vitest";
import { processGroups, processStatus, toolFailed } from "./processGroups";
import { reportAttention, type ReportBlock } from "./reportFold";
import type { Block } from "./types";

const tool = (name: string, options: Partial<Extract<Block, { kind: "tool" }>> = {}): Block =>
  ({ kind: "tool", name, pending: false, content: "OK", ...options });
const summary: Block = { kind: "reasoning_summary", requestId: "r1", parts: [] };
const llm: Block = { kind: "llm_call", reqId: "r2", attempt: 1, pending: false };

describe("process groups", () => {
  it("merges consecutive model requests without moving prose, files or interactive records", () => {
    const blocks: Block[] = [
      summary, tool("read_file"), llm, { ...summary, requestId: "r2" }, tool("edit_file"),
      { kind: "text", text: "Progress" }, tool("bash"), tool("browser_handoff"),
      tool("bg_run"), tool("mcp_ui"), tool("read_file"), { kind: "file", name: "demo.html" },
      tool("bash"), { kind: "question", question: "Continue?", options: [], allowFreeText: true, status: "pending" },
    ];
    expect(processGroups(blocks, new Set(["mcp_ui"]))).toEqual([[0, 1, 3, 4], [6], [10], [12]]);
  });

  it("keeps failed and interrupted tools independently visible", () => {
    const blocks = [tool("read_file"), tool("bash", { content: "[exit 2]\nfailed" }),
      tool("read_file", { failed: true }), tool("bash", { content: "(interrupted)" }), tool("edit_file")];
    expect(processGroups(blocks, new Set())).toEqual([[0], [4]]);
    expect(toolFailed("[error] missing file")).toBe(true);
    expect(toolFailed("[DEBUG]\nError keyword in ordinary text")).toBe(false);
  });

  it("counts completed real calls, including identical names, separately from thinking", () => {
    const pending = tool("bash", { pending: true });
    const blocks = [summary, tool("read_file"), tool("read_file"), tool("edit_file"), pending, pending];
    expect(processStatus(blocks, true, true)).toEqual({
      completed: { read: 2, edit: 1, command: 0, other: 0 }, pending: [pending, pending],
      count: 3, thinking: false, compact: true, reports: 0, planUpdates: 0, plan: null,
    });
    expect(processStatus([summary, tool("read_file")], false, true).compact).toBe(false);
  });

  it("紧邻的回报与工具步骤合成一组，单个回报也成组", () => {
    const report: Block = { kind: "subagent_result", sessionId: "a", summary: "ok" };
    const plan: Block = { kind: "plan_update", planId: "pl_1", text: "- [done] it_1 ok" };
    const none = () => null;
    expect(processGroups([tool("read_file"), report, plan], new Set(), none)).toEqual([[0, 1, 2]]);
    expect(processGroups([{ kind: "text", text: "等待结果" }, report], new Set(), none)).toEqual([[1]]);
    expect(processGroups([report], new Set(), none)).toEqual([[0]]);
    const status = processStatus([report, plan], false, false);
    expect(status).toMatchObject({ reports: 1, planUpdates: 1, compact: true, count: 0 });
    expect(processStatus([{ kind: "text", text: "x" }, tool("bash")], false, false).compact).toBe(false);
  });

  it("失败、停止、等待回答与出错的回报绝不进折叠组（唯一红线）", () => {
    type Result = Extract<Block, { kind: "subagent_result" }>;
    const report = (sessionId: string, extra: Partial<Result> = {}): Block =>
      ({ kind: "subagent_result", sessionId, summary: "ok", ...extra });
    const plan = (text: string): Block => ({ kind: "plan_update", planId: "pl_1", text });
    const child: Record<string, string | undefined> = {};
    const attention = (b: Block) =>
      reportAttention(b as ReportBlock, b.kind === "subagent_result" ? child[(b as Result).sessionId] : undefined);
    const blocks = [
      tool("read_file"), report("ok"), report("err", { error: "boom" }), report("kid"),
      plan("- [failed] it_1 x"), plan("- [done] it_2 ok"), tool("edit_file"),
    ];
    for (const status of ["waiting", "failed", "stopped"]) {
      child.kid = status;
      expect(processGroups(blocks, new Set(), attention)).toEqual([[0, 1], [5, 6]]);
    }
    // Only the child-status signal changes: the error text and the failed plan
    // note keep breaking the group on their own.
    child.kid = "done";
    expect(processGroups(blocks, new Set(), attention)).toEqual([[0, 1], [3], [5, 6]]);
    expect(processGroups(blocks, new Set())).toEqual([[0, 1], [3], [5, 6]]);
  });
});
