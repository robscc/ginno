import { describe, expect, it } from "vitest";
import type { Block } from "./types";
import type { ChatMsg } from "../streamCore";
import {
  foldReportRows,
  isReportMessage,
  isReportRow,
  reportPreview,
  planUpdateNeedsAttention,
  reportNeedsAttention,
  resultNeedsAttention,
} from "./reportFold";

type ResultBlock = Extract<Block, { kind: "subagent_result" }>;
type PlanBlock = Extract<Block, { kind: "plan_update" }>;

const say = (id: string, role: ChatMsg["role"], text = "…"): ChatMsg => ({
  id, role, blocks: [{ kind: "text", text }],
});
const toolStep = (id: string): ChatMsg => ({
  id, role: "assistant", blocks: [{ kind: "tool", name: "read_file", pending: false, content: "OK" }],
});
const result = (id: string, sessionId: string, role: ChatMsg["role"] = "system", extra: Partial<ResultBlock> = {}): ChatMsg => ({
  id, role, blocks: [{ kind: "subagent_result", sessionId, summary: "全部通过", ...extra }],
});
const plan = (id: string, body: string, role: ChatMsg["role"] = "user"): ChatMsg => ({
  id, role, blocks: [{ kind: "plan_update", planId: "pl_1", text: body }],
});
const resultBlock = (m: ChatMsg) => m.blocks[0] as ResultBlock;
const planBlock = (m: ChatMsg) => m.blocks[0] as PlanBlock;

describe("回报的「需要你处理」判定", () => {
  const ok: ResultBlock = { kind: "subagent_result", sessionId: "a", summary: "fine" };

  it("出错文本、失败/停止/等待回答的子会话、error/timeout 的委托都算", () => {
    expect(resultNeedsAttention({ ...ok, error: "boom" })).toBe(true);
    expect(resultNeedsAttention({ ...ok, error: "   " }, "done")).toBe(false);
    for (const status of ["failed", "stopped", "waiting"]) {
      expect(resultNeedsAttention(ok, status)).toBe(true);
    }
    for (const status of ["done", "running", undefined]) {
      expect(resultNeedsAttention(ok, status)).toBe(false);
    }
    expect(resultNeedsAttention({ ...ok, summary: "[delegate backend=codex mode=edit stop=error]" })).toBe(true);
    expect(resultNeedsAttention({ ...ok, summary: "[delegate backend=codex mode=edit stop=timeout]" })).toBe(true);
    expect(resultNeedsAttention({ ...ok, summary: "[delegate backend=codex mode=edit stop=success]" })).toBe(false);
  });

  it("清单更新里出现失败或停止的条目就算", () => {
    const withItems = (text: string): PlanBlock => ({ kind: "plan_update", planId: "pl_1", text });
    expect(planUpdateNeedsAttention(withItems("- [done] it_1 ok\n- [failed] it_2 boom"))).toBe(true);
    expect(planUpdateNeedsAttention(withItems("- [stopped] it_2 killed"))).toBe(true);
    expect(planUpdateNeedsAttention(withItems("- [done] it_1 ok"))).toBe(false);
  });

  it("reportNeedsAttention 按块类型分派", () => {
    expect(reportNeedsAttention({ ...ok, error: "x" })).toBe(true);
    expect(reportNeedsAttention(ok, "waiting")).toBe(true);
    expect(reportNeedsAttention(planBlock(plan("p", "- [failed] it_1 x")))).toBe(true);
    expect(reportNeedsAttention(ok)).toBe(false);
  });
});

describe("isReportMessage", () => {
  it("只认「全是回报块」的消息（含没有正文的助手唤醒回合）", () => {
    const empty: ChatMsg = { id: "m", role: "system", blocks: [] };
    const mixed: ChatMsg = {
      id: "m", role: "user",
      blocks: [{ kind: "task_event", taskId: "t", ev: "finished" }, planBlock(plan("p", "x"))],
    };
    expect(isReportMessage(result("r", "a"))).toBe(true);
    expect(isReportMessage(plan("p", "- [done] it_1 x"))).toBe(true);
    expect(isReportMessage(result("r", "a", "assistant"))).toBe(true);
    expect(isReportMessage(say("a", "assistant"))).toBe(false);
    expect(isReportMessage(empty)).toBe(false);
    expect(isReportMessage(mixed)).toBe(false);
  });
});

