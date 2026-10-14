"use client";

// MemberPanel — AgentTeam 主会话右侧的常驻成员面板（agent-team-design.md
// §5.1）。需求①「同时看到多个 Agent 当前状态」的主落点：每行一个成员实例，
// 显示语义态（§3.3）+ 展示文本（§3.4，纯渲染）+ elapsed + 累计 token。
//
// 状态一律来自 runtime 的 teams.status（§5.9 唯一权威来源），前端不自己判态；
// 本组件只把 TeamMemberRow 渲染出来，并做 §5.3.1 的状态上卷（团队行徽标）。
//
// 点击成员行 → onOpenMember（进成员工作台）。blocked 行高亮——「谁在等我」
// 一眼可见（herdr 的产品主张）。

import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Loader2, Plus, UserX, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import { agentHex } from "@/lib/theme";
import type { Team, TeamMemberRow, TeamMemberStatus } from "@/lib/types";

/** chat 域翻译 + 动态 key 收敛转型（本文件共用；理由见 blocks.tsx useChatT）。
 *  状态徽标文案 key 是 `team.status.${s}` 动态拼的，强类型 tc 不接受 string，
 *  故用 tr 转型版（与 RunBlocks / streamCards / TranscriptList 同款）。 */
function useChatT() {
  const tc = useTranslations("chat");
  const tr = tc as unknown as {
    (key: string, values?: Record<string, string | number>): string;
    has(key: string): boolean;
  };
  return { tc, tr };
}

/** 语义态 → 圆点颜色 + 是否脉冲（working/blocked 脉冲，其余静止）。 */
const STATUS_DOT: Record<TeamMemberStatus, { hex: string; pulse: boolean }> = {
  working: { hex: "#4f8cff", pulse: true },
  blocked: { hex: "#ffb020", pulse: true },
  waiting: { hex: "#a78bfa", pulse: false },
  done: { hex: "#34c77b", pulse: false },
  error: { hex: "#ff5c5c", pulse: false },
  unknown: { hex: "#5c6472", pulse: false },
  idle: { hex: "#5c6472", pulse: false },
};

/** 语义态 → 徽标文案 key（chat.team.status.*）。 */
function statusKey(s: TeamMemberStatus): string {
  return `team.status.${s}`;
}

function StatusDot({ status }: { status: TeamMemberStatus }) {
  const { hex, pulse } = STATUS_DOT[status] ?? STATUS_DOT.idle;
  return (
    <span
      className={`h-2 w-2 shrink-0 rounded-full ${pulse ? "animate-pulse" : ""}`}
      style={{ background: hex, boxShadow: `0 0 0 3px ${hex}22` }}
    />
  );
}

