// streamCore 的 tool.end 配对回归。
//
// 2026-11-15 修复「工具结果错位 + 执行中气泡卡死」。旧实现：
//   blocks.map(…) + !found && (id ? b.id===id : name ? b.name===name : b.pending)
// 三个缺陷（都实测复现过）：
//   ① name 兜底**不要求 pending**，且 map 从头扫取第一个 → 命中最早那个同名块。
//      服务端 BLOCK_PREFIX 分支曾漏发 id，于是被拦工具的 denial 文案覆盖到前面
//      已完成的气泡上，真正被拦的永远停在「执行命令中…」。
//   ② 空结果走 filter **全删** → name-only 时把一个 turn 里所有同名块删光（2 → 0）。
//   ③ 无任何匹配时 map 原样返回 → 结果**静默丢弃**（user.question 分支有 append
//      兜底，tool.end 没有）。
//
// 触发条件：一个 turn 里同名工具被调用多次（bash 重试最常见）+ 其中一次被
// policy/hook/权限拦截。服务端已同步补 id（engine.py BLOCK_PREFIX 分支），
// 这里的测试同时覆盖「服务端仍漏 id」的退化路径——防御必须独立成立。

import { describe, expect, it } from "vitest";
import { applyBlock } from "@/components/chat/streamCore";
import type { Block } from "@/components/chat/blocks";

type ToolB = Extract<Block, { kind: "tool" }>;
const tools = (bs: Block[]) => bs.filter((b): b is ToolB => b.kind === "tool");
const byId = (bs: Block[], id: string) => tools(bs).find((b) => b.id === id);

/** 在 base 之上追加一个已完成的工具块（base 缺省为空数组）。
 *  ⚠️ 必须接 base：早先版本每次都从 [] 起，连续调用会把前一个块丢掉，
 *  让「只删一个」的断言看起来像修复失效——其实是测试自己搭错了场景。 */
function done(id: string, name: string, content: string, base: Block[] = []): Block[] {
  const bs = applyBlock(base, { event: "tool.start", id, name });
  return applyBlock(bs, { event: "tool.end", id, content });
}
/** 再起一个仍在跑的工具块 */
function pending(bs: Block[], id: string, name: string): Block[] {
  return applyBlock(bs, { event: "tool.start", id, name });
}

describe("tool.end 配对：有 id 走精确匹配", () => {
  it("落到同 id 的块上，其它同名块不受影响", () => {
    let bs = done("A", "bash", "第一次的输出");
    bs = pending(bs, "B", "bash");
    bs = applyBlock(bs, { event: "tool.end", id: "B", content: "第二次的输出" });
    expect(byId(bs, "A")?.content).toBe("第一次的输出");
    expect(byId(bs, "A")?.pending).toBe(false);
    expect(byId(bs, "B")?.content).toBe("第二次的输出");
    expect(byId(bs, "B")?.pending).toBe(false);
  });

  it("重放同一 id 幂等（不新增块、内容一致）", () => {
    let bs = pending([], "A", "bash");
    bs = applyBlock(bs, { event: "tool.end", id: "A", content: "结果" });
    const n = tools(bs).length;
    bs = applyBlock(bs, { event: "tool.end", id: "A", content: "结果" });
    expect(tools(bs).length).toBe(n);
    expect(byId(bs, "A")?.content).toBe("结果");
  });

  it("有 id 时即使目标已完成也精确命中（不因 pending 过滤而误追加）", () => {
    let bs = done("A", "bash", "旧");
    bs = applyBlock(bs, { event: "tool.end", id: "A", content: "新" });
    expect(tools(bs).length).toBe(1);
    expect(byId(bs, "A")?.content).toBe("新");
  });
});

