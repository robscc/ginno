"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Bell } from "lucide-react";
import { subscribeUnreadCount } from "@/lib/notice";
import { useGinno } from "@/lib/store";

/**
 * 顶栏通知铃铛（notice-unification-design.md §3 P1）：badge = 通知未读数
 * （notice store，countInBadge:false 的 session 投影不计）+ 会话未读数
 * （session store 的 unreadSessions——单一事实源，§3.3 防双算），两个
 * store 独立 emit、各重算一次，最终值幂等一致。
 *
 * 点击打开右栏收件箱 tab（RightTab 范式，与 RightDock openTab 同款）。
 */
export function NoticeBell() {
  const g = useGinno();
  const t = useTranslations("shell.topbar");
  const [noticeUnread, setNoticeUnread] = useState(0);
  useEffect(() => subscribeUnreadCount(setNoticeUnread), []);
  const sessionUnread = Object.keys(g.unreadSessions).length;
  const total = noticeUnread + sessionUnread;

  const openInbox = () => {
    g.setRightTab("notice", { manual: true });
    g.setRightPanelOpen(true);
  };

  return (
    <button
      onClick={openInbox}
      aria-label={t("noticeBell", { n: total })}
      title={t("noticeBell", { n: total })}
      className="relative rounded-lg p-1.5 text-muted transition-colors hover:bg-card hover:text-txt"
    >
      <Bell className="h-4 w-4" />
      {total > 0 && (
        <span className="absolute -right-0.5 -top-0.5 inline-flex h-4 min-w-[16px] items-center justify-center rounded-full bg-red px-1 text-[10px] font-semibold leading-none text-white">
          {total > 99 ? "99+" : total}
        </span>
      )}
    </button>
  );
}
