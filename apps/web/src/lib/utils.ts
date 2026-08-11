import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Compact relative time (sidebar session rows, draft banner). */
export function relTime(tsSeconds: number): string {
  const diff = Math.max(0, Date.now() / 1000 - tsSeconds);
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

/** Time-of-day greeting for the landing home (prototype A). */
export function greeting(): string {
  const h = new Date().getHours();
  const part = h < 6 ? "夜深了" : h < 12 ? "早上好" : h < 14 ? "中午好" : h < 18 ? "下午好" : "晚上好";
  return `${part},今天想做点什么?`;
}
