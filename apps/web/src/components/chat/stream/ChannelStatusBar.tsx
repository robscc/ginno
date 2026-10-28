"use client";

// ChannelStatusBar — 渠道会话（type="channel"，钉钉/企微）composer 槽位的
// 只读状态栏：对话发生在 IM 侧，桌面端不回话；但 agent chips、模型 chip、
// TopBar 挂载 chip 照常可用——改动对渠道的下一条消息生效
// （run_channel_turn 每 turn 现读 session.agent_id；模型热切/挂载重建同）。
// 仿 DelegationStatusBar 的槽位替换模式；modelChip 由 ChatStream 传入
// （ModelChipMenu 的 props 都在那一层，这里不重复接线）。

import { MessageSquareLock } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";
import type { SessionMeta } from "@/lib/types";

const CHANNEL_NAMES: Record<string, string> = {
  wecom: "WeCom",
  dingtalk: "DingTalk",
};

export function ChannelStatusBar({
  session,
  modelChip,
}: {
  session: SessionMeta;
  /** ModelChipMenu 元素（ChatStream 构建，保持模型热切能力）。 */
  modelChip: ReactNode;
}) {
  const tc = useTranslations("chat");
  const name = CHANNEL_NAMES[session.channel ?? ""] ?? tc("composer.channelGeneric");
  return (
    <div className="flex items-center gap-2 rounded-2xl border border-line bg-card px-3 py-2.5 text-xs text-faint">
      <MessageSquareLock className="h-4 w-4 shrink-0 text-muted" />
      <div className="min-w-0 flex-1 leading-relaxed">
        <span className="text-txt">{tc("composer.channelReadonly", { channel: name })}</span>
        <span className="mx-1.5 text-line2">·</span>
        {tc("composer.channelHint")}
      </div>
      {/* 模型热切对渠道会话同样生效（下一次 LLM 调用即切换） */}
      <span className="flex shrink-0 items-center">{modelChip}</span>
    </div>
  );
}
