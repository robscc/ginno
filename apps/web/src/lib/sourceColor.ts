// sourceColor — 用量「来源」的统一配色（2026-10-13 用户定稿）。
//
// 来源在 runtime 侧不再白名单校验（自然统计），前端不能每来一个新来源就
// 加一条配色。策略：
// * 已知来源 → 正典表固定色（历史观感不变）；
// * 未知来源 → 名称 hash 从 **Ginno 色板**（tailwind.config 的语义色 400
//   档，与 agentHex/categoryStyle 同一家族）确定性取色——同名永远同色，
//   风格与全局一致，不引入外来色相；
// * 底色沿用 theme.ts categoryStyle 的 `hex + "22"` 透明度惯例。

/** 已知来源的正典前景色（保持既有 SRC_STYLE/SOURCE_META 观感，不再外扩）。 */
const CANONICAL_FG: Record<string, string> = {
  chat: "#8d90f8",
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

/** 来源筛选/图例的规范顺序（与 usage overview 的分桶一致）。 */
export const USAGE_SOURCES = [
  "chat",
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
] as const;

/** Ginno 色板（语义色 400 档）：未知来源的取色域。 */
const PALETTE_400 = ["#818cf8", "#a78bfa", "#60a5fa", "#4ade80", "#facc15", "#fb923c", "#f87171"];

/** djb2：短、稳定、无依赖。 */
function hashIndex(name: string, mod: number): number {
  let h = 5381;
  for (let i = 0; i < name.length; i++) {
    h = ((h * 33) ^ name.charCodeAt(i)) >>> 0;
  }
  return h % mod;
}

export function sourceFg(name: string): string {
  return CANONICAL_FG[name] ?? PALETTE_400[hashIndex(name, PALETTE_400.length)];
}

export function sourceBg(name: string): string {
  return `${sourceFg(name)}22`;
}
