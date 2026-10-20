// columnWidth — 聊天阅读列宽的唯一来源（ChatStream / ScheduleRunView 共用）。
// 列宽随窗口自适应：窄窗口自动收窄、宽屏放宽（≈768 → ≈992），
// 公式 clamp(48rem, 50vw + 4rem, 62rem)。bubble 本身仍占满列宽；
// 文本换行边界由 columnWidth 决定的可用宽度给出（见 Markdown.tsx 的 break-words）。
export const CHAT_COL = "mx-auto w-full max-w-[clamp(48rem,50vw+4rem,62rem)]";
