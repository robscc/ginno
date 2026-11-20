// pinStreamCore — PinStream 接入层单测（重构批次 3-1）。
//
// 钉住的是「pin 用户现在拿到的行为」——pinApplyBlock 直调主版
// streamCore.applyBlock 后，pin 视角的三个修复点：
//   ① tool.end 2026-11-15 定位算法（同名块不再误删/错位/静默丢结果）；
//   ② appendStreamDelta 交叉增量合并（thinking/text 交叉流不再碎块）；
//   ③ watchdog believing-live 周期探针 + turn.state 回答的 stale 守卫。
// 全部纯函数断言（vitest node 环境，无 DOM——渲染层由 packaged UI e2e 覆盖）。

import { describe, expect, it } from "vitest";
import {
  pinApplyBlock,
  pinTurnStateAction,
  pinWatchdogTick,
  PIN_SILENCE_CLOSE_MS,
} from "./pinStreamCore";
import type { Block } from "@/components/chat/blocks";

const tool = (id: string | undefined, name: string, content: string, pending: boolean): Block => ({
  kind: "tool",
  id,
  name,
  content,
  pending,
});

describe("pinApplyBlock · tool.end 定位（2026-11-15 主版算法）", () => {
  // 一个 turn 内同名工具调两次（bash 重试最常见）：第一个已完成、第二个在跑。
  const twoBash: Block[] = [
    tool("t1", "bash", "first output", false),
    tool("t2", "bash", "…", true),
  ];

  it("name-only 非空结果落到最近的 pending 同名块，不污染已完成块（旧算法从头扫会覆盖第一个）", () => {
    const out = pinApplyBlock(twoBash, { event: "tool.end", name: "bash", content: "(denied)" });
    expect(out).toHaveLength(2);
    expect(out[0]).toMatchObject({ kind: "tool", id: "t1", content: "first output", pending: false });
    expect(out[1]).toMatchObject({ kind: "tool", id: "t2", content: "(denied)", pending: false });
  });

  it("name-only 空结果只删目标 pending 块，保留已完成同名块（旧算法 filter 全删，实测 2 → 0）", () => {
    const out = pinApplyBlock(twoBash, { event: "tool.end", name: "bash", content: "(no matches)" });
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ kind: "tool", id: "t1", content: "first output" });
  });

  it("有 id 走精确匹配且不限 pending——重连重放同一 tool.end 幂等", () => {
    const once = pinApplyBlock(twoBash, { event: "tool.end", id: "t2", name: "bash", content: "ok" });
    const twice = pinApplyBlock(once, { event: "tool.end", id: "t2", name: "bash", content: "ok" });
    expect(twice).toEqual(once);
    expect(twice[1]).toMatchObject({ id: "t2", content: "ok", pending: false });
  });

  it("无任何可配对块（tool.start 丢帧）时 append 已完成块，不静默丢结果（旧算法原样返回）", () => {
    const out = pinApplyBlock([], { event: "tool.end", name: "bash", content: "orphan result" });
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ kind: "tool", name: "bash", content: "orphan result", pending: false });
  });
});

describe("pinApplyBlock · delta 合块（appendStreamDelta 交叉合并）", () => {
  it("正文增量到达时末尾若是 thinking，仍并回最近 text 块（旧算法会新建碎片卡）", () => {
    let blocks = pinApplyBlock([], { event: "token.delta", content: "A" });
    blocks = pinApplyBlock(blocks, { event: "thinking.delta", content: "t1" });
    blocks = pinApplyBlock(blocks, { event: "token.delta", content: "B" });
    // 与服务端 AIMessageChunk 按 content-block 索引合并对齐：模型在正文
    // block 上续写，thinking 是另一个 block——实时视图不得把 B 挤成独立卡。
    expect(blocks).toEqual([
      { kind: "text", text: "AB" },
      { kind: "thinking", text: "t1" },
    ]);
  });

  it("对称：thinking 增量到达时末尾若是 text，并回最近 thinking 块", () => {
    let blocks = pinApplyBlock([], { event: "thinking.delta", content: "t1" });
    blocks = pinApplyBlock(blocks, { event: "token.delta", content: "A" });
    blocks = pinApplyBlock(blocks, { event: "thinking.delta", content: "t2" });
    expect(blocks).toEqual([
      { kind: "thinking", text: "t1t2" },
      { kind: "text", text: "A" },
    ]);
  });

  it("工具气泡是合并边界：边界之后的增量绝不并进边界之前的块", () => {
    let blocks = pinApplyBlock([], { event: "token.delta", content: "A" });
    blocks = pinApplyBlock(blocks, { event: "tool.start", id: "t1", name: "bash" });
    blocks = pinApplyBlock(blocks, { event: "token.delta", content: "B" });
    blocks = pinApplyBlock(blocks, { event: "token.delta", content: "C" });
    expect(blocks.map((b) => b.kind)).toEqual(["text", "tool", "text"]);
    expect(blocks[0]).toMatchObject({ text: "A" });
    expect(blocks[2]).toMatchObject({ text: "BC" });
  });
});

describe("pinWatchdogTick · 45s 静默 + believing-live 周期探针（fe0d06e 镜像）", () => {
  const now = 1_000_000;

  it("静默未超时且 believing-live → 只探针，不断线", () => {
    expect(
      pinWatchdogTick({ now, lastSeen: now - 10_000, believingLive: true, socketOpen: true }),
    ).toEqual({ close: false, probe: true });
  });

  it("静默超 45s → 断线换新连接（ping 有应答不算静默，这里 lastSeen 只被入站帧刷新）", () => {
    const out = pinWatchdogTick({
      now,
      lastSeen: now - PIN_SILENCE_CLOSE_MS - 1,
      believingLive: false,
      socketOpen: true,
    });
    expect(out.close).toBe(true);
  });

  it("不 believing-live（无 turn / 已收口）→ 不发探针", () => {
    expect(
      pinWatchdogTick({ now, lastSeen: now, believingLive: false, socketOpen: true }).probe,
    ).toBe(false);
  });

  it("socket 非 OPEN（正在关闭）→ 不发探针", () => {
    expect(
      pinWatchdogTick({ now, lastSeen: now, believingLive: true, socketOpen: false }).probe,
    ).toBe(false);
  });
});

describe("pinTurnStateAction · 探针回答处置（含 sendSeq>probeSeq stale 守卫）", () => {
  it("running=true → 流照常续，no-op", () => {
    expect(pinTurnStateAction({ running: true }, { believingLive: true, sendSeq: 0, probeSeq: 5 })).toBe("resume");
  });

  it("running=false 但本端刚发过新 turn（sendSeq > probeSeq）→ 回答已过时，等流事件自愈", () => {
    // 真实时序：新会话首条消息，socket 由发送打开 → onopen 探针 t0、
    // invoke t0+13ms → 回答 running=false 若被采信会把刚发出的 turn 判死。
    expect(pinTurnStateAction({ running: false }, { believingLive: true, sendSeq: 6, probeSeq: 5 })).toBe("stale");
  });

  it("running=false、不 believing-live → 探针发出后 turn 恰好收口，无事可做", () => {
    expect(pinTurnStateAction({ running: false }, { believingLive: false, sendSeq: 0, probeSeq: 5 })).toBe("idle");
  });

  it("running=false、believing-live、无更新的 send → 从 checkpoint 重建（收尾帧丢失自愈）", () => {
    expect(pinTurnStateAction({ running: false }, { believingLive: true, sendSeq: 5, probeSeq: 5 })).toBe("reconcile");
  });
});
