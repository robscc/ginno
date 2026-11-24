import { describe, expect, it } from "vitest";
import { processGroups, processStatus, toolFailed } from "./processGroups";
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
      count: 3, thinking: false, compact: true,
    });
    expect(processStatus([summary, tool("read_file")], false, true).compact).toBe(false);
  });
});
