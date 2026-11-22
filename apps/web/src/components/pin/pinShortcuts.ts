/** Pin 窗 window 级快捷键判定（纯函数，vitest 直测；PinApp onKey 消费）。
 *
 * IME 合成守卫（2026-11 Q8）：中文输入法打字时候选窗口激活，Esc 的语义
 * 是「取消候选」、Enter 是「上屏」——此刻把 pin 窗收成 pill / 跳主窗都是
 * 误伤。与 Composer.tsx / AskUserCard.tsx 的既有 guard 同款：
 * `isComposing` 是标准判据，`keyCode === 229` 兜底合成期间部分引擎
 * （旧 Chromium/Safari）keydown 不置 isComposing、只给 229 的情况。
 */

export type PinShortcutAction = "collapse" | "open-main";

/** keydown 的最小形状（原生 KeyboardEvent 的结构子集，便于单测构造）。 */
export interface PinKeyLike {
  key: string;
  isComposing?: boolean;
  keyCode?: number;
  metaKey?: boolean;
  ctrlKey?: boolean;
}

/** true = 该 keydown 处于 IME 合成中，任何窗口快捷键都不应触发。 */
export function isImeComposing(e: PinKeyLike): boolean {
  return e.isComposing === true || e.keyCode === 229;
}

/** 判定 pin 窗快捷键：Esc → collapse；⌘/Ctrl+Enter → open-main；
 * 合成中或其它键 → null（调用方不响应）。 */
export function pinShortcutAction(e: PinKeyLike): PinShortcutAction | null {
  if (isImeComposing(e)) return null;
  if (e.key === "Escape") return "collapse";
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) return "open-main";
  return null;
}
