"use client";

import { useEffect } from "react";

/**
 * Esc 关闭浮层统一 hook（2026-11-09，PR #69 后推广）。
 *
 * 语义：
 * - `onClose`：Esc 触发的关闭回调（组件仅在浮层打开时挂载即可直接用；
 *   常驻组件传 `enabled=false` 时静默）。
 * - `guard`：可选守卫——返回 true 时本次 Esc **不**关闭（向导进行到
 *   中间步骤时防误触：只在第一步允许 Esc，后续步骤走显式 × / 取消按钮）。
 * - 监听挂在 window 上、依赖稳定化，浮层卸载即移除。
 *
 * 注意：ask_user 类卡片（agent 阻塞等待用户输入）**不要**接此 hook——
 * 关闭 ≠ 回答，误触语义不明。
 */

/** 纯函数层：构造 keydown 处理器（可单测，不依赖 React/DOM）。 */
export function makeEscHandler(
  onClose: () => void,
  guard?: () => boolean,
): (e: KeyboardEvent) => void {
  return (e) => {
    if (e.key !== "Escape") return;
    if (guard && guard()) return;
    e.stopPropagation();
    onClose();
  };
}

export function useEscClose(
  onClose: () => void,
  opts: { enabled?: boolean; guard?: () => boolean } = {},
) {
  const { enabled = true, guard } = opts;
  useEffect(() => {
    if (!enabled) return;
    const onKey = makeEscHandler(onClose, guard);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // onClose/guard 由调用方保证为组件内函数；enabled 变化需重挂。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, onClose, guard]);
}
