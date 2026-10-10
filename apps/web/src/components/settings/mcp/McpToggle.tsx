"use client";

import { Loader2 } from "lucide-react";

// iOS 风格启停开关（原型 v2 .sw）：32×19 轨道 + 13px 滑块，选中态品牌紫。
// pending 态（确认驱动开关，见 McpSettings.toggleServer）：控件禁用防连点，
// 滑块保持在目标位置并半透明呼吸（方向可见——用户看得出开关正在打开还是
// 关闭），轨道空着的一侧叠一个轻量 spinner，直到刷新到的 status 确认后翻转。
// 滑块位移用 transform（GPU 合成，不触发 layout；left 过渡在低端机上会抖）。
// 未选中滑块用深色（bg-txt/75）：浅色主题的 line2 是 212 灰，白滑块几乎隐形。
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
      className={`relative h-[19px] w-8 shrink-0 rounded-full outline-none transition-colors focus-visible:ring-2 focus-visible:ring-indigo/60 ${
        checked ? (pending ? "bg-indigo/50" : "bg-indigo") : "bg-line2"
      } ${pending ? "cursor-wait" : "cursor-pointer"}`}
    >
      <span
        aria-hidden
        className={`absolute left-[3px] top-[3px] h-[13px] w-[13px] rounded-full transition-transform duration-150 ease-out ${
          checked ? "translate-x-[13px] bg-white" : "translate-x-0 bg-txt/75"
        } ${pending ? "opacity-70 animate-pulse" : ""}`}
      />
      {/* spinner 落在轨道空着的一侧（与滑块互补），不与滑块重叠 */}
      {pending && (
        <Loader2
          className={`absolute top-1/2 h-3 w-3 -translate-y-1/2 animate-spin ${
            checked ? "left-[4px] text-white/90" : "right-[4px] text-txt/60"
          }`}
        />
      )}
    </button>
  );
}
