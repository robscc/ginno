/** Session label/icon（session-label-icon-design.md §2）纯逻辑测试。 */
import { describe, expect, it } from "vitest";
import { foldLabels, parseLabelQuery, sessionMatchesLabel } from "./sessionTags";

describe("parseLabelQuery", () => {
  it("非 # 前缀 → 不是 label 查询", () => {
    expect(parseLabelQuery("release")).toEqual({ isLabel: false, label: "" });
    expect(parseLabelQuery("")).toEqual({ isLabel: false, label: "" });
    expect(parseLabelQuery("a #b")).toEqual({ isLabel: false, label: "" });
  });
  it("# 前缀 → 取 # 后整串（忽略前导空白）", () => {
    expect(parseLabelQuery("#release")).toEqual({ isLabel: true, label: "release" });
    expect(parseLabelQuery("  # v0.1")).toEqual({ isLabel: true, label: "v0.1" });
    expect(parseLabelQuery("#RELease")).toEqual({ isLabel: true, label: "RELease" });
  });
  it("混合输入 #a b：MVP 不支持，# 后整串当作 label", () => {
    expect(parseLabelQuery("#a b")).toEqual({ isLabel: true, label: "a b" });
  });
  it("单独 # 不算 label 查询", () => {
    expect(parseLabelQuery("#")).toEqual({ isLabel: false, label: "" });
    expect(parseLabelQuery("#   ")).toEqual({ isLabel: false, label: "" });
  });
});

describe("sessionMatchesLabel", () => {
  const labels = ["release", "v0.1.2"];
  it("子串、忽略大小写、命中任一即通过", () => {
    expect(sessionMatchesLabel(labels, "release")).toBe(true);
    expect(sessionMatchesLabel(labels, "RELE")).toBe(true);
    expect(sessionMatchesLabel(labels, "v0.1")).toBe(true);
    expect(sessionMatchesLabel(labels, "dev")).toBe(false);
  });
  it("无标签/空 needle 不命中", () => {
    expect(sessionMatchesLabel(undefined, "x")).toBe(false);
    expect(sessionMatchesLabel([], "x")).toBe(false);
    expect(sessionMatchesLabel(labels, "")).toBe(false);
  });
});

describe("foldLabels", () => {
  it("≤2 个全显示", () => {
    expect(foldLabels(undefined)).toEqual({ shown: [], extra: 0 });
    expect(foldLabels(["a"])).toEqual({ shown: ["a"], extra: 0 });
    expect(foldLabels(["a", "b"])).toEqual({ shown: ["a", "b"], extra: 0 });
  });
  it(">2 个折叠为 +N", () => {
    expect(foldLabels(["a", "b", "c"])).toEqual({ shown: ["a", "b"], extra: 1 });
    expect(foldLabels(["a", "b", "c", "d"])).toEqual({ shown: ["a", "b"], extra: 2 });
  });
});
