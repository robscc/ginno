"use client";

import { Loader2 } from "lucide-react";

// iOS 风格启停开关（原型 v2 .sw）：32×19 轨道 + 13px 滑块，选中态品牌紫。
// pending 态（确认驱动开关，见 McpSettings.toggleServer）：控件禁用防连点，
// 轨道居中一个轻量 spinner，直到刷新到的 status 确认后才翻转。
export function McpToggle({
  checked,
  onChange,
  label,
  pending,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  pending?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-busy={pending || undefined}
      aria-label={label}
      disabled={pending}
      onClick={() => onChange(!checked)}
      className={`relative h-[19px] w-8 shrink-0 rounded-full transition-colors ${
        pending ? "cursor-wait bg-line2 opacity-70" : checked ? "bg-indigo" : "bg-line2"
      }`}
    >
      {pending ? (
        <Loader2 className="absolute left-1/2 top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 animate-spin text-muted" />
      ) : (
        <span
          className={`absolute top-[3px] h-[13px] w-[13px] rounded-full transition-all ${
            checked ? "left-[16px] bg-white" : "left-[3px] bg-txt/75"
          }`}
        />
      )}
    </button>
  );
}
