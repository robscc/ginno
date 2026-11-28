// sourceColor 单测：正典表固定色 + 未知来源 djb2 确定性取色 + 底色透明度惯例。
// 纯函数、零依赖、零 DOM，node 环境直测。
//
// 未知来源的期望色不是抄实现，而是按源码注释声明的 djb2 算法
// （h=5381; h=((h*33)^charCode)>>>0; h%7）独立预计算后硬编码：
//   ""→5  "mcp:trending"→4  "custom-source"→2  "Chat"→6  "新来源"→5
//   "unknown-xyz"→0  "a"→0  "b"→3

import { describe, expect, it } from "vitest";
import { USAGE_SOURCES, sourceBg, sourceFg } from "@/lib/sourceColor";

/** 正典前景色（与 SRC_STYLE/SOURCE_META 历史观感一致的规格值）。 */
const CANONICAL: Record<string, string> = {
  chat: "#8d90f8",
  image_generation: "#60a5fa",
  goal: "#b39df9",
  compaction: "#d9a93e",
  workflow: "#4ade80",
  memory: "#38bdf8",
  kb: "#60a5fa",
  probe: "#9a9aa6",
  external: "#f472b6",
  subagent: "#34d399",
  subagent_plan: "#5eead4",
  subagent_gate: "#22d3ee",
  title: "#fb923c",
  team: "#e879f9",
  schedule: "#facc15",
  mods: "#c084fc",
  classifier: "#94a3b8",
  channel_resume: "#2dd4bf",
  other: "#9a9aa6",
};

/** Ginno 色板（语义色 400 档）——未知来源只允许落在这 7 色里。 */
const PALETTE_400 = ["#818cf8", "#a78bfa", "#60a5fa", "#4ade80", "#facc15", "#fb923c", "#f87171"];

describe("sourceFg 正典表", () => {
  it("19 个已知来源逐一返回固定正典色", () => {
    for (const [name, hex] of Object.entries(CANONICAL)) {
      expect(sourceFg(name), name).toBe(hex);
    }
  });

  it("USAGE_SOURCES 是 19 项规范顺序，且每项都有正典色", () => {
    expect(USAGE_SOURCES).toEqual([
      "chat",
      "image_generation",
      "goal",
      "compaction",
      "workflow",
      "memory",
      "kb",
      "probe",
      "external",
      "subagent",
      "subagent_plan",
      "subagent_gate",
      "title",
      "team",
      "schedule",
      "mods",
      "classifier",
      "channel_resume",
      "other",
    ]);
    expect(USAGE_SOURCES).toHaveLength(19);
    for (const name of USAGE_SOURCES) {
      expect(CANONICAL[name], name).toBeDefined();
      expect(sourceFg(name)).toBe(CANONICAL[name]);
    }
  });
});

describe("sourceFg 未知来源（hash 取色）", () => {
  it("确定性：同名永远同色，且命中独立预计算的 djb2 期望值", () => {
    const expected: Array<[string, string]> = [
      ["", "#fb923c"], // 空串：h 停在 5381，5381 % 7 = 5
      ["mcp:trending", "#facc15"],
      ["custom-source", "#60a5fa"],
      ["Chat", "#f87171"],
      ["新来源", "#fb923c"], // 非 ASCII 码点同样参与 hash
      ["unknown-xyz", "#818cf8"],
    ];
    for (const [name, hex] of expected) {
      expect(sourceFg(name), name).toBe(hex);
      expect(sourceFg(name), `${name} 二次调用`).toBe(hex);
    }
  });

  it("未知来源只落在 Ginno 色板内，不引入外来色相", () => {
    const names = ["a", "b", "c", "foo", "bar", "baz", "来源甲", "来源乙", "x-1", "x-2", "x-3"];
    for (const n of names) {
      expect(PALETTE_400, n).toContain(sourceFg(n));
    }
  });

  it("不同名字可以分到不同色（hash 有区分度，不是恒返回一色）", () => {
    expect(sourceFg("a")).toBe("#818cf8");
    expect(sourceFg("b")).toBe("#4ade80");
    expect(sourceFg("a")).not.toBe(sourceFg("b"));
    const seen = new Set(names100().map(sourceFg));
    expect(seen.size).toBeGreaterThanOrEqual(5); // 100 个名字至少铺开 5/7 色
  });

  it("大小写敏感：Chat/CHAT 不是正典 chat，走 hash 且与 chat 不同色", () => {
    expect(sourceFg("chat")).toBe("#8d90f8");
    expect(sourceFg("Chat")).toBe("#f87171");
    expect(sourceFg("CHAT")).not.toBe("#8d90f8");
    expect(PALETTE_400).toContain(sourceFg("CHAT"));
  });
});

describe("sourceBg 底色", () => {
  it("已知来源：正典色 + 22 透明度后缀", () => {
    expect(sourceBg("chat")).toBe("#8d90f822");
    expect(sourceBg("subagent")).toBe("#34d39922");
    expect(sourceBg("other")).toBe("#9a9aa622");
  });

  it("未知来源与空串：hash 色 + 22，且与 sourceFg 严格一致", () => {
    for (const n of ["mcp:trending", "custom-source", "", "新来源"]) {
      expect(sourceBg(n)).toBe(`${sourceFg(n)}22`);
    }
    expect(sourceBg("mcp:trending")).toBe("#facc1522");
    expect(sourceBg("")).toBe("#fb923c22");
  });

  it("格式恒为 #RRGGBB22（9 字符）", () => {
    for (const n of [...USAGE_SOURCES, "unknown-xyz", ""]) {
      expect(sourceBg(n)).toMatch(/^#[0-9a-f]{6}22$/);
    }
  });
});

describe("quirk：原型链键穿透正典表（固化当前行为，不改源码）", () => {
  // CANONICAL_FG 是普通对象字面量，`CANONICAL_FG[name] ?? fallback` 对
  // "toString"/"constructor" 等继承键取到的是 Object.prototype 上的函数/对象，
  // 非 null/undefined → ?? 不兜底，sourceFg 实际返回的不是颜色字符串。
  // 来源名来自 runtime 自然统计，理论上可出现这类名字。缺陷已报告，此处固化。
  it('sourceFg("toString") 返回继承的 toString 函数而非色板色', () => {
    expect(sourceFg("toString")).toBe(Object.prototype.toString);
    expect(typeof sourceFg("toString")).toBe("function");
  });

  it('sourceFg("constructor") 返回 Object 构造器', () => {
    expect(sourceFg("constructor")).toBe(Object);
  });

  it('sourceBg("toString") 把函数字符串化后拼 22', () => {
    const bg = sourceBg("toString");
    expect(bg.startsWith("function")).toBe(true);
    expect(bg.endsWith("22")).toBe(true);
    expect(bg).toBe(`${String(Object.prototype.toString)}22`);
  });
});

/** 100 个互不相同的来源名，用于色板分布断言。 */
function names100(): string[] {
  return Array.from({ length: 100 }, (_, i) => `src-${i}`);
}
