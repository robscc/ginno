"use client";

// TeamReplyCard — AgentTeam 主会话里的「成员回帖卡」（agent-team-design.md
// §5.2 要点 2）。成员在自己的 session 跑完，产出以**结构化回帖**写回主会话
// checkpoint（AIMessage.additional_kwargs.ginno_team_reply），前端据此渲染成
// 可折叠卡片而非普通气泡：
//   · 头像 + 成员名 + 状态徽标（归属一眼可见，与 AssistantBubble 同视觉语言）
//   · 折叠摘要（默认）/ 展开全文
//   · 「进入工作台」跳该成员的完整 session（全文永远在成员 session，主会话只留摘要）
//
// 跳转用自定义事件解耦：回帖卡深埋在 ChatStream → TranscriptList 里，逐层透传
// 回调要改 3 个组件签名；/team 页监听 window 事件即可（与 ginno:focus-latest
// 同款先例，AppShell.tsx）。
//
// backlog P2-25：事件只有 /team 的 TeamShell 监听——其它路由点「进入工作台」
// 会静默丢失。现在不在 /team 时先把意图存 sessionStorage 再 router.push("/team")，
// TeamShell 挂载时消费；同路由仍走 window 事件（零延迟）。
//
// backlog P2-14：折叠头重构为两个**平级**按钮——旧实现把 span role=button
// 嵌在 button 内（HTML 交互内容嵌套非法，键盘/读屏双重混乱）。状态/耗时
// meta 未补：ginno_team_reply 现有字段只有 team/instance/session/name/truncated，
// 无 status/elapsed，新增字段属 runtime 改动（本批次边界外，见交付报告）。

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ChevronDown, ChevronRight, ExternalLink } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import { agentHex } from "@/lib/theme";
import type { TeamReplyMeta } from "@/lib/types";
import type { Block } from "@/components/chat/blocks";
import { InnerBlocks, RefBlocks } from "@/components/chat/blocks";
import { stashOpenMemberIntent } from "@/components/team/openMemberIntent";

/** 请求打开某成员的工作台（/team 页监听）。teamId 可选：跨团队跳转时
 *  TeamShell 据此先选中团队再进工作台。 */
export function requestOpenMemberWorkspace(
  sessionId: string,
  instanceId: string,
  teamId?: string,
) {
  window.dispatchEvent(
    new CustomEvent("ginno:open-team-member", {
      detail: { sessionId, instanceId, teamId },
    }),
  );
}

export function TeamReplyCard({
  meta,
  agentId,
  blocks,
}: {
  meta: TeamReplyMeta;
  /** 回帖成员的 agent_id（来自消息的 agentId，additional_kwargs.agent_id）。 */
  agentId?: string | null;
  blocks: Block[];
}) {
  const g = useGinno();
  const tc = useTranslations("chat");
  const router = useRouter();
  // 默认展开（2026-11-15 产品决定）：回帖不再截断，直接显示全文。
  // 保留折叠开关供用户主动收起长回帖，但初值是「全文可见」。
  const [open, setOpen] = useState(true);
  // 回帖的 agent 用 agent_id 反查配色/图标（与主对话归属徽标同源）。
  const agent = g.agents.find((a) => a.id === agentId) ?? null;
  // 回帖卡的颜色取成员对应 agent 的色；查不到用中性蓝。
  const hex = agentHex(agent?.color ?? "blue");
  const name = meta.display_name || agent?.name || tc("stream.fallbackAgent");
  const hasInner = blocks.some((b) => b.kind !== "ref");

  const openWorkspace = () => {
    // P2-25：不在 /team → 先存一次性意图再导航（TeamShell 挂载时消费）；
    // 已在 /team → 只发事件，避免留下会在下次挂载时误重放的陈旧意图。
    if (window.location.pathname !== "/team") {
      stashOpenMemberIntent({
        teamId: meta.team_id,
        instanceId: meta.instance_id,
        sessionId: meta.member_session_id,
      });
      router.push("/team");
    }
    requestOpenMemberWorkspace(meta.member_session_id, meta.instance_id, meta.team_id);
  };

  return (
    <div className="min-w-0">
      {/* 归属行：成员色 dot + 名字 + 「回帖」徽标（区分普通 assistant 气泡） */}
      <div className="mb-1 flex items-center gap-2">
        <span
          className="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium"
          style={{ borderColor: hex + "55", background: hex + "1a", color: hex }}
          title={name}
        >
          <span className="h-1.5 w-1.5 rounded-full" style={{ background: hex }} />
          {name}
        </span>
        <span
          className="rounded-full border border-line px-1.5 py-0.5 text-[10px] text-faint"
          title={tc("team.replyHint")}
        >
          {tc("team.replyBadge")}
        </span>
      </div>

      <div className="rounded-xl border border-line bg-card text-sm leading-relaxed text-txt">
        {/* 折叠头（P2-14）：折叠开关与「进入工作台」是两个平级按钮，
            不再 button 套 role=button。摘要截断时提示全文在工作台。 */}
        <div className="flex w-full items-center gap-2 px-4 py-2">
          <button
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            className="flex min-w-0 flex-1 items-center gap-2 text-left"
          >
            {open ? (
              <ChevronDown className="h-3.5 w-3.5 shrink-0 text-faint" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5 shrink-0 text-faint" />
            )}
            <span className="min-w-0 flex-1 truncate text-xs text-muted">
              {open ? tc("team.collapse") : tc("team.expandFull")}
            </span>
            {meta.truncated && (
              <span className="shrink-0 text-[10px] text-faint">{tc("team.truncated")}</span>
            )}
          </button>
          <button
            onClick={openWorkspace}
            className="flex shrink-0 items-center gap-1 rounded-md border border-line2 px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-violet/50 hover:text-violet"
            title={tc("team.openWorkspace")}
          >
            <ExternalLink className="h-3 w-3" />
            {tc("team.openWorkspace")}
          </button>
        </div>

        {/* 正文：默认全文展示（不截断）；用户主动收起时才 clamp。 */}
        <div className={`border-t border-line px-4 py-3 ${open ? "" : "line-clamp-2"}`}>
          {hasInner ? (
            <InnerBlocks blocks={blocks} streaming={false} />
          ) : (
            <span className="text-xs text-faint">{tc("stream.emptyReply")}</span>
          )}
        </div>
        <RefBlocks blocks={blocks} />
      </div>
    </div>
  );
}
