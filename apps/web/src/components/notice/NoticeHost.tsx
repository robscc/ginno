"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { CircleCheck, Info, Octagon, TriangleAlert, X } from "lucide-react";
import {
  dismissNotice,
  pauseNotice,
  resumeNotice,
  subscribeNotices,
  type Notice,
  type NoticeLevel,
} from "@/lib/notice";

/**
 * 全局通知中心宿主（notice-center-design.md §3/§4；右下角统一收编见
 * notice-unification-design.md §2.2/§4 P0）。
 *
 * 挂载点：AppShell 顶层的右下角统一容器（`fixed bottom-6 right-6 z-50`，
 * 与 DevSwapCard 持久卡同容器堆叠避让）——定位类由容器持有，本组件只是
 * 容器内的流式子元素，因此 /settings、/kb、/workflows 等全路由可见。
 *
 * 旧 mods 子系统的右下 toast（chat/mod/ModToastHost）已退役：`mod.toast`
 * 帧由 engine 直接 `pushNotice(source:"mod")` 进本总线（§4 收编表）。
 */

const LEVEL_META: Record<
  NoticeLevel,
  { icon: typeof Info; bar: string; fg: string; iconBg: string }
> = {
  info: { icon: Info, bar: "bg-blue", fg: "text-blue-400", iconBg: "bg-blue/15" },
  success: { icon: CircleCheck, bar: "bg-green", fg: "text-green-400", iconBg: "bg-green/15" },
  warn: { icon: TriangleAlert, bar: "bg-yellow", fg: "text-yellow-400", iconBg: "bg-yellow/15" },
  error: { icon: Octagon, bar: "bg-red", fg: "text-red-400", iconBg: "bg-red/15" },
};

function NoticeCard({ n }: { n: Notice }) {
  const t = useTranslations("chat");
  const meta = LEVEL_META[n.level] ?? LEVEL_META.info;
  const Icon = meta.icon;
  return (
    <div
      role={n.level === "error" ? "alert" : "status"}
      aria-live={n.level === "error" ? "assertive" : "polite"}
      onMouseEnter={() => pauseNotice(n.id)}
      onMouseLeave={() => resumeNotice(n.id)}
      className={`pointer-events-auto flex w-[340px] gap-2.5 overflow-hidden rounded-xl border border-line bg-card2 py-2.5 pl-0 pr-2.5 text-xs leading-relaxed text-txt shadow-lg transition-all duration-[180ms] ease-out ${
        n.entered ? "translate-y-0 opacity-100" : "translate-y-1 opacity-0"
      }`}
    >
      {/* 左侧 2px 语义色条 */}
      <span className={`w-0.5 shrink-0 self-stretch rounded-r ${meta.bar}`} />
      <span className={`mt-px flex h-4 w-4 shrink-0 items-center justify-center rounded-full ${meta.iconBg}`}>
        <Icon className={`h-3 w-3 ${meta.fg}`} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="selectable break-words whitespace-pre-wrap">{n.text}</div>
        {n.action && (
          <button
            onClick={() => {
              n.action!.onClick();
              dismissNotice(n.id);
            }}
            className="mt-1.5 rounded-md border border-line2 px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-violet/60 hover:text-txt"
          >
            {n.action.label}
          </button>
        )}
      </div>
      <button
        onClick={() => dismissNotice(n.id)}
        aria-label={t("notice.dismiss")}
        title={t("notice.dismiss")}
        className="mt-px shrink-0 rounded p-0.5 text-faint transition-colors hover:text-txt"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

export function NoticeHost() {
  const [items, setItems] = useState<Notice[]>([]);

  useEffect(() => subscribeNotices(setItems), []);

  // Esc 关闭最上一条；输入框内不拦截（不抢用户的 Esc）。
  useEffect(() => {
    if (items.length === 0) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const el = e.target as HTMLElement | null;
      if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
      dismissNotice(items[items.length - 1].id);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items]);

  if (items.length === 0) return null;
  return (
    // pointer-events-none：不挡住底下的内容，每张卡片自己恢复
    // pointer-events-auto；定位（fixed 右下角）由 AppShell 的统一容器持有。
    <div className="pointer-events-none flex flex-col items-end gap-2">
      {items.map((n) => (
        <NoticeCard key={n.id} n={n} />
      ))}
    </div>
  );
}