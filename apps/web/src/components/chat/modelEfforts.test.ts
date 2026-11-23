import { describe, expect, it } from "vitest";
import type { ProviderConfig } from "@/lib/types";
import { inheritHomeEffort, menuEfforts, modelEffortLevels, normalizeHomeEffort } from "./modelEfforts";

const sol = "codex/gpt-6.2-sol";
const luna = "codex/gpt-6.2-luna";
const alias = "codex/new-route";
const tagged: ProviderConfig = {
  enabled: true, protocol: "openai-responses", api_key: "k", base_url: "https://example.test",
  model_thinking_efforts: {
    [sol]: ["low", "medium", "high", "xhigh", "max", "ultra"],
    [luna]: ["low", "medium", "high", "xhigh", "max"],
    [alias]: [],
  },
};
const plain: ProviderConfig = { ...tagged, model_thinking_efforts: {} };
const solLevels = modelEffortLevels(tagged, sol);
const lunaLevels = modelEffortLevels(tagged, luna);
const unknownLevels = modelEffortLevels(tagged, alias);

describe("saved model capabilities", () => {
  it("scopes options by provider and full model ID and preserves the empty capability list", () => {
    expect(menuEfforts(solLevels)).toEqual(["auto", "low", "medium", "high", "xhigh", "max", "ultra"]);
    expect(menuEfforts(unknownLevels)).toEqual(["auto"]);
    expect(menuEfforts(modelEffortLevels(plain, sol))).toEqual(["auto", "low", "medium", "high"]);
    expect(modelEffortLevels(plain, "toString")).toBeUndefined();
  });

  it("preserves a legacy off on default models and resolves it against Codex capabilities", () => {
    expect(normalizeHomeEffort("off")).toBe("off");
    expect(normalizeHomeEffort("off", solLevels)).toBe("low");
    expect(normalizeHomeEffort("off", unknownLevels)).toBe("auto");
  });
});

describe("home model switch without an explicit effort", () => {
  it("resets when leaving a tagged model even if the destination offers the same effort", () => {
    expect(inheritHomeEffort("high", solLevels, undefined)).toBe("auto");
  });

  it("carries supported efforts and resets above a lower cap or an unknown cap", () => {
    expect(inheritHomeEffort("max", solLevels, lunaLevels)).toBe("max");
    expect(inheritHomeEffort("ultra", solLevels, lunaLevels)).toBe("auto");
    expect(inheritHomeEffort("high", solLevels, unknownLevels)).toBe("auto");
    expect(inheritHomeEffort("high", undefined, lunaLevels)).toBe("high");
  });
});
