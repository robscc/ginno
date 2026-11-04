import { describe, expect, it } from "vitest";
import { innerTail, isWaitingNextRound } from "./waitState";
import type { Block } from "./blocks/types";

// 最小块工厂：只填判据关心的字段（判据只读 kind/pending/status）。
const tool = (pending: boolean): Block =>
  ({ kind: "tool", name: "bash", content: pending ? "" : "ok", pending }) as Block;
const text = (t = "hi"): Block => ({ kind: "text", text: t }) as Block;
const question = (status: "pending" | "answered" | "skipped"): Block =>
  ({ kind: "question", question: "q?", status }) as Block;

describe("innerTail", () => {
  it("跳过尾部 ref 块取最后一个内层块", () => {
    const blocks = [text("a"), tool(false), { kind: "ref", refKind: "file", name: "f" } as Block];
    expect(innerTail(blocks)?.kind).toBe("tool");
  });

  it("全 ref 块返回 undefined", () => {
    expect(innerTail([{ kind: "ref", refKind: "file", name: "f" } as Block])).toBeUndefined();
  });

  it("空数组返回 undefined", () => {
    expect(innerTail([])).toBeUndefined();
  });

  it("跳过尾部 turn_meta 块取最后一个内容块（chrome 不占流推进位）", () => {
    const blocks = [
      text("a"),
      tool(false),
      { kind: "turn_meta", t0Ms: 1 } as Block,
    ];
    expect(innerTail(blocks)?.kind).toBe("tool");
  });

  it("仅 turn_meta（TTFT）返回 undefined", () => {
    expect(innerTail([{ kind: "turn_meta", t0Ms: 1 } as Block])).toBeUndefined();
  });
});

describe("isWaitingNextRound", () => {
  it("尾块为已结束的工具 → 等待（用户反馈的场景：tool.end 后静默窗）", () => {
    expect(isWaitingNextRound([text("前言"), tool(false)], true, false)).toBe(true);
  });

  it("尾块为运行中的工具 → 不等待（spinner 自己在转）", () => {
    expect(isWaitingNextRound([tool(true)], true, false)).toBe(false);
  });

  it("尾块为正文/思考流 → 不等待（光标/脉冲已是活性信号）", () => {
    expect(isWaitingNextRound([tool(false), text("回答中")], true, false)).toBe(false);
  });

  it("尾块为 pending 问题卡 → 不等待（等的是用户）", () => {
    expect(isWaitingNextRound([tool(false), question("pending")], true, false)).toBe(false);
  });

  it("尾块为已作答的问题 → 等待（resume 后模型被重新调用）", () => {
    expect(isWaitingNextRound([question("answered")], true, false)).toBe(true);
    expect(isWaitingNextRound([question("skipped")], true, false)).toBe(true);
  });

  it("尾块为已注入的 steer 带 → 等待（吸收后模型重新生成）", () => {
    const band = { kind: "steer", text: "补充", steerId: "s1", injectedAt: 0 } as Block;
    expect(isWaitingNextRound([tool(false), band], true, false)).toBe(true);
  });

  it("非流式（历史气泡）→ 永不等待", () => {
    expect(isWaitingNextRound([tool(false)], false, false)).toBe(false);
    expect(isWaitingNextRound([tool(false)], undefined, undefined)).toBe(false);
  });

  it("停车中（权限/提案停靠卡）→ 不等待，避免秒表空转误导", () => {
    expect(isWaitingNextRound([tool(false)], true, true)).toBe(false);
  });

  it("空气泡（无内层块）→ 不等待（由 TTFT 状态行负责）", () => {
    expect(isWaitingNextRound([{ kind: "ref", refKind: "file", name: "f" } as Block], true, false)).toBe(false);
  });

  it("ref 块不掩盖等待态：工具结束后来的 ref.emit 不熄灭等待行", () => {
    const blocks = [tool(false), { kind: "ref", refKind: "link", name: "l" } as Block];
    expect(isWaitingNextRound(blocks, true, false)).toBe(true);
  });

  it("turn_meta 块不掩盖等待态：usage 追加的小字排在尾部也不熄灭等待行", () => {
    const blocks = [text("前言"), tool(false), { kind: "turn_meta", t0Ms: 1 } as Block];
    expect(isWaitingNextRound(blocks, true, false)).toBe(true);
  });
});
