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
//
// 本批次修复（docs/agent-team-backlog.md）：
// · P1-14 成员级生命周期 UI：行尾 ⋯ 菜单（重命名 / 设为领队 / 降为成员 /
//   移除），调既有 patchTeamMember / removeTeamMember（runtime 已同步
//   leader_instance_id，前端只管调 API）；移除走 ConfirmModal 二次确认。
// · P2-16 elapsed 本地按 last_active 实时走字（快照 elapsed_s 只作兜底）。
// · P2-18 会话增删 / 成员变更补 catch + 面板内可见错误条（不用 alert）。
// · P2-19 教育性文案：上卷规则说明条 + 「@=投递不是换 persona」脚注。
// · P2-5 成本可见：每行实例累计 token + 脚注团队合计（数据由 TeamShell 的
//   useTeamUsage 低频拉取下发，本组件纯渲染，精确值进 tooltip）。
// · P1-19 overlay 模式：窄窗口下由 TeamShell 以抽屉形式渲染（不挤对话列）。
// · 顺手修：领队活状态行此前在「已剔除领队的 rows」里 find 领队 → 恒
//   undefined，永远回退静态 persona；改从 allRows 找（生命周期菜单也挂在它上面）。

import { useEffect, useState } from "react";
import {
  ArrowDown,
  ChevronDown,
  ChevronRight,
  Crown,
  Loader2,
  MoreHorizontal,
  PenLine,
  Plus,
  RefreshCw,
  Trash2,
  UserX,
  X,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import { agentHex } from "@/lib/theme";
import { ConfirmModal } from "@/components/ConfirmModal";
import type { Team, TeamMemberRow, TeamMemberStatus } from "@/lib/types";
import { useEscClose } from "./useEscClose";
import { fmtTokens, type TeamUsage } from "./useTeamUsage";

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

/** P2-16：elapsed 实时走字。runtime 的 elapsed_s = now - last_active
 *  （status.py member_row），快照一到前端就冻结——blocked 等待时长不再
 *  增长。本地按同一口径用 last_active 现算，1s tick 驱动重渲染；
 *  last_active 缺失（极老数据）才回退快照值。 */
function liveElapsed(r: TeamMemberRow): number {
  return r.last_active > 0
    ? Math.max(0, Date.now() / 1000 - r.last_active)
    : r.elapsed_s;
}

export function MemberPanel({
  team,
  activeInstanceId,
  activeSessionId,
  onOpenMember,
  onOpenMemberSession,
  onMutate,
  onAddMember,
  overlay = false,
  onClose,
  usage = null,
  usageLoading = false,
  usageFailed = false,
  onRefreshUsage,
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
  /** P1-19：窄窗口抽屉模式（TeamShell 用绝对定位包裹，不挤对话列）。 */
  overlay?: boolean;
  /** 抽屉模式的关闭按钮回调。 */
  onClose?: () => void;
  /** P2-5：团队成本聚合（TeamShell 统一低频拉取后下发，面板不自取）。 */
  usage?: TeamUsage | null;
  usageLoading?: boolean;
  usageFailed?: boolean;
  onRefreshUsage?: () => void;
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
  // P1-14 成员生命周期：⋯ 菜单 / 重命名输入态 / 移除确认 / 进行中。
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameText, setRenameText] = useState("");
  const [confirmRemove, setConfirmRemove] = useState<TeamMemberRow | null>(null);
  const [busyMember, setBusyMember] = useState<string | null>(null);
  // P2-18：面板内可见错误条（成员变更 / 会话增删失败都不静默）。
  const [panelError, setPanelError] = useState<string | null>(null);
  // elapsed 每秒走字（仅 working/blocked 需要，但便宜起见统一 tick）。
  const [, setTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);

  // P2-17：Esc 关菜单 / 关移除确认（ConfirmModal 本体在边界外，Esc 在调用侧接）。
  useEscClose(() => setMenuFor(null), menuFor !== null);
  useEscClose(() => setConfirmRemove(null), confirmRemove !== null);

  const agentById = (id: string) => g.agents.find((a) => a.id === id) ?? null;

  /** P2-5：每实例累计 token chip（人类可读，精确输入/输出/调用数进 tooltip）。
   *  usage 未加载（null）时不渲染——不拿 0 冒充「还没拉到的数据」。 */
  const usageChip = (instanceId: string) => {
    if (!usage) return null;
    const u = usage.members.find((m) => m.instance_id === instanceId);
    return (
      <span
        className="shrink-0 text-[10px] tabular-nums text-faint"
        title={tc("team.usageTip", {
          days: usage.days,
          input: fmtTokens(u?.input_tokens ?? 0),
          output: fmtTokens(u?.output_tokens ?? 0),
          calls: u?.calls ?? 0,
        })}
      >
        {tc("team.usageTok", { n: fmtTokens(u?.total_tokens ?? 0) })}
      </span>
    );
  };

  // ---- P1-14 成员生命周期动作（runtime 端点齐备，此前全仓库零调用）----
  const patchMember = async (
    instanceId: string,
    patch: { display_name?: string; role?: "leader" | "member" },
  ) => {
    if (busyMember) return;
    setBusyMember(instanceId);
    setMenuFor(null);
    setPanelError(null);
    try {
      const r = await api.patchTeamMember(team.id, instanceId, patch);
      // runtime 拒绝（如「已有领队」）带回本地化 error，直接展示。
      if (!r.ok) setPanelError(r.error || tc("team.memberPatchFailed"));
      else await onMutate();
    } catch {
      setPanelError(tc("team.memberPatchFailed"));
    } finally {
      setBusyMember(null);
    }
  };

  const submitRename = () => {
    const id = renamingId;
    const name = renameText.trim();
    setRenamingId(null);
    if (id && name) void patchMember(id, { display_name: name });
  };

  const removeMember = async (row: TeamMemberRow) => {
    if (busyMember) return;
    setBusyMember(row.instance_id);
    setPanelError(null);
    try {
      const r = await api.removeTeamMember(team.id, row.instance_id);
      if (!r.ok) setPanelError(r.error || tc("team.memberRemoveFailed"));
      else await onMutate();
    } catch {
      setPanelError(tc("team.memberRemoveFailed"));
    } finally {
      setBusyMember(null);
      setConfirmRemove(null);
    }
  };

  /** 行尾 ⋯ 菜单触发器（hover / 键盘 focus 都可见——P2-17）。 */
  const menuButton = (r: TeamMemberRow) => (
    <button
      onClick={(e) => {
        e.stopPropagation();
        setMenuFor(menuFor === r.instance_id ? null : r.instance_id);
      }}
      disabled={busyMember === r.instance_id}
      className={`shrink-0 rounded p-0.5 text-faint transition-opacity hover:text-txt focus-visible:opacity-100 group-focus-within:opacity-100 disabled:opacity-60 ${
        menuFor === r.instance_id ? "opacity-100" : "opacity-0 group-hover:opacity-100"
      }`}
      title={tc("team.memberManage")}
    >
      {busyMember === r.instance_id ? (
        <Loader2 className="h-3 w-3 animate-spin" />
      ) : (
        <MoreHorizontal className="h-3 w-3" />
      )}
    </button>
  );

  /** ⋯ 菜单本体：重命名 / 设为领队 / 降为成员 / 移除（P1-14）。 */
  const memberMenu = (r: TeamMemberRow) => (
    <div
      className="absolute right-2 top-9 z-20 w-44 rounded-md border border-line bg-card py-1 text-xs shadow-lg"
      onClick={(e) => e.stopPropagation()}
    >
      <button
        className="flex w-full items-center gap-1.5 px-2 py-1 text-left text-muted hover:bg-card2 hover:text-txt"
        onClick={() => {
          setMenuFor(null);
          setRenameText(r.display_name);
          setRenamingId(r.instance_id);
        }}
      >
        <PenLine className="h-3 w-3" /> {tc("team.rename")}
      </button>
      {r.role === "leader" ? (
        <button
          className="flex w-full items-center gap-1.5 px-2 py-1 text-left text-muted hover:bg-card2 hover:text-txt"
          onClick={() => void patchMember(r.instance_id, { role: "member" })}
        >
          <ArrowDown className="h-3 w-3" /> {tc("team.demoteMember")}
        </button>
      ) : (
        <button
          className="flex w-full items-center gap-1.5 px-2 py-1 text-left text-muted hover:bg-card2 hover:text-txt"
          onClick={() => void patchMember(r.instance_id, { role: "leader" })}
        >
          <Crown className="h-3 w-3" /> {tc("team.promoteLeader")}
        </button>
      )}
      <button
        className="flex w-full items-center gap-1.5 px-2 py-1 text-left text-muted hover:bg-red/10 hover:text-red"
        onClick={() => {
          setMenuFor(null);
          setConfirmRemove(r);
        }}
      >
        <Trash2 className="h-3 w-3" /> {tc("team.removeMember")}
      </button>
    </div>
  );

  /** 重命名输入行（Enter 提交 / Esc 取消 / 失焦提交，同团队行范式）。 */
  const renameInput = (
    <div className="px-3 py-2.5">
      <input
        autoFocus
        value={renameText}
        onChange={(e) => setRenameText(e.target.value)}
        onBlur={submitRename}
        onKeyDown={(e) => {
          if (e.key === "Enter") submitRename();
          if (e.key === "Escape") {
            e.stopPropagation();
            setRenamingId(null);
          }
        }}
        className="w-full rounded border border-violet/50 bg-card px-1.5 py-1 text-xs text-txt outline-none"
      />
    </div>
  );

  // 领队活状态行从 allRows 找（rows 已剔除领队——此前在 rows 里 find 恒空）。
  const leadRow = allRows.find((r) => r.instance_id === team.leader_instance_id) ?? null;

  return (
    <aside
      onClick={() => setMenuFor(null)}
      className={`flex w-[300px] shrink-0 flex-col overflow-y-auto border-l border-line bg-panel ${
        overlay ? "max-w-[85vw] shadow-2xl" : ""
      }`}
    >
      {/* 头：成员数 + 上限 + 添加（抽屉模式多一个关闭） */}
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
        {overlay && onClose && (
          <button
            onClick={onClose}
            className="rounded-md border border-line px-1.5 py-0.5 text-xs text-muted transition-colors hover:text-txt"
            title={tc("team.closePanel")}
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      {/* P2-18：可见错误条——成员变更 / 会话操作失败不静默吞。 */}
      {panelError && (
        <div className="flex items-start gap-2 border-b border-red/30 bg-red/10 px-3 py-1.5 text-[11px] leading-relaxed text-red">
          <span className="min-w-0 flex-1">{panelError}</span>
          <button
            onClick={() => setPanelError(null)}
            className="shrink-0 transition-opacity hover:opacity-70"
            title={tc("team.guideDismiss")}
          >
            <X className="h-3 w-3" />
          </button>
        </div>
      )}

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
          {/* P2-19：上卷规则说明条（原型 .rollup 的教育性文案）。只写
              与实现一致的不变量：冒泡判据 blocked/done，working 止于团队行。 */}
          <div className="mt-1 border-t border-line/50 pt-1 text-[10px] leading-relaxed">
            {tc("team.rollupExplainer")}
          </div>
        </div>
      )}

      {/* 领队（2026-10-14 定稿：领队 = 独立 leader 成员实例，有自己的会话，
          与主会话「协作广场」分开）。有 leader 实例 → 活状态行，点开进领队
          工作台；没有 → 回退显示主会话 persona（静态，提示可任命）。 */}
      {(() => {
        if (leadRow) {
          const agent = agentById(leadRow.agent_id);
          const hex = agentHex(agent?.color);
          return (
            <div className="group relative border-b border-line bg-violet/[0.06]">
              {renamingId === leadRow.instance_id ? (
                renameInput
              ) : (
                <div className="flex items-start gap-1 px-3 py-2.5">
                  {/* 主点击区与 ⋯ 菜单是平级按钮（不嵌套交互内容）。 */}
                  <button
                    onClick={() => onOpenMember(leadRow)}
                    className="min-w-0 flex-1 text-left transition-colors hover:opacity-90"
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
                    <div className="mt-1 flex items-center gap-2 pl-[26px] text-[10px] leading-relaxed text-faint">
                      <span className="min-w-0 flex-1 truncate" title={leadRow.status_detail}>
                        {leadRow.status_detail || tc("team.leadInstanceNote")}
                      </span>
                      {/* P2-5：领队实例的累计 token（设计 §5.6 成本可见）。 */}
                      {usageChip(leadRow.instance_id)}
                    </div>
                  </button>
                  {menuButton(leadRow)}
                </div>
              )}
              {menuFor === leadRow.instance_id && memberMenu(leadRow)}
            </div>
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
            className={`group relative border-b border-line/60 transition-colors hover:bg-card ${
              sel ? "bg-card shadow-[inset_2px_0_0_var(--violet,#a78bfa)]" : ""
            }`}
          >
            {renamingId === r.instance_id ? (
              renameInput
            ) : (
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
                    {fmtElapsed(liveElapsed(r))}
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
            )}
            <div className="flex items-center gap-2 px-3 pb-2 pl-[26px]">
              {r.role === "leader" && (
                <span className="text-[10px] text-faint">{tc("team.leaderBadge")}</span>
              )}
              {/* P2-5：该实例累计 token（多会话全部计入，runtime 端点已聚合）。 */}
              {usageChip(r.instance_id)}
              <button
                onClick={() => setOpenIds((m) => ({ ...m, [r.instance_id]: !expanded }))}
                className="ml-auto flex items-center gap-1 text-[10px] text-faint transition-colors hover:text-muted"
              >
                {expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                {tc("team.sessionsCount", { count: sids.length })}
              </button>
              {/* P1-14：成员生命周期入口（重命名/升降领队/移除） */}
              {menuButton(r)}
            </div>
            {menuFor === r.instance_id && memberMenu(r)}
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
                            setPanelError(null);
                            try {
                              const res = await api.deleteTeamMemberSession(team.id, r.instance_id, sid);
                              // P2-18：ok:false 与异常都要让用户看见。
                              if (!res.ok) setPanelError(res.error || tc("team.sessionOpFailed"));
                              await onMutate();
                            } catch {
                              setPanelError(tc("team.sessionOpFailed"));
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
                          className="shrink-0 rounded p-0.5 text-faint opacity-0 transition-opacity hover:text-red focus-visible:opacity-100 group-hover:opacity-100 group-focus-within:opacity-100"
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
                    setPanelError(null);
                    try {
                      const res = await api.addTeamMemberSession(team.id, r.instance_id);
                      if (res.ok && res.session_id) {
                        onOpenMemberSession(r.instance_id, res.session_id);
                      } else if (!res.ok) {
                        setPanelError(res.error || tc("team.sessionOpFailed"));
                      }
                      await onMutate();
                    } catch {
                      setPanelError(tc("team.sessionOpFailed"));
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

      {/* 脚注：共享/隔离说明（§5.4）+ P2-19 「@=投递」教育性脚注 */}
      <div className="mt-auto border-t border-line px-3 py-2.5 text-[10px] leading-relaxed text-faint">
        <div>
          <b className="font-medium text-muted">{tc("team.footShared")}</b>{" "}
          {team.shared_workspace?.primary_folder || "—"}
        </div>
        <div className="mt-0.5">{tc("team.footIsolated")}</div>
        {/* P2-5：团队合计成本 + 手动刷新（低频拉取的手动入口；60s 自动刷新
            由 TeamShell 的 useTeamUsage 承担，不在状态热路径里）。 */}
        {usage && (
          <div className="mt-1.5 flex items-center gap-1.5 border-t border-line/60 pt-1.5">
            <span
              className="min-w-0 flex-1 truncate tabular-nums"
              title={tc("team.usageTip", {
                days: usage.days,
                input: fmtTokens(usage.total.input_tokens),
                output: fmtTokens(usage.total.output_tokens),
                calls: usage.total.calls,
              })}
            >
              <b className="font-medium text-muted">
                {tc("team.usageTotalLabel", { days: usage.days })}
              </b>{" "}
              {tc("team.usageTotalValue", {
                tokens: fmtTokens(usage.total.total_tokens),
                calls: usage.total.calls,
              })}
            </span>
            {onRefreshUsage && (
              <button
                onClick={onRefreshUsage}
                disabled={usageLoading}
                className="shrink-0 rounded p-0.5 transition-colors hover:text-muted disabled:opacity-60"
                title={tc("team.usageRefresh")}
              >
                <RefreshCw className={`h-3 w-3 ${usageLoading ? "animate-spin" : ""}`} />
              </button>
            )}
          </div>
        )}
        {usageFailed && (
          <div className="mt-1 text-red/80">{tc("team.usageFailed")}</div>
        )}
        <div className="mt-1.5 border-t border-line/60 pt-1.5">{tc("team.atDeliveryNote")}</div>
      </div>

      {/* P1-14：移除成员二次确认（复用仓库 ConfirmModal；级联删会话是
          不可逆动作，必须点名成员）。Esc 由上方 useEscClose 承接。
          R3 巡检 Q5：成员有活 turn（working，或 parked 等输入的 blocked）
          时确认框必须说明后果——强停 turn + 全部会话不可恢复，对照「解散
          团队」的活 turn 防呆范式；空闲成员保持原文案。 */}
      {confirmRemove && (
        <ConfirmModal
          title={tc("team.removeMemberConfirmTitle")}
          message={
            confirmRemove.status === "working" || confirmRemove.status === "blocked"
              ? tc("team.removeMemberRunningMsg", { name: confirmRemove.display_name })
              : tc("team.removeMemberConfirmMsg", { name: confirmRemove.display_name })
          }
          confirmLabel={tc("team.removeMember")}
          onConfirm={() => void removeMember(confirmRemove)}
          onCancel={() => setConfirmRemove(null)}
        />
      )}
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
