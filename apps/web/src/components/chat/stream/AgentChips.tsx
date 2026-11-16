"use client";

// AgentChips — composer 上方的 @agent 选择 chips（自 ChatStream.tsx 机械拆出，
// 渲染逐字一致）。「推荐」标签由草稿关键词派生（render 期，同 sel 模式）。

import { useTranslations } from "next-intl";
import { agentHex } from "@/lib/theme";
import { Icon } from "@/components/icons";
import type { useGinno } from "@/lib/store";
import type { SessionMeta } from "@/lib/types";
import { useA2aPeers } from "@/lib/useA2aPeers";
import { recommendAgentId } from "../streamCore";
import { peerEndpointHint, peerTargetValue } from "./peerTarget";

export function AgentChips({
  g,
  session,
  target,
  input,
  onSelect,
  onDeliver,
}: {
  g: ReturnType<typeof useGinno>;
  session: SessionMeta | null;
  target: string | null;
  input: string;
  onSelect: (id: string | null) => void;
  /** 团队广场专用：点 chip = 插入 @mention 投递，而不是换 persona（P1-20）。 */
  onDeliver?: (agentId: string) => void;
}) {
  const tc = useTranslations("chat");
  const recAgentId = recommendAgentId(input, new Set(g.agents.map((a) => a.id)));
  // 远端 peer 候选（remote-session-design.md R3 定案 #10）：只在首页拉取
  // ——peer 是「新建对话的执行方」，会话建立后归属已定（定案 #11 不可中途
  // 切换），会话内不需要这份列表。
  const peers = useA2aPeers(!session);

  // P1-20：团队广场的语义是**投递给成员**，不是「Ask X」换 persona。
  // 此前渲染全量 agent + persona 文案，选到非成员会静默失败（deliver 解析
  // 不到该 agent 的成员实例）。改为只列本团队成员，blocked 禁选。
  const team = session?.type === "team"
    ? g.teams.find((t) => t.main_session_id === session.id)
    : undefined;
  if (team) {
    const rows = team.member_rows ?? [];
    if (!rows.length) return null;
    return (
      <>
        {rows.map((r) => {
          const a = g.agents.find((x) => x.id === r.agent_id);
          const hex = agentHex(a?.color);
          const blocked = r.status === "blocked";
          return (
            <button
              key={r.instance_id}
              disabled={blocked}
              title={blocked ? tc("team.chipBlocked") : tc("team.chipDeliverTitle")}
              onClick={() => !blocked && onDeliver?.(r.agent_id)}
              className={
                blocked
                  ? "flex cursor-not-allowed items-center gap-1.5 rounded-lg border border-line bg-card px-2.5 py-1 text-xs text-muted opacity-40"
                  : "flex items-center gap-1.5 rounded-lg border border-line bg-card px-2.5 py-1 text-xs text-muted transition-colors hover:border-line2 hover:text-txt"
              }
            >
              <Icon name={a?.icon ?? "bot"} className="h-3.5 w-3.5" style={{ color: hex }} />
              {tc("team.chipDeliver", { name: r.display_name })}
              {blocked && <span className="text-[10px]">⛔</span>}
            </button>
          );
        })}
      </>
    );
  }

  // 远端会话（定案 #11）：执行方已钉死在远端节点，本地 agent 切换 chips
  // 整行隐藏——留着只会诱导「切了等于换会话」的非法操作。
  if (session?.remote_peer) return null;

  return (
    <>
      {g.agents.map((a) => {
        const sel = (target ?? session?.agent_id) === a.id;
        const hex = agentHex(a.color);
        const rec = recAgentId === a.id && !sel;
        return (
          <button
            key={a.id}
            onClick={() => onSelect(sel ? null : a.id)}
            className={
              sel
                ? "flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs transition-colors"
                : "flex items-center gap-1.5 rounded-lg border border-line bg-card px-2.5 py-1 text-xs text-muted transition-colors hover:border-line2 hover:text-txt"
            }
            style={sel ? { borderColor: hex, background: hex + "1a", color: hex } : undefined}
          >
            <Icon name={a.icon} className="h-3.5 w-3.5" />
            {tc("agentChips.ask", { name: a.name })}
            {rec && (
              <span className="rounded-full border border-yellow/40 bg-yellow/10 px-1.5 text-[10px] leading-4 text-yellow">
                {tc("agentChips.recommended")}
              </span>
            )}
          </button>
        );
      })}
      {/* 远端 peer 与本地 agent 并列（定案 #10）：机器图标 + 地址提示，
          一眼看出「这轮在哪台机器跑」。仅首页（!session）——选中后随
          createAndSend 以 remote_peer 建会话；target 用 peer:<slug> 前缀
          与本地 agent id 区分（peerTarget.ts）。 */}
      {!session &&
        peers
          .filter((p) => p.enabled)
          .map((p) => {
            const val = peerTargetValue(p.slug);
            const sel = target === val;
            return (
              <button
                key={p.slug}
                data-testid={`remote-peer-chip-${p.slug}`}
                onClick={() => onSelect(sel ? null : val)}
                title={tc("remote-peer-chip-title", { name: p.name, endpoint: p.endpoint })}
                className={
                  sel
                    ? "flex items-center gap-1.5 rounded-lg border border-violet/60 bg-violet/10 px-2.5 py-1 text-xs text-violet transition-colors"
                    : "flex items-center gap-1.5 rounded-lg border border-line bg-card px-2.5 py-1 text-xs text-muted transition-colors hover:border-line2 hover:text-txt"
                }
              >
                <Icon name="server" className="h-3.5 w-3.5" />
                {tc("remote-peer-chip-ask", { name: p.name })}
                <span className="max-w-[14rem] truncate font-mono text-[10px] text-faint">
                  {peerEndpointHint(p.endpoint)}
                </span>
              </button>
            );
          })}
    </>
  );
}
