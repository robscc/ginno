"use client";

/**
 * 鸭子状态动画图标（子会话三档节头）——docs/design/prototypes/
 * duck-status-prototype.html 定稿（2026-10-12）：
 *   running 翻滚彩虹鸭 —— 彩虹渐变 + 5s 缓慢色相流转，鸭身不停向前
 *           翻滚、水波加速溅开，「卖力干活」的直觉。
 *   done    领奖薄荷鸭 —— 对勾徽章呼吸发光，鸭身满足地轻晃。
 *   failed  翻肚珊瑚鸭 —— 四脚朝天漂在水面缓缓打转（仰泳式放弃）。
 * 鸭身复用品牌鸭单路径（与 icons.tsx 的 Duck 同源）；腮红 + 星星眼是
 * 共用的可爱语言，跟随鸭组一起翻转（翻肚鸭的腮红也是倒挂的）。
 * 动画全部走 CSS（keyframes 在 globals.css 的 `ginno-duck` 段，按
 * prefers-reduced-motion 全停但渐变保留——不动的彩虹仍是彩虹，颜色
 * 始终能分档）。渐变 id 用 useId 保证多实例不串。
 */
import { useId } from "react";
import type { SubagentBucket } from "./types";

const DUCK_D =
  "M8.5,5A1.5,1.5 0 0,0 7,6.5A1.5,1.5 0 0,0 8.5,8A1.5,1.5 0 0,0 10,6.5A1.5,1.5 0 0,0 8.5,5M10,2A5,5 0 0,1 15,7C15,8.7 14.15,10.2 12.86,11.1C14.44,11.25 16.22,11.61 18,12.5C21,14 22,12 22,12C22,12 21,21 15,21H9C9,21 4,21 4,16C4,13 7,12 6,10C2,10 2,6.5 2,6.5C3,7 4.24,7 5,6.65C5.19,4.05 7.36,2 10,2Z";

/** 马卡龙渐变：running 斜铺彩虹，done/failed 单色系纵向（浅→深）。 */
const GRADIENTS: Record<
  SubagentBucket,
  { x2: number; y2: number; stops: [number, string][] }
> = {
  running: {
    x2: 1,
    y2: 1,
    stops: [
      [0, "#ff9d9d"],
      [0.2, "#ffc98a"],
      [0.4, "#ffeb9c"],
      [0.6, "#9fe8b8"],
      [0.8, "#8fd4f2"],
      [1, "#c9b6f7"],
    ],
  },
  done: { x2: 0, y2: 1, stops: [[0, "#c8f8de"], [1, "#57d69f"]] },
  failed: { x2: 0, y2: 1, stops: [[0, "#ffd4ca"], [1, "#ff9b9b"]] },
};

/** 鸭身 + 腮红 + 星星眼。pathClass 只在 running 上用（色相流转加在
 *  鸭身路径上而不是整组——腮红要keep樱花粉，不能跟着彩虹转色）。 */
function DuckBody({ fill, pathClass }: { fill: string; pathClass?: string }) {
  return (
    <>
      <path className={pathClass} d={DUCK_D} fill={fill} />
      <ellipse cx="9.4" cy="9.1" rx=".95" ry=".55" fill="#ffa1c9" opacity=".85" />
      <circle cx="8.1" cy="6.1" r=".5" fill="#fff" opacity=".95" />
    </>
  );
}

export function DuckStatusIcon({
  status,
  size = 16,
  className,
}: {
  status: SubagentBucket;
  size?: number;
  className?: string;
}) {
  // useId 带 «»:» 等字符，url(#…) 引用会炸——滤成纯字母数字。
  const gid = `duck-grad-${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const grad = GRADIENTS[status];
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      className={`ginno-duck ${className ?? ""}`}
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2={grad.x2} y2={grad.y2}>
          {grad.stops.map(([offset, color]) => (
            <stop key={offset} offset={offset} stopColor={color} />
          ))}
        </linearGradient>
      </defs>
      {status === "running" ? (
        <>
          {/* 卖力扑腾：水波溅得比稳态快（1.2s 交替） */}
          <ellipse className="gd-ripple gd-r1" cx="12" cy="19.5" rx="7" ry="1.8" fill="none" stroke="#8fd4f2" strokeWidth="1.2" />
          <ellipse className="gd-ripple gd-r2" cx="12" cy="19.5" rx="7" ry="1.8" fill="none" stroke="#8fd4f2" strokeWidth="1.2" />
          <g className="gd-roll">
            <DuckBody fill={`url(#${gid})`} pathClass="gd-hue" />
          </g>
        </>
      ) : status === "done" ? (
        <>
          <g className="gd-sway">
            <DuckBody fill={`url(#${gid})`} />
          </g>
          <g className="gd-badge">
            <circle cx="18" cy="5.5" r="3.2" fill="#3fd68f" opacity=".22" />
            <circle cx="18" cy="5.5" r="3.2" fill="none" stroke="#3fd68f" strokeWidth="1.3" />
            <path
              d="M16.7 5.5l1 1.05 1.6-1.85"
              fill="none"
              stroke="#3fd68f"
              strokeWidth="1.4"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </g>
        </>
      ) : (
        <>
          <ellipse className="gd-ripple gd-fr1" cx="12" cy="20" rx="7.5" ry="1.8" fill="none" stroke="#8fd4f2" strokeWidth="1.2" />
          <ellipse className="gd-ripple gd-fr2" cx="12" cy="20" rx="7.5" ry="1.8" fill="none" stroke="#8fd4f2" strokeWidth="1.2" />
          <g className="gd-float">
            <DuckBody fill={`url(#${gid})`} />
          </g>
        </>
      )}
    </svg>
  );
}
