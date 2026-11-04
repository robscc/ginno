import { describe, expect, it } from "vitest";
import { blankConfig, countNonDefaults, reconcileDefaultModel } from "./modelConfigShared";

// 进阶段「已改项」徽标的计数口径（0 = 不显示徽标）。
const base = () => ({
  ...blankConfig(),
  name: "x",
  base_url: "https://x.example/v1",
  api_key: "k",
  models: ["m"],
  default_model: "m",
});

describe("countNonDefaults", () => {
  it("counts 0 for a default (blank) config", () => {
    expect(countNonDefaults(base())).toBe(0);
  });

  it("counts each non-default advanced field once", () => {
    const cfg = {
      ...base(),
      enable_search: true,
      thinking_mode: "none" as const,
      extra_body: { top_k: 5 },
    };
    expect(countNonDefaults(cfg)).toBe(3);
  });

  it("treats auto / unset / all-zero budgets as defaults", () => {
    const cfg = {
      ...base(),
      thinking_mode: "auto" as const,
      thinking_param_style: "auto" as const,
      thinking_budgets: { low: 0, medium: 0, high: 0 },
    };
    expect(countNonDefaults(cfg)).toBe(0);
  });

  it("counts a budget override >0 and a non-5 max_uses", () => {
    expect(countNonDefaults({ ...base(), thinking_budgets: { high: 2048 } })).toBe(1);
    expect(
      countNonDefaults({ ...base(), server_web_search: true, server_web_search_max_uses: 9 }),
    ).toBe(2);
    // max_uses 缺省（undefined）视同默认 5，只计开关本身
    expect(countNonDefaults({ ...base(), server_web_search: true })).toBe(1);
  });
});

// 默认模型不变式（default_model ∈ models）：chip 行移除 / textarea 解析 /
// mount 收敛共用。radio 行内点选外无旁路——脏值一律收敛到合法值。
describe("reconcileDefaultModel", () => {
  it("keeps the previous default while it survives in the list", () => {
    expect(reconcileDefaultModel(["a", "b", "c"], "b")).toBe("b");
  });

  it("falls to the first entry when the default was removed", () => {
    expect(reconcileDefaultModel(["a", "c"], "b")).toBe("a");
  });

  it("falls to the first entry when there was no default (legacy/dirty)", () => {
    expect(reconcileDefaultModel(["a", "c"], "")).toBe("a");
    expect(reconcileDefaultModel(["a", "c"], undefined)).toBe("a");
  });

  it("returns empty string for an empty list (validation catches it)", () => {
    expect(reconcileDefaultModel([], "a")).toBe("");
    expect(reconcileDefaultModel([], undefined)).toBe("");
  });

  it("does not resurrect a default that is not in the list", () => {
    expect(reconcileDefaultModel(["x"], "y")).toBe("x");
  });
});
