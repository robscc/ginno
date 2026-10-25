import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import { uiText } from "../i18n/uiText";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Compact relative time (sidebar session rows, draft banner). */
export function relTime(tsSeconds: number): string {
  const diff = Math.max(0, Date.now() / 1000 - tsSeconds);
  if (diff < 60) return uiText("relTime.justNow");
  if (diff < 3600) return uiText("relTime.minAgo", { n: Math.floor(diff / 60) });
  if (diff < 86400) return uiText("relTime.hrAgo", { n: Math.floor(diff / 3600) });
  return uiText("relTime.dAgo", { n: Math.floor(diff / 86400) });
}

/** Time-of-day greeting for the landing home (prototype A). */
export function greeting(): string {
  const h = new Date().getHours();
  const part =
    h < 6
      ? uiText("greeting.lateNight")
      : h < 12
        ? uiText("greeting.morning")
        : h < 18
          ? uiText("greeting.afternoon")
          : uiText("greeting.evening");
  return uiText("greeting.prompt", { part });
}

/**
 * 本次 click 是不是「拖选正文后松开鼠标」的尾巴？
 * mouseup 之后浏览器照样会给可点击的行/卡片派发 click，如果此刻的选区就落在
 * 该元素内，说明用户是在选文本而不是点开它——可点击的内容行（执行记录、
 * 用量表、工作流日志）先用它挡一下，避免一拖选就顺带跳转/展开。
 * 只看 anchor：选区从别处拖过来、落在本行之外时仍然放行点击。
 */
export function clickedWhileSelecting(ev: { currentTarget: EventTarget & HTMLElement }): boolean {
  if (typeof window === "undefined") return false;
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.anchorNode) return false;
  const anchorEl =
    sel.anchorNode.nodeType === Node.TEXT_NODE ? sel.anchorNode.parentElement : (sel.anchorNode as Element | null);
  return !!anchorEl && ev.currentTarget.contains(anchorEl);
}
