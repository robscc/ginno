"use client";

// 未读角标（schedule-unseen）：实心红圆 + 白色数字——经典通知徽章形。
// 侧栏定时任务入口与执行记录页签共用同一视觉。0 不渲染；>99 收敛为 99+
// （徽章宽度有界，三位数挤进 16px 会糊）。
export function UnseenBadge({
  n,
  title,
  className = "",
}: {
  n: number;
  title?: string;
  className?: string;
}) {
  if (n <= 0) return null;
  const label = n > 99 ? "99+" : String(n);
  return (
    <span
      title={title}
      className={`inline-flex h-4 min-w-4 shrink-0 items-center justify-center rounded-full bg-red px-1 text-[10px] font-semibold leading-none text-white tabular-nums ${className}`}
    >
      {label}
    </span>
  );
}
