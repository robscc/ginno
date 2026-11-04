import { describe, expect, it } from "vitest";
import {
  FRONTMATTER_RE,
  parseFrontmatter,
  splitFrontmatter,
} from "./markdownFrontmatter";

const FM = `---
title: 特性雷达 2026-11-04
date: 2026-11-04
tags: [特性雷达, 竞品调研, "本地优先"]
confidence: 中高（来源已核实）
sources:
  - https://github.com/trending?since=daily
  - https://openrig.dev/
nested_ignored:
  key.sub: 不进列表计数（缩进非 "- "）
empty:
---

# 正文标题

内容段落。
`;

describe("parseFrontmatter", () => {
  it("解析标量键（剥引号）", () => {
    const entries = parseFrontmatter("title: my page\nplain: 42\nquoted: \"hello\"");
    expect(entries).toEqual([
      { key: "title", value: "my page" },
      { key: "plain", value: "42" },
      { key: "quoted", value: "hello" },
    ]);
  });

  it("行内数组拆成 items", () => {
    const entries = parseFrontmatter("tags: [a, b, 'c d']");
    expect(entries).toEqual([
      { key: "tags", value: "3", items: ["a", "b", "c d"], list: true },
    ]);
  });

  it("多行 '- ' 列表计条数、非列表子键不计", () => {
    const entries = parseFrontmatter(
      "sources:\n  - https://a\n  - https://b\nnested_ignored:\n  key.sub: v\nlast: ok",
    );
    expect(entries).toEqual([
      { key: "sources", value: "2", list: true },
      { key: "last", value: "ok" },
    ]);
  });
});

describe("splitFrontmatter", () => {
  it("剥离 frontmatter，正文完整保留", () => {
    const { body, entries } = splitFrontmatter(FM);
    expect(body.startsWith("# 正文标题")).toBe(true);
    expect(body).toContain("内容段落。");
    const byKey = Object.fromEntries(entries.map((e) => [e.key, e]));
    expect(byKey.title.value).toBe("特性雷达 2026-11-04");
    expect(byKey.tags.items).toEqual(["特性雷达", "竞品调研", "本地优先"]);
    expect(byKey.sources).toEqual({ key: "sources", value: "2", list: true });
    expect(byKey.empty).toBeUndefined();
  });

  it("无 frontmatter 的文本原样返回", () => {
    const text = "# 标题\n\n正文，无属性块。";
    expect(splitFrontmatter(text)).toEqual({ body: text, entries: [] });
  });

  it("开头的普通分隔线不被误剥（解析不出键时保留原文）", () => {
    const text = "---\n\n# 只有分隔线开头的文档\n\n正文。";
    expect(splitFrontmatter(text).body).toBe(text);
  });

  it("frontmatter 未闭合（流式中途）不剥离", () => {
    const partial = "---\ntitle: streaming";
    expect(splitFrontmatter(partial).body).toBe(partial);
  });

  it("正则只认文档开头", () => {
    const text = "正文里的 --- 分隔线\n\n---\n\n更多";
    expect(FRONTMATTER_RE.test(text)).toBe(false);
  });
});
