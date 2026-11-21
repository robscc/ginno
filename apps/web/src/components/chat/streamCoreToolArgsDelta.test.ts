// tool.args_delta（2026-11-21 流式参数预览）reducer 契约。
//
// 背景：大 JSON tool args 生成期可达数十秒，气泡只有 "…" 干等（实测
// 94s 零反馈）。服务端新增 tool.args_delta——累计尾部快照（替换式，
// 非 chunk 拼接），终态 tool.args 到达后清空回 argsPreview。
// 这里钉住三件事：① delta 只写入尚无终态 preview 的块（!argsPreview
// 门控）；② 终态 tool.args 一次性替换并清 argsStream；③ 未知 id /
// 空 tail 静默忽略（重连乱序不炸）。

import { describe, expect, it } from "vitest";
import { applyBlock } from "@/components/chat/streamCore";
import type { Block } from "@/components/chat/blocks";

type ToolB = Extract<Block, { kind: "tool" }>;
const tools = (bs: Block[]) => bs.filter((b): b is ToolB => b.kind === "tool");

describe("tool.args_delta", () => {
  const base: Block[] = applyBlock([], { event: "tool.start", id: "t1", name: "bash" });

  it("首次 delta 写入 argsStream（替换式）", () => {
    const bs = applyBlock(base, { event: "tool.args_delta", id: "t1", tail: '{"comm', total: 6 });
    expect(tools(bs)[0].argsStream).toBe('{"comm');
    // 第二段快照：直接替换，不拼接
    const bs2 = applyBlock(bs, { event: "tool.args_delta", id: "t1", tail: '"command": "ls', total: 20 });
    expect(tools(bs2)[0].argsStream).toBe('"command": "ls');
  });

  it("终态 tool.args 替换 preview 并清空 argsStream", () => {
    const mid = applyBlock(base, { event: "tool.args_delta", id: "t1", tail: '{"comm', total: 6 });
    const done = applyBlock(mid, { event: "tool.args", id: "t1", preview: "ls -la /tmp" });
    const t = tools(done)[0];
    expect(t.argsPreview).toBe("ls -la /tmp");
    expect(t.argsStream).toBeUndefined();
    // 终态后迟到的 delta 不得回写（!argsPreview 门控）
    const late = applyBlock(done, { event: "tool.args_delta", id: "t1", tail: "stale", total: 99 });
    expect(tools(late)[0].argsPreview).toBe("ls -la /tmp");
    expect(tools(late)[0].argsStream).toBeUndefined();
  });

  it("未知 id / 空 tail 静默忽略", () => {
    // map 返回新数组（引用不同），契约是「内容与原状逐字段一致」而非引用同一。
    const a = applyBlock(base, { event: "tool.args_delta", id: "nope", tail: "x", total: 1 });
    expect(a).toStrictEqual(base);
    const b = applyBlock(base, { event: "tool.args_delta", id: "t1", tail: "", total: 0 });
    expect(b).toStrictEqual(base);
    expect(tools(b)[0].argsStream).toBeUndefined();
  });
});