describe("foldReportRows（回报并入前一条助手消息的尾部）", () => {
  const planTotal = () => 3;

  it("实时 system 行与回放 user 行同样并入宿主，行序与长度不变", () => {
    const messages = [
      say("u1", "user", "开始"),
      toolStep("a1"),
      result("r1", "child-ok", "system"),
      plan("p1", "- [done] it_1 ok", "user"),
      say("a2", "assistant", "收到，继续"),
    ];
    const { rows, absorbed } = foldReportRows(messages, { attention: () => null, planTotal });
    expect(rows.map((m) => m.id)).toEqual(["u1", "a1", "r1", "p1", "a2"]);
    expect([...absorbed]).toEqual([2, 3]);
    expect(rows[1].blocks.map((b) => b.kind)).toEqual(["tool", "subagent_result", "plan_update"]);
    expect(rows[0]).toBe(messages[0]);
    expect(rows[4]).toBe(messages[4]);
    expect(messages[1].blocks).toHaveLength(1);
  });

  it("没有可挂靠的助手消息时，整串回报留在首行", () => {
    const afterUser = foldReportRows([
      say("u1", "user"), result("r1", "a"), plan("p1", "- [done] it_1 x"), say("a1", "assistant"),
    ], { attention: () => null });
    expect(afterUser.rows[1].blocks.map((b) => b.kind)).toEqual(["subagent_result", "plan_update"]);
    expect([...afterUser.absorbed]).toEqual([2]);

    const errorHost = foldReportRows([
      { ...say("a1", "assistant"), error: true }, result("r1", "a"),
    ], { attention: () => null });
    expect(errorHost.rows[1].blocks).toHaveLength(1);
    expect(errorHost.absorbed.size).toBe(0);
  });

  it("清单进度按时间线累计并盖在清单块上", () => {
    const { rows, absorbed } = foldReportRows([
      toolStep("a0"), plan("p1", "- [done] it_1 ok"),
      say("a1", "assistant"), plan("p2", "- [done] it_2 ok\n- [failed] it_3 boom"),
      say("a2", "assistant"), plan("p3", "- [done] it_4 ok"),
    ], { attention: () => null, planTotal });
    // Each plan note lands right after the assistant message in front of it.
    const progressAt = (index: number) => (rows[index].blocks[1] as PlanBlock).progress;
    expect(progressAt(0)).toEqual({ planId: "pl_1", done: 1, failed: 0, stopped: 0, total: 3 });
    expect(progressAt(2)).toEqual({ planId: "pl_1", done: 2, failed: 1, stopped: 0, total: 3 });
    expect(progressAt(4)).toEqual({ planId: "pl_1", done: 3, failed: 1, stopped: 0, total: 3 });
    expect([...absorbed]).toEqual([1, 3, 5]);
  });

  it("混合消息不是回报串：保持独立，后续回报照常并入", () => {
    const mixed: ChatMsg = {
      id: "mix", role: "user",
      blocks: [{ kind: "task_event", taskId: "t", ev: "finished" }, planBlock(plan("x", "- [done] it_1 ok"))],
    };
    // A run right after a non-assistant row has no host: it stays a standalone row.
    const afterMixed = foldReportRows([toolStep("a1"), mixed, result("r1", "a")], { attention: () => null });
    expect(afterMixed.rows[1]).toBe(mixed);
    expect(afterMixed.absorbed.size).toBe(0);
    expect(afterMixed.rows[2].blocks).toHaveLength(1);

    // Once an assistant message follows, later runs attach to it as usual.
    const { rows, absorbed } = foldReportRows([mixed, toolStep("a1"), result("r1", "a")], { attention: () => null });
    expect(rows[0]).toBe(mixed);
    expect([...absorbed]).toEqual([2]);
    expect(rows[1].blocks.map((b) => b.kind)).toEqual(["tool", "subagent_result"]);
  });

  it("reportPreview：收起态的两行预览是可读文字，不是 Markdown 原文", () => {
    const report = [
      "## 证据",
      "",
      "1. `node --check app.js` 通过，**58 项**全部通过。",
      "- [x] [日志](/tmp/run.log)",
      "```bash",
      "pytest -q",
      "```",
      "| a | b |",
      "| --- | --- |",
      "| 1 | 2 |",
    ].join("\n");
    expect(reportPreview(report)).toBe(
      "证据 1. node --check app.js 通过，58 项全部通过。 - [x] 日志 … | a | b | | 1 | 2 |",
    );
    expect(reportPreview("")).toBe("");
  });

  it("回放把回报放在下一条助手消息的开头：摘下来并到上一条助手消息，与实时同位", () => {
    const head: ChatMsg = {
      id: "a2", role: "assistant",
      blocks: [resultBlock(result("r", "a")), { kind: "text", text: "收到，继续" }],
    };
    const hosted = foldReportRows([toolStep("a1"), head], { attention: () => null });
    expect(hosted.rows[0].blocks.map((b) => b.kind)).toEqual(["tool", "subagent_result"]);
    expect(hosted.rows[1].blocks.map((b) => b.kind)).toEqual(["text"]);
    expect(hosted.absorbed.size).toBe(0);

    // No assistant in front: the reports stay at the bubble head (same fold,
    // one level up) instead of vanishing into a host that does not exist.
    const unhosted = foldReportRows([say("u1", "user"), head], { attention: () => null });
    expect(unhosted.rows[1].blocks.map((b) => b.kind)).toEqual(["subagent_result", "text"]);
    expect(unhosted.absorbed.size).toBe(0);
    expect(unhosted.rows[1]).toBe(head);

    // A report row directly before that bubble joins the detached head.
    const joined = foldReportRows([say("u1", "user"), result("r0", "b"), head], { attention: () => null });
    expect(joined.rows[1].blocks.map((b) => b.kind)).toEqual(["subagent_result", "subagent_result"]);
    expect(joined.rows[2].blocks.map((b) => b.kind)).toEqual(["text"]);
    expect(joined.absorbed.size).toBe(0);
  });

  it("整条助手消息只有回报块时按回报串处理，不留下空气泡", () => {
    const only: ChatMsg = { id: "a2", role: "assistant", blocks: [resultBlock(result("r", "a"))] };
    const { rows, absorbed } = foldReportRows([toolStep("a1"), only], { attention: () => null });
    expect(rows[0].blocks.map((b) => b.kind)).toEqual(["tool", "subagent_result"]);
    expect([...absorbed]).toEqual([1]);
  });

  it("id 为空的回放行按索引登记，不会误伤其它无 id 的行", () => {
    const { rows, absorbed } = foldReportRows([
      toolStep("a1"), { id: null as unknown as string, role: "user", blocks: [resultBlock(result("r", "a"))] },
      { id: null as unknown as string, role: "user", blocks: [{ kind: "text", text: "tail band" }] },
    ], { attention: () => null });
    expect([...absorbed]).toEqual([1]);
    expect(rows[2].blocks).toHaveLength(1);
  });

  it("isReportRow：带回报的系统行（可混有员工回传/任务事件），不含上下文条与正文", () => {
    const worker: Block = { kind: "worker_result", sessionId: "w", summary: "ok" };
    const task: Block = { kind: "task_event", taskId: "t", ev: "finished" };
    const context: Block = { kind: "context", text: "ctx" };
    const row = (role: ChatMsg["role"], blocks: Block[]): ChatMsg => ({ id: "m", role, blocks });
    expect(isReportRow(row("user", [resultBlock(result("r", "a")), worker]))).toBe(true);
    expect(isReportRow(row("system", [task, planBlock(plan("p", "x")), worker]))).toBe(true);
    expect(isReportRow(row("assistant", [resultBlock(result("r", "a"))]))).toBe(false);
    expect(isReportRow(row("user", [task, worker]))).toBe(false);
    expect(isReportRow(row("system", [context, resultBlock(result("r", "a"))]))).toBe(false);
    expect(isReportRow(row("user", [{ kind: "text", text: "hi" }, resultBlock(result("r", "a"))]))).toBe(false);
  });
});
