"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { CheckCheck, MessageSquare } from "lucide-react";
import {
  markAllNoticesRead,
  markNoticeRead,
  subscribeInbox,
  subscribeUnreadCount,
  type InboxEntry,
} from "@/lib/notice";
import { useGinno } from "@/lib/store";
import { LEVEL_META } from "./NoticeHost";

/**
 * 收件箱面板（notice-unification-design.md §3 P1）：右栏 notice tab。
 *
 * 列表 = notice 总线的收件箱视图（最近 100 条滚动窗口，最新在前）：
 *  - 单条点击 = 已读触发③——markNoticeRead + 执行条目行为（session 投影
 *    条目 → 打开对应会话，见 §3.3 双写：打开动作本身会让 session store
 *    清未读，两侧幂等收敛）；
 *  - 「全部已读」= 触发④；
 *  - toast 超时自动消失的条目保留在此（未读态），与手机通知中心同语义。
 */

function entryTime(ts: number): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return "";
  }
}

function InboxRow({ e, onOpen }: { e: InboxEntry; onOpen: (e: InboxEntry) => void }) {
  const t = useTranslations("right.notice");
  const meta = LEVEL_META[e.level] ?? LEVEL_META.info;
  const Icon = meta.icon;
  const unread = e.readAt === null;
  const isSession = e.kind === "session";
  return (
    <button
      onClick={() => onOpen(e)}
      aria-label={`${e.text}${unread ? ` (${t("unread")})` : ""}`}
      className={`flex w-full items-start gap-2 rounded-lg px-2 py-2 text-left transition-colors hover:bg-card2 ${
        unread ? "bg-card/60" : "opacity-70"
      }`}
    >
      <span className={`mt-px flex h-4 w-4 shrink-0 items-center justify-center rounded-full ${meta.iconBg}`}>
        <Icon className={`h-3 w-3 ${meta.fg}`} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="selectable block break-words text-xs leading-relaxed text-txt">
          {e.text}
        </span>
        <span className="mt-0.5 flex items-center gap-1.5 text-[10px] text-faint">
          {entryTime(e.createdAt)}
          {isSession && (
            <span className="inline-flex items-center gap-0.5">
              <MessageSquare className="h-2.5 w-2.5" />
              {t("sessionKind")}
            </span>
          )}
        </span>
      </span>
      {unread && (
        <span className="mt-1 inline-block h-2 w-2 shrink-0 rounded-full bg-violet" aria-label={t("unread")} />
      )}
    </button>
  );
}

export function NoticeInboxPanel() {
  const t = useTranslations("right.notice");
  const g = useGinno();
  const [entries, setEntries] = useState<InboxEntry[]>([]);
  const [unread, setUnread] = useState(0);

  useEffect(() => subscribeInbox(setEntries), []);
  useEffect(() => subscribeUnreadCount(setUnread), []);

  // 已读触发③：点条目 = 已读 + 行为（session 投影 → 打开会话；打开动作
  // 内部的 clearSessionUnread 会再 markNoticeReadByKey 一次，幂等双写）。
  const openEntry = (e: InboxEntry) => {
    markNoticeRead(e.id, e.dedupeKey);
    if (e.actionMeta?.kind === "session" && e.actionMeta.id) g.setActiveSession(e.actionMeta.id);
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center justify-between border-b border-line px-3 py-2">
        <span className="text-[11px] text-faint">
          {unread > 0 ? t("unreadCount", { n: unread }) : t("allRead")}
        </span>
        <button
          onClick={() => markAllNoticesRead()}
          disabled={unread === 0}
          aria-label={t("markAll")}
          title={t("markAll")}
          className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[11px] text-muted transition-colors hover:bg-card hover:text-txt disabled:cursor-default disabled:opacity-40"
        >
          <CheckCheck className="h-3.5 w-3.5" />
          {t("markAll")}
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
        {entries.length === 0 ? (
          <p className="px-2 py-8 text-center text-xs text-faint">{t("empty")}</p>
        ) : (
          <div className="flex flex-col gap-0.5">
            {entries.map((e) => (
              <InboxRow key={e.id} e={e} onOpen={openEntry} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
