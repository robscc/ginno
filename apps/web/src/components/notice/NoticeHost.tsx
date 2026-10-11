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
 * 全局通知中心宿主（notice-center-design.md §3/§4）。
 *
 * 挂载点：AppShell 的工作区列（TopBar 之下）——`absolute right-3 top-2`，
 * 因此**不会覆盖右侧栏**（RightPanel/RightDock 是该列的兄弟节点），也不占用
 * TopBar 右侧的 usage / ⋮ 控件区。非工作区路由（Settings/KB/Workflows）的模态
 * 层级更高（z-50+），通知被压在下面属预期，不参与竞争。
 *
 * 旧实现（模型热切提示）住在 `chat/mod/ModToastHost`——mods 子系统的右下角
 * 载体，语义错位且 mods 未挂载时帧直接丢弃。这里改为跨视图的公共层。
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
        n.entered ? "translate-y-0 opacity-100" : "-translate-y-1 opacity-0"
      }`}
    >
      {/* 左侧 2px 语义色条 */}
      <span className={`w-0.5 shrink-0 self-stretch rounded-r ${meta.bar}`} />
      <span className={`mt-px flex h-4 w-4 shrink-0 items-center justify-center rounded-full ${meta.iconBg}`}>
        <Icon className={`h-3 w-3 ${meta.fg}`} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="break-words whitespace-pre-wrap">{n.text}</div>
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
    // pointer-events-none：容器只是定位壳，不挡住底下的聊天内容；
    // 每张卡片自己恢复 pointer-events-auto。
    <div className="pointer-events-none absolute right-3 top-2 z-30 flex flex-col items-end gap-2">
      {items.map((n) => (
        <NoticeCard key={n.id} n={n} />
      ))}
    </div>
  );
}