function fmtElapsed(s: number): string {
  if (!s || s < 0) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${Math.round(s % 60)}s`;
  return `${Math.floor(m / 60)}h${m % 60}m`;
}

export function MemberPanel({
  team,
  activeInstanceId,
  activeSessionId,
  onOpenMember,
  onOpenMemberSession,
  onMutate,
  onAddMember,
}: {
  team: Team;
  /** 当前打开工作台的成员（高亮该行）。 */
  activeInstanceId?: string | null;
  /** 当前打开工作台的具体会话（多会话下区分到条）。 */
  activeSessionId?: string | null;
  onOpenMember: (row: TeamMemberRow) => void;
  onOpenMemberSession: (instanceId: string, sessionId: string) => void;
  /** 会话增删后的统一刷新（团队 + 会话列表）。 */
  onMutate: () => Promise<void> | void;
  onAddMember: () => void;
}) {
  const g = useGinno();
  const { tc, tr } = useChatT();
  const allRows = team.member_rows ?? [];
  // 领队实例由置顶 👑 行展示，成员列表不再重复（2026-10-14）。
  const rows = allRows.filter((r) => r.instance_id !== team.leader_instance_id);
  const rollup = team.rollup;
  // 多会话展开态 + 新会话/删除会话的进行中标记 + 删除两步确认。
  const [openIds, setOpenIds] = useState<Record<string, boolean>>({});
  const [busySid, setBusySid] = useState<string | null>(null);
  const [confirmDelSid, setConfirmDelSid] = useState<string | null>(null);
  // elapsed 每秒走字（仅 working/blocked 需要，但便宜起见统一 tick）。
  const [, setTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);

  const agentById = (id: string) => g.agents.find((a) => a.id === id) ?? null;

  return (
    <aside className="flex w-[300px] shrink-0 flex-col overflow-y-auto border-l border-line bg-panel">
      {/* 头：成员数 + 上限 + 添加 */}
      <div className="flex items-center gap-2 border-b border-line px-3 py-2.5">
        <span className="text-xs font-medium text-muted">
          {tc("team.membersTitle", { count: rows.length })}
        </span>
        <span className="text-[10px] text-faint">
          {tc("team.memberCap", { cap: 6 })}
        </span>
        <button
          onClick={onAddMember}
          className="ml-auto rounded-md border border-line px-1.5 py-0.5 text-xs text-muted transition-colors hover:border-violet/50 hover:text-violet"
          title={tc("team.addMember")}
        >
          <Plus className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* 上卷摘要（§5.3.1）：团队级状态 + needs_action 提示 */}
      {rollup && (
        <div className="border-b border-line bg-base/40 px-3 py-2 text-[10.5px] leading-relaxed text-faint">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            {rollup.blocked > 0 && (
              <span className="text-yellow-500">
                {tc("team.rollupBlocked", { n: rollup.blocked })}
              </span>
            )}
            {rollup.working > 0 && (
              <span className="text-blue-400">
                {tc("team.rollupWorking", { n: rollup.working })}
              </span>
            )}
            {rollup.done_unread > 0 && (
              <span className="text-green-500">
                {tc("team.rollupDone", { n: rollup.done_unread })}
              </span>
            )}
            {rollup.blocked === 0 && rollup.working === 0 && rollup.done_unread === 0 && (
              <span>{tc("team.rollupQuiet")}</span>
            )}
          </div>
        </div>
      )}

      {/* 领队（2026-10-14 定稿：领队 = 独立 leader 成员实例，有自己的会话，
          与主会话「协作广场」分开）。有 leader 实例 → 活状态行，点开进领队
          工作台；没有 → 回退显示主会话 persona（静态，提示可任命）。 */}
      {(() => {
        const leadRow = rows.find((r) => r.instance_id === team.leader_instance_id);
        if (leadRow) {
          const agent = agentById(leadRow.agent_id);
          const hex = agentHex(agent?.color);
          return (
            <button
              onClick={() => onOpenMember(leadRow)}
              className="block border-b border-line bg-violet/[0.06] px-3 py-2.5 text-left transition-colors hover:bg-violet/[0.1]"
            >
              <div className="flex items-center gap-2">
                <span className="text-[13px] leading-none">👑</span>
                <StatusDot status={leadRow.status} />
                <span
                  className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-[10px] font-bold"
                  style={{ background: hex, color: "#0b0d10" }}
                >
                  {leadRow.display_name.slice(0, 1).toUpperCase()}
                </span>
                <span className="min-w-0 flex-1 truncate text-xs font-medium text-txt">
                  {leadRow.display_name}
                </span>
                <span className="shrink-0 rounded-full border border-violet/40 bg-violet/10 px-1.5 py-px text-[10px] text-violet">
                  {tc("team.leadBadge")}
                </span>
              </div>
              <div className="mt-1 truncate pl-[26px] text-[10px] leading-relaxed text-faint" title={leadRow.status_detail}>
                {leadRow.status_detail || tc("team.leadInstanceNote")}
              </div>
            </button>
          );
        }
        const lead = g.agents.find((x) => x.id === team.main_agent_id);
        if (!lead) return null;
        const hex = agentHex(lead.color);
        return (
          <div className="border-b border-line bg-violet/[0.06] px-3 py-2.5 opacity-80">
            <div className="flex items-center gap-2">
              <span className="text-[13px] leading-none">👑</span>
              <span
                className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-[10px] font-bold"
                style={{ background: hex, color: "#0b0d10" }}
              >
                {lead.name.slice(0, 1).toUpperCase()}
              </span>
              <span className="min-w-0 flex-1 truncate text-xs font-medium text-txt">
                {lead.name}
              </span>
              <span className="shrink-0 rounded-full border border-violet/40 bg-violet/10 px-1.5 py-px text-[10px] text-violet">
                {tc("team.leadBadge")}
              </span>
            </div>
            <div className="mt-1 pl-[26px] text-[10px] leading-relaxed text-faint">
              {tc("team.leadNote")}
            </div>
          </div>
        );
      })()}

      {/* 成员行 */}
      {rows.length === 0 && (
        <div className="px-3 py-6 text-center text-xs leading-relaxed text-faint">
          <UserX className="mx-auto mb-2 h-5 w-5 opacity-50" />
          {tc("team.noMembers")}
          <br />
          <button onClick={onAddMember} className="mt-1 text-violet hover:underline">
            {tc("team.addFirst")}
          </button>
        </div>
      )}

      {rows.map((r) => {
        const agent = agentById(r.agent_id);
        const hex = agentHex(agent?.color);
        const sel = activeInstanceId === r.instance_id;
        const blocked = r.status === "blocked";
        // 成员全部会话（P1 存量成员没有 session_ids，回退活跃一条）。
        const sids = r.session_ids?.length ? r.session_ids : [r.session_id].filter(Boolean);
        const expanded = openIds[r.instance_id] ?? false;
        return (
          <div
            key={r.instance_id}
            className={`border-b border-line/60 transition-colors hover:bg-card ${
              sel ? "bg-card shadow-[inset_2px_0_0_var(--violet,#a78bfa)]" : ""
            }`}
          >
            <button
              onClick={() => onOpenMember(r)}
              className="block w-full px-3 py-2.5 text-left"
            >
              <div className="flex items-center gap-2">
                <StatusDot status={r.status} />
                <span
                  className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-[10px] font-bold"
                  style={{ background: hex, color: "#0b0d10" }}
                >
                  {(r.display_name || r.agent_id).slice(0, 1).toUpperCase()}
                </span>
                <span className="min-w-0 flex-1 truncate text-xs font-medium text-txt">
                  {r.display_name}
                </span>
                <span
                  className={`shrink-0 rounded-full border px-1.5 py-px text-[10px] ${
                    blocked
                      ? "border-yellow-500/40 bg-yellow-500/10 text-yellow-500"
                      : "border-line bg-card text-faint"
                  }`}
                >
                  {tr(statusKey(r.status))}
                </span>
                <span className="shrink-0 text-[10px] tabular-nums text-faint">
                  {fmtElapsed(r.elapsed_s)}
                </span>
              </div>
              {/* 展示文本（§3.4 纯渲染，绝不参与逻辑） */}
              <div
                className={`mt-1 truncate pl-[26px] text-[11px] ${
                  blocked ? "text-yellow-500/90" : "text-muted"
                }`}
                title={r.status_detail}
              >
                {r.status_detail || tc("team.noDetail")}
              </div>
            </button>
            <div className="flex items-center gap-2 px-3 pb-2 pl-[26px]">
              {r.role === "leader" && (
                <span className="text-[10px] text-faint">{tc("team.leaderBadge")}</span>
              )}
              <button
                onClick={() => setOpenIds((m) => ({ ...m, [r.instance_id]: !expanded }))}
                className="ml-auto flex items-center gap-1 text-[10px] text-faint transition-colors hover:text-muted"
              >
                {expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                {tc("team.sessionsCount", { count: sids.length })}
              </button>
            </div>
            {expanded && (
              <div className="border-t border-line/40 bg-base/30 px-2 py-1.5">
                {sids.map((sid) => {
                  const meta = g.sessions.find((s) => s.id === sid);
                  const active = r.session_id === sid;
                  const viewing = activeSessionId === sid;
                  return (
                    <div
                      key={sid}
                      className={`group flex items-center gap-1.5 rounded px-1.5 py-1 text-[11px] transition-colors ${
                        viewing ? "bg-violet/10 text-txt" : "text-muted hover:bg-card"
                      }`}
                    >
                      <button
                        onClick={() => onOpenMemberSession(r.instance_id, sid)}
                        className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
                        title={meta?.title || sid}
                      >
                        <span className="min-w-0 flex-1 truncate">
                          {meta?.title?.replace(/ · [^·]*$/, "") || sid.slice(0, 8)}
                        </span>
                        {active && (
                          <span className="shrink-0 rounded bg-violet/15 px-1 text-[9px] text-violet">
                            {tc("team.activeSession")}
                          </span>
                        )}
                      </button>
                      {confirmDelSid === sid ? (
                        <button
                          onClick={async () => {
                            if (busySid) return;
                            setBusySid(sid);
                            setConfirmDelSid(null);
                            try {
                              await api.deleteTeamMemberSession(team.id, r.instance_id, sid);
                              await onMutate();
                            } finally {
                              setBusySid(null);
                            }
                          }}
                          className="shrink-0 font-medium text-red hover:underline"
                        >
                          {tc("team.confirmDeleteSession")}
                        </button>
                      ) : (
                        <button
                          onClick={() => setConfirmDelSid(sid)}
                          disabled={busySid === sid}
                          className="shrink-0 rounded p-0.5 text-faint opacity-0 transition-opacity hover:text-red group-hover:opacity-100"
                          title={tc("team.deleteSession")}
                        >
                          {busySid === sid ? (
                            <Loader2 className="h-3 w-3 animate-spin" />
                          ) : (
                            <X className="h-3 w-3" />
                          )}
                        </button>
                      )}
                    </div>
                  );
                })}
                <button
                  onClick={async () => {
                    if (busySid) return;
                    setBusySid(r.instance_id);
                    try {
                      const res = await api.addTeamMemberSession(team.id, r.instance_id);
                      if (res.ok && res.session_id) {
                        onOpenMemberSession(r.instance_id, res.session_id);
                      }
                      await onMutate();
                    } finally {
                      setBusySid(null);
                    }
                  }}
                  disabled={busySid === r.instance_id}
                  className="mt-1 flex w-full items-center gap-1 rounded px-1.5 py-1 text-[11px] text-faint transition-colors hover:bg-card hover:text-violet disabled:opacity-50"
                >
                  {busySid === r.instance_id ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    <Plus className="h-3 w-3" />
                  )}
                  {tc("team.newSession")}
                </button>
              </div>
            )}
          </div>
        );
      })}

      {/* 脚注：共享/隔离说明（§5.4） */}
      <div className="mt-auto border-t border-line px-3 py-2.5 text-[10px] leading-relaxed text-faint">
        <div>
          <b className="font-medium text-muted">{tc("team.footShared")}</b>{" "}
          {team.shared_workspace?.primary_folder || "—"}
        </div>
        <div className="mt-0.5">{tc("team.footIsolated")}</div>
      </div>
    </aside>
  );
}

/** 加载态占位（团队数据在路上）。 */
export function MemberPanelSkeleton() {
  return (
    <aside className="flex w-[300px] shrink-0 items-center justify-center border-l border-line bg-panel">
      <Loader2 className="h-4 w-4 animate-spin text-faint" />
    </aside>
  );
}
