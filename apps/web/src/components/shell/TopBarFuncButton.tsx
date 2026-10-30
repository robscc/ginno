"use client";

/** TopBar 统一功能按钮(topbar-unification.md §2.2)。

一个模子:32px 方形 ghost 图标按钮,右上角计数/状态徽标,hover = 原生
title tooltip(全站惯例),click 由调用方接管(通常开同宽 popover)。
「状态位」类按钮没有面板 —— 传 `onClick=undefined` 时渲染非交互 div,
避免空点按的手势暗示。 */

import type { CSSProperties, ReactNode } from "react";

export function TopBarFuncButton({
  onClick,
  open,
  title,
  count,
  dot,
  active,
  live,
  children,
  className = "",
}: {
  /** 缺省 = 纯状态位(div,不可点)。 */
  onClick?: () => void;
  /** 面板开合态(aria-expanded + 视觉激活)。 */
  open?: boolean;
  /** 原生 tooltip = 按钮语义 + 关键数据(hover 展示)。 */
  title: string;
  /** 计数徽标(>0 才渲染;0 是合法的「空」态,不占视觉)。 */
  count?: number;
  /** 状态点徽标(hex 颜色;与 count 互斥,count 优先)。 */
  dot?: string;
  /** 激活色(有内容/进行中时图标着色;空态 text-muted)。 */
  active?: string;
  /** 呼吸描边(仅状态按钮 running 时)。 */
  live?: boolean;
  children: ReactNode;
  className?: string;
}) {
  const style: CSSProperties | undefined = active || open ? { color: active || "rgb(var(--txt))" } : undefined;
  const badge =
    count != null && count > 0 ? (
      <span className="absolute right-0 top-0 flex h-3.5 min-w-3.5 items-center justify-center rounded-full px-0.5 text-[9px] font-bold leading-none text-black" style={{ background: "var(--st-running)" }}>
        {count > 99 ? "99+" : count}
      </span>
    ) : dot ? (
      <span className="absolute right-1 top-1 h-1.5 w-1.5 rounded-full" style={{ background: dot }} />
    ) : null;

  const body = (
    <>
      {children}
      {badge}
      {live && (
        <span className="pointer-events-none absolute inset-0 rounded-lg ring-1 ring-[var(--st-running)] opacity-60 motion-safe:animate-pulse" />
      )}
    </>
  );

  if (!onClick) {
    return (
      <div title={title} aria-label={title} className={`relative flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-muted ${className}`} style={style}>
        {body}
      </div>
    );
  }
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-label={title}
      aria-haspopup={open !== undefined ? "dialog" : undefined}
      aria-expanded={open}
      className={`relative flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-muted transition-colors hover:bg-card hover:text-txt ${open ? "bg-card text-txt" : ""} ${className}`}
      style={style}
    >
      {body}
    </button>
  );
}
