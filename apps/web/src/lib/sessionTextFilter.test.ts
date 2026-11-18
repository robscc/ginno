/**
 * sessionTextFilter（2026-11-18 旅程走查 P1-2/P1-3）：就地过滤的打分制匹配。
 * contains(3) > 拼音首字母(2) > 子序列(1)；空查询直通；稳定排序 contains 优先。
 */
import { describe, expect, it } from "vitest";
import { filterByText, textMatchScore } from "./sessionTextFilter";

describe("textMatchScore", () => {
  it("空查询 = 全匹配（不过滤语义）", () => {
    expect(textMatchScore("任何标题", "")).toBe(3);
    expect(textMatchScore("any", "   ")).toBe(3);
  });

  it("contains（大小写不敏感）得 3 分", () => {
    expect(textMatchScore("Ginno Harness 谱查", "harness")).toBe(3);
    expect(textMatchScore("deploy plan", "PLAN")).toBe(3);
  });

  it("拼音首字母串 contains 得 2 分（「会话 热更新」→ hhrgx）", () => {
    expect(textMatchScore("会话 热更新", "hhrgx")).toBe(2);
    expect(textMatchScore("每日总结", "mrzj")).toBe(2);
  });

  it("子序列（跳跃保序）得 1 分", () => {
    expect(textMatchScore("deploy", "dpy")).toBe(1);
  });

  it("不匹配得 0", () => {
    expect(textMatchScore("deploy", "xyz")).toBe(0);
    expect(textMatchScore("会话", "mrzj")).toBe(0);
  });
});

describe("filterByText", () => {
  const items = ["每日总结", "deploy plan", "会话热更新"];

  it("空查询原样返回（同引用语义的不过滤）", () => {
    expect(filterByText(items, "", (s) => s)).toEqual(items);
  });

  it("按分数过滤并排序（contains 优先于子序列）", () => {
    // "deploy plan" contains(3)；"每日总结" 对 "dl" 无子序列…借英文混合验证排序
    const out = filterByText(["deploy plan", "deeploy"], "dep", (s) => s);
    expect(out).toEqual(["deploy plan", "deeploy"]); // 3 分在前，1 分在后
  });

  it("对象列表 + 拼音命中", () => {
    const objs = [{ t: "每日总结" }, { t: "deploy" }];
    expect(filterByText(objs, "mrzj", (o) => o.t)).toEqual([{ t: "每日总结" }]);
  });
});
