"use client";

// useEscClose — 团队空间模态/行内菜单的统一 Esc 关闭（backlog P2-17）。
// 仓库纪律（ConfirmModal docstring）：webview 里原生对话框不可靠，模态一律
// React 自绘；Esc 同理必须由组件自己接。嵌套模态（如 AddMemberModal 之上再
// 开 NewAgentModal）用模块级栈保证只有栈顶响应——一次 Esc 只关最上一层。

import { useEffect, useRef } from "react";

const stack: Array<{ close: () => void }> = [];

/** enabled=false 时不注册（条件场景：行内菜单打开 / 确认弹窗打开）。 */
export function useEscClose(onClose: () => void, enabled = true) {
  const ref = useRef(onClose);
  ref.current = onClose;
  useEffect(() => {
    if (!enabled) return;
    const entry = { close: () => ref.current() };
    stack.push(entry);
    const handler = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (stack[stack.length - 1] !== entry) return;
      e.preventDefault();
      entry.close();
    };
    window.addEventListener("keydown", handler);
    return () => {
      const i = stack.indexOf(entry);
      if (i >= 0) stack.splice(i, 1);
      window.removeEventListener("keydown", handler);
    };
  }, [enabled]);
}
