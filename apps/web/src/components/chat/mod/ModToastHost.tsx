"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Mod toast 宿主(claude-code-mods-design.md §7.2):AppShell 顶层消费
 * `mod.toast` 帧。全局原本没有 toast 系统(connector fallback 是单条局部
 * state),这里用模块级事件总线——engine 的 socket 回调在任何组件树之外也能
 * 推送,宿主组件挂载后订阅即可,侵入最小。
 *
 * 形态参照 AppShell 的 fallbackToast:右下角卡片、8s 自动消失、最多堆叠 5 条。
 */

export type ModToastLevel = "info" | "warn" | "error";

interface ModToast {
  id: number;
  level: ModToastLevel;
  text: string;
}

type Listener = (t: ModToast) => void;

let listeners: Listener[] = [];
let seq = 0;

/** 供 engine 的 `mod.toast` 分支调用;宿主未挂载时帧自然丢弃。 */
export function pushModToast(level: ModToastLevel, text: string) {
  if (!text) return;
  const t: ModToast = { id: ++seq, level, text };
  for (const l of listeners) {
    try {
      l(t);
    } catch {
      /* 单个订阅者异常不影响其他 */
    }
  }
}

const LEVEL_META: Record<ModToastLevel, { border: string; bg: string; dot: string }> = {
  info: { border: "border-blue/40", bg: "bg-blue/10", dot: "bg-blue" },
  warn: { border: "border-yellow/40", bg: "bg-yellow/10", dot: "bg-yellow" },
  error: { border: "border-red/40", bg: "bg-red/10", dot: "bg-red" },
};

const MAX_TOASTS = 5;
const AUTO_DISMISS_MS = 8000;

export function ModToastHost() {
  const [toasts, setToasts] = useState<ModToast[]>([]);
  const timersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());

  useEffect(() => {
    const dismiss = (id: number) => {
      setToasts((prev) => prev.filter((x) => x.id !== id));
    };
    const listener: Listener = (t) => {
      // 超出堆叠上限挤掉最旧的一条。
      setToasts((prev) => [...prev.slice(-(MAX_TOASTS - 1)), t]);
      const timer = setTimeout(() => {
        timersRef.current.delete(timer);
        dismiss(t.id);
      }, AUTO_DISMISS_MS);
      timersRef.current.add(timer);
    };
    listeners.push(listener);
    return () => {
      listeners = listeners.filter((l) => l !== listener);
      for (const timer of timersRef.current) clearTimeout(timer);
      timersRef.current.clear();
    };
  }, []);

  if (toasts.length === 0) return null;
  return (
    <div className="fixed bottom-5 right-5 z-50 flex w-80 flex-col gap-2">
      {toasts.map((t) => {
        const meta = LEVEL_META[t.level] ?? LEVEL_META.info;
        return (
          <div
            key={t.id}
            className={`rounded-xl border px-4 py-3 text-xs leading-relaxed text-txt shadow-lg ${meta.border} ${meta.bg}`}
          >
            <div className="flex items-start gap-2">
              <span className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${meta.dot}`} />
              <span className="min-w-0 break-words whitespace-pre-wrap">{t.text}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
