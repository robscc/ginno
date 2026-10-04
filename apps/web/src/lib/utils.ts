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
