// rightTabs 单测：右侧面板 tab 注册表 + 顺序规范化纯函数。
// 无外部依赖可桩：lucide-react 图标在 node 下就是普通对象（比引用同一性），
// ./store 的 RightTab 是 type-only import（构建期擦除，不拉 React 模块图）。

import { describe, expect, it } from "vitest";
import { Bell, Brain, Code2, FileBox, ListTodo, Sparkles, Zap } from "lucide-react";
import {
  ALL_RIGHT_TAB_IDS,
  RIGHT_TABS,
  RIGHT_TAB_BY_ID,
  canonicalTabOrder,
  visibleTabOrder,
} from "@/lib/rightTabs";
import type { RightTab } from "@/lib/store";

const DEFAULT_ORDER: RightTab[] = [
  "artifacts",
  "todo",
  "workflow",
  "memory",
  "synthesis",
  "code",
  "notice",
];

describe("注册表", () => {
  it("七个 tab，顺序即默认顺序（新 tab 只追加末尾的约定）", () => {
    expect(RIGHT_TABS.map((t) => t.id)).toEqual(DEFAULT_ORDER);
    expect(ALL_RIGHT_TAB_IDS).toEqual(DEFAULT_ORDER);
  });

  it("labelKey 一律 tabs.<id>（right 域相对路径）", () => {
    for (const t of RIGHT_TABS) expect(t.labelKey).toBe(`tabs.${t.id}`);
  });

  it("icon 是对应的 lucide 组件（同一引用）", () => {
    const expected = {
      artifacts: FileBox,
      todo: ListTodo,
      workflow: Zap,
      memory: Brain,
      synthesis: Sparkles,
      code: Code2,
      notice: Bell,
    };
    for (const t of RIGHT_TABS) expect(t.icon).toBe(expected[t.id]);
  });

  it("RIGHT_TAB_BY_ID 覆盖全部 id 且映射回同一对象", () => {
    expect(Object.keys(RIGHT_TAB_BY_ID).sort()).toEqual([...DEFAULT_ORDER].sort());
    for (const t of RIGHT_TABS) expect(RIGHT_TAB_BY_ID[t.id]).toBe(t);
  });
});

describe("canonicalTabOrder", () => {
  it("无存储偏好 → 默认顺序", () => {
    expect(canonicalTabOrder()).toEqual(DEFAULT_ORDER);
    expect(canonicalTabOrder(undefined)).toEqual(DEFAULT_ORDER);
  });

  it("非数组输入（null/数字/字符串/对象）→ 默认顺序", () => {
    for (const v of [null, 42, "code", { id: "code" }]) {
      expect(canonicalTabOrder(v)).toEqual(DEFAULT_ORDER);
    }
  });

  it("空数组 → 默认顺序", () => {
    expect(canonicalTabOrder([])).toEqual(DEFAULT_ORDER);
  });

  it("非字符串元素被过滤，其余保持相对顺序", () => {
    expect(canonicalTabOrder(["code", 7, null, undefined, {}, "todo"])).toEqual([
      "code",
      "todo",
      "artifacts",
      "workflow",
      "memory",
      "synthesis",
      "notice",
    ]);
  });

  it("本构建不认识的 id 被丢弃（已删除的 tab 不残留）", () => {
    const out = canonicalTabOrder(["ghost", "notice", "legacy_tab"]);
    expect(out).toEqual(["notice", "artifacts", "todo", "workflow", "memory", "synthesis", "code"]);
    expect(out).not.toContain("ghost");
    expect(out).not.toContain("legacy_tab");
  });

  it("重复 id 去重，保留首次出现位置", () => {
    expect(canonicalTabOrder(["code", "code", "todo", "code"])).toEqual([
      "code",
      "todo",
      "artifacts",
      "workflow",
      "memory",
      "synthesis",
      "notice",
    ]);
  });

  it("存储里缺失的 tab 按默认顺序补到末尾（新面板不至于隐形）", () => {
    expect(canonicalTabOrder(["notice", "artifacts"])).toEqual([
      "notice",
      "artifacts",
      "todo",
      "workflow",
      "memory",
      "synthesis",
      "code",
    ]);
  });

  it("完整合法排列原样保留", () => {
    const reversed = [...DEFAULT_ORDER].reverse();
    expect(canonicalTabOrder(reversed)).toEqual(reversed);
  });
});

describe("visibleTabOrder", () => {
  it("无参数 → 全部可见", () => {
    expect(visibleTabOrder()).toEqual(DEFAULT_ORDER);
  });

  it("按用户顺序渲染并过滤隐藏项（缺失 tab 仍先补齐再过滤）", () => {
    expect(visibleTabOrder(["code", "notice", "todo"], ["notice"])).toEqual([
      "code",
      "todo",
      "artifacts",
      "workflow",
      "memory",
      "synthesis",
    ]);
  });

  it("hidden 非数组 → 忽略；含非字符串 → 只过滤合法部分", () => {
    expect(visibleTabOrder(undefined, "todo")).toEqual(DEFAULT_ORDER);
    expect(visibleTabOrder(undefined, ["todo", 3, null])).toEqual(
      DEFAULT_ORDER.filter((id) => id !== "todo"),
    );
  });

  it("隐藏未知 id 无副作用", () => {
    expect(visibleTabOrder(undefined, ["ghost"])).toEqual(DEFAULT_ORDER);
  });

  it("全部隐藏 → 保底保留 canonical 第一个（默认序 → artifacts）", () => {
    expect(visibleTabOrder(undefined, DEFAULT_ORDER)).toEqual(["artifacts"]);
  });

  it("全部隐藏 → 保底跟随用户自定义顺序的第一个", () => {
    expect(visibleTabOrder(["notice", "code"], DEFAULT_ORDER)).toEqual(["notice"]);
  });
});
