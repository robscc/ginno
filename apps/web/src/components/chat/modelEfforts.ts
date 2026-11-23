import type { ProviderConfig, ThinkingEffort } from "@/lib/types";

type EffortLevels = NonNullable<ProviderConfig["model_thinking_efforts"]>[string];
const DEFAULT_EFFORTS = ["low", "medium", "high"] as const;

export function modelEffortLevels(provider: ProviderConfig | undefined, model: string | undefined) {
  const efforts = provider?.model_thinking_efforts;
  return model && efforts && Object.hasOwn(efforts, model) ? efforts[model] : undefined;
}

export function menuEfforts(levels?: EffortLevels): readonly ThinkingEffort[] {
  return ["auto", ...(levels ?? DEFAULT_EFFORTS)];
}

export function normalizeHomeEffort(value: string | undefined, levels?: EffortLevels): ThinkingEffort {
  if (value === "off") {
    if (levels === undefined) return "off";
    return levels.includes("low") ? "low" : "auto";
  }
  return menuEfforts(levels).find((effort) => effort === value) ?? "auto";
}

export function inheritHomeEffort(
  value: ThinkingEffort,
  source: EffortLevels | undefined,
  destination: EffortLevels | undefined,
): ThinkingEffort {
  return source !== undefined && destination === undefined
    ? "auto"
    : normalizeHomeEffort(value, destination);
}
