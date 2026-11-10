import { describe, expect, it, beforeEach } from "vitest";

import {
  buildFindIndex,
  extractSearchableText,
  resetFindCache,
  searchFindIndex,
} from "./sessionFind";

describe("sessionFind（find-bar-design §3）", () => {
  beforeEach(() => resetFindCache());

  it("提取表：各 kind 入/不入索引", () => {
    const texts = extractSearchableText([
      { kind: "text", text: "正文" },
      { kind: "thinking", text: "推理" },
      { kind: "tool", name: "bash", argsPreview: "ls", content: "out" },
      { kind: "tool", name: "bash", content: "…", pending: true }, // pending 不搜
      { kind: "steer", text: "插话" },
      { kind: "skill", name: "todo", text: "清单" },
      { kind: "context", text: "系统行" },
      { kind: "file", name: "报告.xlsx" },
      { kind: "image", url: "http://x" }, // 不入
      { kind: "turn_meta" }, // 不入
    ]);
    expect(texts).toEqual([
      "正文",
      "推理",
      "bash\nls\nout",
      "插话",
      "/todo 清单",
      "系统行",
      "报告.xlsx",
    ]);
  });

  it("question 块含题干/选项/答案", () => {
    const [t] = extractSearchableText([
      { kind: "question", question: "选哪个", options: ["A", "B"], answer: "A" },
    ]);
    expect(t).toContain("选哪个");
    expect(t).toContain("A");
  });

  it("索引跨消息、blockIdx 只数可搜块", () => {
    const idx = buildFindIndex(
      [
        { id: "m1", blocks: [{ kind: "text", text: "甲" }, { kind: "image", url: "x" }, { kind: "text", text: "乙" }] },
        { id: "m2", blocks: [{ kind: "tool", name: "bash", content: "boom" }] },
      ],
      null,
    );
    expect(idx.map((e) => `${e.msgId}:${e.blockIdx}`)).toEqual(["m1:0", "m1:1", "m2:0"]);
  });

  it("搜索：大小写不敏感、多命中有序、空串零命中", () => {
    const idx = buildFindIndex(
      [
        { id: "m1", blocks: [{ kind: "text", text: "Deploy failed. deploy again" }] },
        { id: "m2", blocks: [{ kind: "text", text: "ok" }] },
      ],
      null,
    );
    const hits = searchFindIndex(idx, "DEPLOY");
    expect(hits).toHaveLength(2);
    expect(hits[0]).toMatchObject({ msgId: "m1", blockIdx: 0, start: 0, len: 6 });
    expect(hits[1].start).toBeGreaterThan(0);
    expect(searchFindIndex(idx, "")).toEqual([]);
    expect(searchFindIndex(idx, "   ")).toEqual([]);
  });

  it("流式标记：liveId 命中标记 streaming", () => {
    const idx = buildFindIndex(
      [{ id: "live", blocks: [{ kind: "text", text: "增长中" }] }],
      "live",
    );
    expect(idx[0].streaming).toBe(true);
  });

  it("版本缓存：blocks 引用不变命中缓存，变则重提取", () => {
    const blocks = [{ kind: "text", text: "v1" }];
    buildFindIndex([{ id: "m", blocks }], null);
    const again = buildFindIndex([{ id: "m", blocks }], null);
    expect(again[0].text).toBe("v1");
    const blocks2 = [{ kind: "text", text: "v2 长文本" }];
    const fresh = buildFindIndex([{ id: "m", blocks: blocks2 }], null);
    expect(fresh[0].text).toBe("v2 长文本");
  });
});
