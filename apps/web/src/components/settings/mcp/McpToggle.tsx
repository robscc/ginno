"use client";

import { Loader2 } from "lucide-react";

// iOS 风格启停开关（原型 v2 .sw）：32×19 轨道 + 13px 滑块，选中态品牌紫。
// pending 态（确认驱动开关，见 McpSettings.toggleServer）：控件禁用防连点，
// 滑块停在「翻转前」的位置半透明呼吸——不做乐观位移；spinner 落在目标侧
// （轨道空着的一侧）指示方向，status 确认后 pending 清除、滑块才滑动。
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
        checked ? "bg-indigo" : "bg-line2"
      } ${pending ? "cursor-wait opacity-80" : "cursor-pointer"}`}
    >
      <span
        aria-hidden
        className={`absolute left-[3px] top-[3px] h-[13px] w-[13px] rounded-full transition-transform duration-150 ease-out ${
          checked ? "translate-x-[13px] bg-white" : "translate-x-0 bg-txt/75"
        } ${pending ? "opacity-70 animate-pulse" : ""}`}
      />
      {/* spinner 落在轨道空着的一侧（= 目标侧），不与滑块重叠。
          定位与旋转必须分层：animate-spin 的 keyframes 覆盖整个 transform，
          与 -translate-y-1/2 叠在同一元素上会把定位插值掉——spinner 一边转
          一边漂移。外层 span 只管定位，内层 icon 只管转。 */}
      {pending && (
        <span
          aria-hidden
          className={`absolute top-[3px] flex h-[13px] w-[13px] items-center justify-center ${
            checked ? "left-[3px]" : "right-[3px]"
          }`}
        >
          <Loader2 className={`h-3 w-3 animate-spin ${checked ? "text-white/90" : "text-txt/60"}`} />
        </span>
      )}
    </button>
  );
}
