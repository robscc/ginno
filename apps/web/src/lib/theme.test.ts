// theme 单测：agent/优先级/分类三套配色的查表与兜底。
// 零 DOM、仅 type import，node 环境直测。基线 83.33%（agentHex 与
// categoryStyle 函数体未覆盖），本文件补齐全部分支并固化原型链 quirk。

import { describe, expect, it } from "vitest";
import {
  AGENT_HEX,
  CATEGORY_HEX,
  PRIORITY_HEX,
  PRIORITY_LABEL,
  agentHex,
  categoryStyle,
} from "@/lib/theme";

describe("AGENT_HEX / agentHex", () => {
  it("正典 6 色表逐值核对", () => {
    expect(AGENT_HEX).toEqual({
      blue: "#3b82f6",
      orange: "#f97316",
      green: "#22c55e",
      violet: "#8b5cf6",
      indigo: "#6366f1",
      red: "#ef4444",
    });
  });

  it("已知颜色名返回对应 hex", () => {
    for (const [name, hex] of Object.entries(AGENT_HEX)) {
      expect(agentHex(name), name).toBe(hex);
    }
  });

  it("undefined / 空串 / 未知名 → 兜底 indigo", () => {
    expect(agentHex()).toBe("#6366f1");
    expect(agentHex(undefined)).toBe("#6366f1");
    expect(agentHex("")).toBe("#6366f1");
    expect(agentHex("teal")).toBe("#6366f1");
    expect(agentHex("BLUE")).toBe("#6366f1"); // 大小写敏感，未命中即兜底
    expect(agentHex(null as unknown as string)).toBe("#6366f1");
  });

  it("quirk：原型链键穿透查表——agentHex(\"toString\") 返回函数而非兜底色", () => {
    // AGENT_HEX 是普通对象字面量，(color && AGENT_HEX[color]) || indigo
    // 对继承键取到 truthy 的 Object.prototype.toString → 不兜底。
    // 颜色名来自用户/模型输入，理论上可出现这类值。缺陷已报告，固化现状。
    expect(agentHex("toString")).toBe(Object.prototype.toString);
    expect(typeof agentHex("toString")).toBe("function");
    expect(agentHex("constructor")).toBe(Object);
  });
});

describe("PRIORITY_HEX / PRIORITY_LABEL", () => {
  it("三档优先级颜色与文案逐值核对", () => {
    expect(PRIORITY_HEX).toEqual({ high: "#ef4444", medium: "#eab308", low: "#22c55e" });
    expect(PRIORITY_LABEL).toEqual({ high: "High", medium: "Medium", low: "Low" });
  });

  it("两表键集一致且恰为 high/medium/low", () => {
    expect(Object.keys(PRIORITY_HEX).sort()).toEqual(["high", "low", "medium"]);
    expect(Object.keys(PRIORITY_LABEL).sort()).toEqual(Object.keys(PRIORITY_HEX).sort());
  });
});

describe("CATEGORY_HEX / categoryStyle", () => {
  it("正典 3 类逐值核对", () => {
    expect(CATEGORY_HEX).toEqual({ Dev: "#3b82f6", PM: "#f97316", Design: "#a855f7" });
  });

  it("已知分类：前景色 + 22 透明度底色", () => {
    expect(categoryStyle("Dev")).toEqual({ color: "#3b82f6", bg: "#3b82f622" });
    expect(categoryStyle("PM")).toEqual({ color: "#f97316", bg: "#f9731622" });
    expect(categoryStyle("Design")).toEqual({ color: "#a855f7", bg: "#a855f722" });
  });

  it("未知分类 / 空串 → indigo 兜底，底色同惯例", () => {
    expect(categoryStyle("Ops")).toEqual({ color: "#6366f1", bg: "#6366f122" });
    expect(categoryStyle("")).toEqual({ color: "#6366f1", bg: "#6366f122" });
    expect(categoryStyle("dev")).toEqual({ color: "#6366f1", bg: "#6366f122" }); // 大小写敏感
  });

  it("底色格式恒为 #RRGGBB22", () => {
    for (const c of ["Dev", "PM", "Design", "Ops", ""]) {
      expect(categoryStyle(c).bg).toMatch(/^#[0-9a-f]{6}22$/);
    }
  });

  it("quirk：categoryStyle(\"toString\") 的 color 是函数，bg 是函数字符串拼 22", () => {
    // CATEGORY_HEX[category] || "#6366f1" 对继承键取到 truthy 函数 → 不兜底。
    const style = categoryStyle("toString");
    expect(style.color).toBe(Object.prototype.toString);
    expect(style.bg).toBe(`${String(Object.prototype.toString)}22`);
    expect(style.bg.endsWith("22")).toBe(true);
  });
});
