"use client";

import { useEffect, useId, useLayoutEffect, useRef } from "react";
import { useTranslations } from "next-intl";

// ---- 模块级辅助 ----

/** 打开中的 ConfirmModal 栈：叠层模态（确认弹窗上再开确认弹窗）只有栈顶响应
 *  Esc 与 Tab 陷阱，一次按键只作用在最上层（模式先例：team/useEscClose）。 */
type ModalEntry = {
  container: HTMLDivElement | null;
  cancel: () => void;
};

const openModals: ModalEntry[] = [];

/** 供更早注册的 window 捕获监听器判断本次 Escape 是否属于确认框。 */
export const hasOpenConfirmModal = () => openModals.length > 0;

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

const useIsomorphicLayoutEffect =
  typeof window !== "undefined" ? useLayoutEffect : useEffect;

/** In-app confirmation modal. Used instead of window.confirm because the native
 *  dialog is unreliable in the Tauri webview; being React-rendered, this works
 *  identically in Tauri and the browser.
 *
 *  焦点与键盘可达性（2026-11-21 无障碍补齐）：
 *  - 打开时初始焦点落在「取消」这个安全动作上，不会一键 Tab+Enter 误触危险操作；
 *  - Tab / Shift+Tab 焦点循环被限制在对话框内（焦点陷阱）；
 *  - Esc 关闭且在 window 捕获阶段 stopPropagation 吃掉事件——底下的抽屉/面板/
 *    React 合成层的 Esc 处理器（ConnectorDetail、team/useEscClose、AppShell 等
 *    多为 window/document 冒泡监听）不会再收到同一次按键，不会「一次 Esc 连环关」；
 *  - 关闭时焦点回到打开前的触发元素；触发元素已被卸载（确认删除的常见结局）则
 *    降级为不处理，交给浏览器默认行为。 */
export function ConfirmModal({
  title,
  message,
  confirmLabel,
  onConfirm,
  onCancel,
}: {
  title: string;
  message: string;
  confirmLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const t = useTranslations("ui");
  // 未显式传入确认键时默认「删除」（危险操作），取消键走 ui 域通用文案
  const confirmText = confirmLabel ?? t("common.delete");

  const containerRef = useRef<HTMLDivElement | null>(null);
  // 初始焦点：取消按钮（安全动作）
  const cancelBtnRef = useRef<HTMLButtonElement | null>(null);
  // 打开前持有焦点的元素（通常就是触发按钮）
  const triggerRef = useRef<HTMLElement | null>(null);
  const entryRef = useRef<ModalEntry | null>(null);
  // onCancel 可能随渲染换引用，键盘处理器经 ref 取最新值，避免 effect 反复重挂
  const onCancelRef = useRef(onCancel);
  onCancelRef.current = onCancel;

  const titleId = useId();
  const descId = useId();

  useIsomorphicLayoutEffect(() => {
    const entry: ModalEntry = {
      container: containerRef.current,
      cancel: () => onCancelRef.current(),
    };
    entryRef.current = entry;
    openModals.push(entry);
    triggerRef.current = (document.activeElement as HTMLElement | null) ?? null;
    cancelBtnRef.current?.focus();
    return () => {
      const i = openModals.indexOf(entry);
      if (i >= 0) openModals.splice(i, 1);
      entryRef.current = null;
      const trigger = triggerRef.current;
      triggerRef.current = null;
      // 触发器仍在文档里才回焦；确认删除等场景触发器已随行卸载 → 降级不处理
      if (trigger && trigger.isConnected) trigger.focus();
    };
  }, []);

  // Esc / Tab 陷阱：window 捕获阶段拦截（先例：FloatingSettings 快捷键录制器）。
  // stopPropagation 在捕获阶段生效时，事件不会下传到 document/React 根、也不会
  // 冒泡回 window 的冒泡监听器——底层面板的 Esc 不会被同一次按键触发。
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const entry = entryRef.current;
      if (!entry) return;
      if (openModals[openModals.length - 1] !== entry) return; // 叠层时仅栈顶响应
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        entry.cancel();
        return;
      }
      if (e.key !== "Tab") return;
      const container = entry.container;
      if (!container) return;
      const focusables = Array.from(
        container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      ).filter(
        (el) => el.offsetParent !== null || el === document.activeElement,
      );
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (!first || !last) return;
      const active = document.activeElement as HTMLElement | null;
      const inside = active != null && container.contains(active);
      if (e.shiftKey) {
        // 首个元素上 Shift+Tab → 绕到末尾；焦点已逃出容器 → 拉回末尾。
        // 中间位置不劫持，交还浏览器默认行为。
        if (!inside || active === first) {
          e.preventDefault();
          e.stopPropagation();
          last.focus();
        }
      } else {
        // 末个元素上 Tab → 绕回头部；焦点已逃出容器 → 拉回头部
        if (!inside || active === last) {
          e.preventDefault();
          e.stopPropagation();
          first.focus();
        }
      }
    }
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);

  const confirmBtnCls =
    "rounded-lg bg-red px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";

  return (
    <div
      ref={containerRef}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onMouseDown={onCancel}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={descId}
    >
      <div
        className="w-full max-w-sm rounded-xl border border-line bg-card p-4 shadow-2xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div id={titleId} className="text-sm font-semibold text-txt">
          {title}
        </div>
        <div id={descId} className="mt-2 text-xs leading-relaxed text-muted">
          {message}
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <button
            ref={cancelBtnRef}
            onClick={onCancel}
            className="rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:text-txt focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            {t("common.cancel")}
          </button>
          <button onClick={onConfirm} className={confirmBtnCls}>
            {confirmText}
          </button>
        </div>
      </div>
    </div>
  );
}
