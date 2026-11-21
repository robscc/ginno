// DiffView 纯函数契约（2026-11-21 编辑 diff 显示）。
// parseDiffLines：unified 文本 → 行模型（双列行号、hunk 头、+/-/ctx）；
// foldContextSpans：连续 ≥8 ctx 折叠为占位（段首尾各留 2 行）。

import { describe, expect, it } from "vitest";
import { foldContextSpans, parseDiffLines } from "@/components/chat/blocks/diffView";

const SAMPLE = `--- a/x.py
+++ b/x.py
@@ -1,12 +1,12 @@
 l1
 l2
-l3
+l3
 l4
 l5
 l6
 l7
 l8
 l9
 l10
 l11
 l12
@@ -20,2 +20,2 @@
 a
-b
+c`;

describe("parseDiffLines", () => {
  it("解析 hunk/行号/增删", () => {
    const ls = parseDiffLines(SAMPLE);
    expect(ls[0].t).toBe("hunk");
    const del = ls.find((l) => l.t === "del" && l.text === "l3");
    const add = ls.find((l) => l.t === "add" && l.text === "l3");
    expect((del as { ln1: number }).ln1).toBe(3);
    expect((add as { ln2: number }).ln2).toBe(3);
    const ctx1 = ls.find((l) => l.t === "ctx" && l.text === "l1") as { ln1: number; ln2: number };
    expect(ctx1.ln1).toBe(1);
    expect(ctx1.ln2).toBe(1);
  });

  it("非 diff 垃圾输入不炸（按 ctx 容错）", () => {
    const ls = parseDiffLines("random\ntext");
    expect(ls).toHaveLength(0); // 未进 hunk 的行被忽略
  });
});

describe("foldContextSpans", () => {
  it("长上下文折叠、短上下文保留", () => {
    const ls = parseDiffLines(SAMPLE);
    const spans = foldContextSpans(ls);
    const fold = spans.find((s) => s.kind === "fold") as { kind: "fold"; n: number };
    // 第一个 hunk 的 ctx 被 l3 的 -/+ 打断：l4..l12 是 9 行连续段 → 9-4=5 折叠
    expect(fold).toBeDefined();
    expect(fold.n).toBe(5);
    // 全部行无丢失（折叠行保留在 items 里待展开）
    const total = spans.reduce((acc, s) => acc + s.items.length, 0);
    expect(total).toBe(ls.length);
  });

  it("无长上下文时不产生折叠", () => {
    const ls = parseDiffLines("--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n");
    expect(foldContextSpans(ls).some((s) => s.kind === "fold")).toBe(false);
  });
});