describe("tool.end 配对：无 id 的退化路径（缺陷①）", () => {
  it("denial 落到**正在跑的**那个同名块，前面已完成的内容不被改写", () => {
    let bs = done("A", "bash", "== ab1e349\n== 436faed");
    bs = pending(bs, "B", "bash");
    // 服务端 BLOCK_PREFIX 旧形态：只有 name，没有 id
    bs = applyBlock(bs, { event: "tool.end", name: "bash", content: "policy denied" });
    expect(byId(bs, "A")?.content).toBe("== ab1e349\n== 436faed"); // ← 旧实现这里被改写
    expect(byId(bs, "A")?.pending).toBe(false);
    expect(byId(bs, "B")?.content).toBe("policy denied");
    expect(byId(bs, "B")?.pending).toBe(false); // ← 旧实现这里永远 true（卡死）
  });

  it("多个 pending 同名块时取**最近**的一个（从后往前）", () => {
    let bs = pending([], "A", "bash");
    bs = pending(bs, "B", "bash");
    bs = pending(bs, "C", "bash");
    bs = applyBlock(bs, { event: "tool.end", name: "bash", content: "denied" });
    expect(byId(bs, "A")?.pending).toBe(true);
    expect(byId(bs, "B")?.pending).toBe(true);
    expect(byId(bs, "C")?.pending).toBe(false);
    expect(byId(bs, "C")?.content).toBe("denied");
  });

  it("没有 pending 同名块时**追加**，绝不覆盖已完成的历史气泡", () => {
    let bs = done("A", "bash", "历史输出");
    bs = applyBlock(bs, { event: "tool.end", name: "bash", content: "denied" });
    expect(byId(bs, "A")?.content).toBe("历史输出");
    expect(tools(bs).length).toBe(2);
    expect(tools(bs)[1].content).toBe("denied");
    expect(tools(bs)[1].pending).toBe(false);
  });

  it("名字不匹配任何 pending 块时不误伤同名的已完成块", () => {
    let bs = done("A", "bash", "历史");
    bs = pending(bs, "B", "grep_files");
    bs = applyBlock(bs, { event: "tool.end", name: "bash", content: "denied" });
    expect(byId(bs, "A")?.content).toBe("历史");
    expect(byId(bs, "B")?.pending).toBe(true); // 别的工具不受影响
    expect(tools(bs).length).toBe(3);
  });

  it("既无 id 也无 name → 落到最近的 pending 块", () => {
    let bs = pending([], "A", "bash");
    bs = pending(bs, "B", "read_file");
    bs = applyBlock(bs, { event: "tool.end", content: "x" });
    expect(byId(bs, "A")?.pending).toBe(true);
    expect(byId(bs, "B")?.pending).toBe(false);
  });
});

describe("tool.end 空结果：只删目标块（缺陷②）", () => {
  it("name-only 时只删那一个 pending，不把同名块全删光", () => {
    let bs = done("A", "grep_files", "file1.ts:3:hit");
    bs = done("B", "grep_files", "file2.ts:9:hit", bs);
    bs = pending(bs, "C", "grep_files");
    bs = applyBlock(bs, { event: "tool.end", name: "grep_files", content: "(no matches)" });
    expect(tools(bs).length).toBe(2); // ← 旧实现这里变 0
    expect(byId(bs, "A")?.content).toBe("file1.ts:3:hit");
    expect(byId(bs, "B")?.content).toBe("file2.ts:9:hit");
    expect(byId(bs, "C")).toBeUndefined();
  });

  it("有 id 时只删同 id 的那个", () => {
    let bs = done("A", "grep_files", "hit");
    bs = pending(bs, "B", "grep_files");
    bs = applyBlock(bs, { event: "tool.end", id: "B", content: "no results" });
    expect(tools(bs).length).toBe(1);
    expect(byId(bs, "A")).toBeDefined();
  });

  it("空结果但无可配对块 → 原样返回，不凭空追加", () => {
    const bs = done("A", "bash", "历史");
    const after = applyBlock(bs, { event: "tool.end", name: "bash", content: "(empty)" });
    expect(tools(after).length).toBe(1);
    expect(byId(after, "A")?.content).toBe("历史");
  });
});

describe("tool.end 无匹配：追加而非静默丢弃（缺陷③）", () => {
  it("数组里没有同名块时结果仍然落地", () => {
    const bs: Block[] = [{ kind: "text", text: "hello" }];
    const after = applyBlock(bs, { event: "tool.end", name: "bash", content: "policy denied" });
    expect(after.length).toBe(2); // ← 旧实现这里仍是 1，结果消失
    expect(tools(after)[0]).toMatchObject({
      kind: "tool",
      name: "bash",
      content: "policy denied",
      pending: false,
    });
  });

  it("空数组 + 只有 id 也能落地（tool.start 丢帧的场景）", () => {
    const after = applyBlock([], { event: "tool.end", id: "X", content: "结果" });
    expect(tools(after).length).toBe(1);
    expect(tools(after)[0]).toMatchObject({ id: "X", content: "结果", pending: false });
  });

  it("既无 id 也无 name 且无 pending → 追加通用块，不抛异常", () => {
    const after = applyBlock([], { event: "tool.end", content: "结果" });
    expect(tools(after).length).toBe(1);
    expect(tools(after)[0].pending).toBe(false);
  });
});

describe("tool.end 与 question 卡的优先级不受本次改动影响", () => {
  it("ask_user 的 tool.end 仍然折进卡片而不是工具块", () => {
    let bs: Block[] = [];
    bs = applyBlock(bs, { event: "tool.start", id: "Q", name: "ask_user" });
    bs = applyBlock(bs, {
      event: "user.question",
      id: "Q",
      question: "选哪个？",
      options: ["A", "B"],
    });
    bs = applyBlock(bs, {
      event: "tool.end",
      id: "Q",
      content: JSON.stringify({ ok: true, source: "option", option_index: 1, answer: "B" }),
    });
    expect(tools(bs).length).toBe(0);
    const q = bs.find((b) => b.kind === "question");
    expect(q).toBeDefined();
    expect((q as Extract<Block, { kind: "question" }>).status).toBe("answered");
  });
});
