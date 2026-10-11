"use client";

// 未读角标（schedule-unseen）：实心圆 + 白色数字——经典通知徽章形。
// 侧栏定时任务入口与执行记录页签共用同一视觉。0 不渲染；>99 收敛为 99+
// （徽章宽度有界，三位数挤进 16px 会糊）。
//
// 颜色语义（2026-10-11 用户裁定）：红只留给「坏消息」——未读里有 error /
// missed 才红；纯成功未读用品牌紫，避免「一切正常也一片红」的误导。
export function UnseenBadge({
  n,
  tone = "red",
  title,
  className = "",
}: {
  n: number;
  /** red = 未读含 error/missed；violet = 未读全是成功/跳过 */
  tone?: "red" | "violet";
  title?: string;
  className?: string;
}) {
  if (n <= 0) return null;
  const label = n > 99 ? "99+" : String(n);
  return (
    <span
      title={title}
      className={`inline-flex h-4 min-w-4 shrink-0 items-center justify-center rounded-full px-1 text-[10px] font-semibold leading-none text-white tabular-nums ${
        tone === "red" ? "bg-red" : "bg-violet"
      } ${className}`}
    >
      {label}
    </span>
  );
}
