import type { Priority } from "./types";

export const AGENT_HEX: Record<string, string> = {
  blue: "#3b82f6",
  orange: "#f97316",
  green: "#22c55e",
  violet: "#8b5cf6",
  indigo: "#6366f1",
  red: "#ef4444",
};

export function agentHex(color?: string): string {
  return (color && AGENT_HEX[color]) || AGENT_HEX.indigo;
}

export const PRIORITY_HEX: Record<Priority, string> = {
  high: "#ef4444",
  medium: "#eab308",
  low: "#22c55e",
};

export const PRIORITY_LABEL: Record<Priority, string> = {
  high: "High",
  medium: "Medium",
  low: "Low",
};

export const CATEGORY_HEX: Record<string, string> = {
  Dev: "#3b82f6",
  PM: "#f97316",
  Design: "#a855f7",
};

export function categoryStyle(category: string): { color: string; bg: string } {
  const hex = CATEGORY_HEX[category] || "#6366f1";
  return { color: hex, bg: hex + "22" };
}
