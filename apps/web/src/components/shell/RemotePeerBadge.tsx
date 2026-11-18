"use client";

// RemotePeerBadge — 会话头的远端归属徽标（remote-session-design.md R3）。
// 会话挂 remote_peer 时一眼可见「这个会话在哪台机器跑」：机器图标 +
// 「远端 · <name>」，tooltip 给完整 endpoint 与「中途不可切换」语义
// （定案 #11）。peer 名从 GET /api/a2a/peers 解析（脱敏读面），解析不到
// （peer 已被删除/对端列表不可达）回落显示 slug——归属事实不因列表缺失
// 而消失。

import { useTranslations } from "next-intl";
import { Icon } from "@/components/icons";
import { useA2aPeers } from "@/lib/useA2aPeers";

export function RemotePeerBadge({
  slug,
  resolvedName,
}: {
  slug: string;
  /** session 载荷自带的显示名（后端 PeerStore 真源解析）——优先于本地
   *  peers 查找，消除异步拉取落定前 {name} 闪 slug 的窗口（与
   *  ModelChipMenu 同修，i18n 错位竞态根因见 sessions.py）。 */
  resolvedName?: string | null;
}) {
  const tr = useTranslations("shell");
  const peers = useA2aPeers(true);
  const peer = peers.find((p) => p.slug === slug);
  const name = resolvedName || peer?.name || slug;
  const endpoint = peer?.endpoint ?? "";
  return (
    <span
      data-testid="remote-peer-badge"
      title={
        endpoint
          ? tr("remote-badge-title", { name, endpoint })
          : tr("remote-badge-title-noendpoint", { name })
      }
      className="flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full border border-violet/40 bg-violet/10 px-2 py-px text-[10px] leading-4 text-violet"
    >
      <Icon name="server" className="h-3 w-3" />
      {tr("remote-badge", { name })}
    </span>
  );
}