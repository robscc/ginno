"use client";

// TeamShell — AgentTeam 独立空间（agent-team-design.md §5.1 / §6，P2 产品重构
// 2026-10-13：完全隔离 + 左栏团队列表 + 团队管理）。
//
// 三栏：左 = 团队列表（活跃/归档分组、新建、重命名/归档/解散）；中 = 主会话
// ChatStream 或成员工作台（另一个 ChatStream，独立 session）；右 = MemberPanel
// （成员状态 + 多会话管理）。团队的 session 不进主侧栏（AppShell 按 type 过滤），
// 本页是它们唯一入口——「我的对话」与「团队空间」是两个世界。
//
// 自包含页（照 ScheduledPage 先例）：自己 fetch 团队列表 + 订阅主会话 WS 的
// team.changed 事件刷新状态，不侵入全局 store。ChatStream 完全由 session prop
// 驱动、自带 WS，所以主会话与成员工作台可以各挂一个实例（同页两个 socket，
// _SESSION_WS 是 list，支持多订阅）。
//
// 状态一律来自 runtime teams.status（§5.9 唯一权威来源），本页不自己判态。

import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft, ArchiveRestore, Loader2, PenLine, Plus, Trash2, Users } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { SessionMeta, Team, TeamMemberRow } from "@/lib/types";
import { ChatStream } from "@/components/chat/ChatStream";
import { MemberPanel, MemberPanelSkeleton } from "./MemberPanel";
import { AddMemberModal } from "./AddMemberModal";
import { NewTeamModal } from "./NewTeamModal";

type View =
  | { kind: "main" }
  | { kind: "member"; instanceId: string; sessionId: string };

export function TeamShell() {
  const g = useGinno();
  const tc = useTranslations("chat");

  const [teams, setTeams] = useState<Team[]>([]);
  const [teamId, setTeamId] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [view, setView] = useState<View>({ kind: "main" });
  const [addOpen, setAddOpen] = useState(false);
  const [newTeamOpen, setNewTeamOpen] = useState(false);
  // 团队管理：rename（输入态）与 disband（两步确认）都在行内完成，不引大弹窗。
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameText, setRenameText] = useState("");
  const [confirmDisbandId, setConfirmDisbandId] = useState<string | null>(null);
  const [archivedOpen, setArchivedOpen] = useState(false);
  const [menuId, setMenuId] = useState<string | null>(null);

  const team = useMemo(
    () => teams.find((t) => t.id === teamId) ?? null,
    [teams, teamId],
  );

  // ---- 拉团队列表 ----
  const refresh = useCallback(async () => {
    try {
      const r = await api.listTeams();
      const list = r.teams ?? [];
      setTeams(list);
      // 默认选第一个未归档团队（或保持当前选择）。
      setTeamId((cur) =>
        cur && list.some((t) => t.id === cur) ? cur : list.find((t) => !t.archived)?.id ?? list[0]?.id ?? null,
      );
    } catch {
      /* sidecar 未起——保留上次数据 */
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 成员/团队增删后，会话列表也要刷新（成员 session 是真实 session）。
  const afterMutate = useCallback(async () => {
    await Promise.all([refresh(), g.reloadSessions()]);
  }, [refresh, g]);

  // ---- 团队管理动作 ----
  const patchTeamLocal = useCallback(
    async (id: string, patch: Record<string, unknown>) => {
      await api.patchTeam(id, patch);
      await refresh();
    },
    [refresh],
  );

  const submitRename = useCallback(async () => {
    const id = renamingId;
    const name = renameText.trim();
    setRenamingId(null);
    if (id && name) await patchTeamLocal(id, { name });
  }, [renamingId, renameText, patchTeamLocal]);

  const disband = useCallback(
    async (id: string, force = false) => {
      setConfirmDisbandId(null);
      setMenuId(null);
      try {
        const r = await api.deleteTeam(id, force);
        if (!r.ok && r.live?.length && !force) {
          // 防呆回执：点名正在运行的成员——先处理再删，不静默吞。
          alert(tc("team.disbandBlocked", { names: r.live.join("、") }));
          return;
        }
      } catch {
        /* sidecar down */
      }
      if (teamId === id) setTeamId(null);
      await afterMutate();
    },
    [teamId, afterMutate, tc],
  );

  // ---- 订阅主会话 WS 的 team.changed（状态回流，§5.3）----
  // 只订阅当前团队主会话；事件里带完整 team payload，直接替换本地那份。
  const mainSid = team?.main_session_id;
  useEffect(() => {
    if (!mainSid) return;
    let ws: WebSocket | null = null;
    try {
      ws = api.openSessionSocket(mainSid);
    } catch {
      return; // 预渲染环境
    }
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data) as {
          event?: string;
          team_id?: string;
          team?: Team;
        };
        if (msg.event === "team.changed" && msg.team && msg.team_id) {
          setTeams((prev) =>
            prev.map((t) => (t.id === msg.team_id ? (msg.team as Team) : t)),
          );
        }
      } catch {
        /* 非 JSON 帧 */
      }
    };
    return () => {
      try {
        ws?.close();
      } catch {
        /* noop */
      }
    };
  }, [mainSid]);

  // ---- 成员工作台跳转（TeamReplyCard 的「进入工作台」也走这个事件）----
  const openMemberSession = useCallback(
    (instanceId: string, sessionId: string) => {
      setView({ kind: "member", instanceId, sessionId });
      // 打开即标记 seen：done → idle（§3.3 收件箱语义）。
      if (teamId) void api.markTeamMemberSeen(teamId, instanceId).then(() => refresh());
    },
    [teamId, refresh],
  );

  const openMember = useCallback(
    (row: TeamMemberRow) => openMemberSession(row.instance_id, row.session_id),
    [openMemberSession],
  );

  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as { sessionId?: string; instanceId?: string };
      if (detail?.sessionId && detail?.instanceId) {
        openMemberSession(detail.instanceId, detail.sessionId);
      }
    };
    window.addEventListener("ginno:open-team-member", handler);
    return () => window.removeEventListener("ginno:open-team-member", handler);
  }, [openMemberSession]);

  // ---- 当前视图要渲染的 session ----
  const sessionById = useCallback(
    (id: string | undefined | null): SessionMeta | null =>
      (id ? g.sessions.find((s) => s.id === id) ?? null : null),
    [g.sessions],
  );

  const mainSession = sessionById(team?.main_session_id);
  const memberSession =
    view.kind === "member" ? sessionById(view.sessionId) : null;

  // ---- 团队列表行（左栏）----
  const renderTeamRow = (t: Team) => {
    const sel = t.id === teamId;
    const rollup = t.rollup;
    const busy = (rollup?.working ?? 0) > 0;
    const alert = (rollup?.needs_action ?? false) && !t.archived;
    if (renamingId === t.id) {
      return (
        <div key={t.id} className="px-2 py-1.5">
          <input
            autoFocus
            value={renameText}
            onChange={(e) => setRenameText(e.target.value)}
            onBlur={() => void submitRename()}
            onKeyDown={(e) => {
              if (e.key === "Enter") void submitRename();
              if (e.key === "Escape") setRenamingId(null);
            }}
            className="w-full rounded border border-violet/50 bg-card px-1.5 py-1 text-xs text-txt outline-none"
          />
        </div>
      );
    }
    return (
      <div
        key={t.id}
        title={`${t.name} · ${t.id}`}
        className={`group relative flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1.5 text-xs transition-colors ${
          sel ? "bg-violet/15 text-txt" : "text-muted hover:bg-card"
        } ${t.archived ? "opacity-55" : ""}`}
        onClick={() => {
          setTeamId(t.id);
          setView({ kind: "main" });
          setMenuId(null);
        }}
      >
        {alert ? (
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-yellow-500" />
        ) : busy ? (
          <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-blue-400" />
        ) : (
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-transparent" />
        )}
        <span className="min-w-0 flex-1 truncate">{t.name}</span>
        <span className="shrink-0 text-[10px] tabular-nums text-faint">
          {t.member_rows?.length ?? t.members?.length ?? 0}
        </span>
        {/* 行内菜单（hover 显示） */}
        <button
          onClick={(e) => {
            e.stopPropagation();
            setMenuId(menuId === t.id ? null : t.id);
          }}
          className={`shrink-0 rounded p-0.5 text-faint opacity-0 transition-opacity hover:text-txt group-hover:opacity-100 ${menuId === t.id ? "opacity-100" : ""}`}
          title={tc("team.manage")}
        >
          <PenLine className="h-3 w-3" />
        </button>
        {menuId === t.id && (
          <div
            className="absolute right-1 top-full z-20 mt-1 w-36 rounded-md border border-line bg-card py-1 text-xs shadow-lg"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              className="flex w-full items-center gap-1.5 px-2 py-1 text-left text-muted hover:bg-card2 hover:text-txt"
              onClick={() => {
                setMenuId(null);
                setRenameText(t.name);
                setRenamingId(t.id);
              }}
            >
              <PenLine className="h-3 w-3" /> {tc("team.rename")}
            </button>
            <button
              className="flex w-full items-center gap-1.5 px-2 py-1 text-left text-muted hover:bg-card2 hover:text-txt"
              onClick={() => {
                setMenuId(null);
                void patchTeamLocal(t.id, { archived: !t.archived });
              }}
            >
              <ArchiveRestore className="h-3 w-3" />
              {t.archived ? tc("team.unarchive") : tc("team.archive")}
            </button>
            {confirmDisbandId === t.id ? (
              <button
                className="flex w-full items-center gap-1.5 px-2 py-1 text-left font-medium text-red hover:bg-red/10"
                onClick={() => void disband(t.id)}
              >
                <Trash2 className="h-3 w-3" /> {tc("team.confirmDisband")}
              </button>
            ) : (
              <button
                className="flex w-full items-center gap-1.5 px-2 py-1 text-left text-muted hover:bg-red/10 hover:text-red"
                onClick={() => setConfirmDisbandId(t.id)}
              >
                <Trash2 className="h-3 w-3" /> {tc("team.disband")}
              </button>
            )}
          </div>
        )}
      </div>
    );
  };

  const activeTeams = teams.filter((t) => !t.archived);
  const archivedTeams = teams.filter((t) => t.archived);

  // ---- 渲染 ----
  if (!loaded) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-faint" />
      </div>
    );
  }

  if (!team) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 text-sm text-faint">
        <Users className="h-8 w-8 opacity-40" />
        <div className="text-center leading-relaxed">
          {tc("team.empty")}
          <br />
          <span className="text-xs">{tc("team.emptyHint")}</span>
        </div>
        <button
          onClick={() => setNewTeamOpen(true)}
          className="flex items-center gap-1.5 rounded-md border border-line2 px-3 py-1.5 text-xs text-txt transition-colors hover:border-violet/50 hover:text-violet"
        >
          <Plus className="h-3.5 w-3.5" />
          {tc("team.newTeam")}
        </button>
        {newTeamOpen && (
          <NewTeamModal
            onClose={() => setNewTeamOpen(false)}
            onCreated={async (t) => {
              setNewTeamOpen(false);
              await afterMutate();
              setTeamId(t.id);
            }}
          />
        )}
      </div>
    );
  }

  return (
    <div className="flex min-h-0 min-w-0 flex-1">
      {/* 左栏：团队列表（本空间的导航与会话管理入口） */}
      <aside className="flex w-52 shrink-0 flex-col border-r border-line bg-panel">
        <div className="flex items-center gap-2 px-3 py-2.5">
          <Users className="h-3.5 w-3.5 shrink-0 text-violet" />
          <span className="flex-1 text-xs font-medium text-muted">
            {tc("team.teamsTitle", { count: activeTeams.length })}
          </span>
          <button
            onClick={() => setNewTeamOpen(true)}
            className="rounded-md border border-line px-1.5 py-0.5 text-xs text-muted transition-colors hover:border-violet/50 hover:text-violet"
            title={tc("team.newTeam")}
          >
            <Plus className="h-3.5 w-3.5" />
          </button>
        </div>
        <div
          className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-1.5"
          onClick={() => setMenuId(null)}
        >
          {activeTeams.map(renderTeamRow)}
          {archivedTeams.length > 0 && (
            <div className="mt-2">
              <button
                onClick={() => setArchivedOpen((v) => !v)}
                className="flex w-full items-center gap-1 px-2 py-1 text-[10px] text-faint hover:text-muted"
              >
                {tc("team.archivedSection", { count: archivedTeams.length })}
              </button>
              {archivedOpen && archivedTeams.map(renderTeamRow)}
            </div>
          )}
        </div>
      </aside>

      {/* 右侧：顶栏 + 主区 */}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="flex h-12 shrink-0 items-center gap-3 border-b border-line bg-panel px-4">
          <span className="text-sm font-semibold">{team.name}</span>
          {/* 团队 uuid（2026-10-14 用户需求）：排障对日志用，点击复制 */}
          <button
            onClick={() => void navigator.clipboard?.writeText(team.id).catch(() => {})}
            className="rounded px-1 py-0.5 font-mono text-[10px] text-faint transition-colors hover:bg-card2 hover:text-muted"
            title={tc("team.idCopyTitle", { id: team.id })}
          >
            {team.id}
          </button>
          <span className="text-xs text-faint">
            {tc("team.memberCount", { count: team.member_rows?.length ?? 0 })}
          </span>

          {view.kind === "member" && (
            <button
              onClick={() => setView({ kind: "main" })}
              className="ml-2 flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs text-muted transition-colors hover:border-violet/50 hover:text-violet"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              {tc("team.backToMain")}
            </button>
          )}

          <div className="ml-auto flex items-center gap-2">
            {team.rollup?.needs_action && (
              <span className="rounded-full border border-yellow-500/40 bg-yellow-500/10 px-2 py-0.5 text-[11px] text-yellow-500">
                {team.rollup.blocked > 0
                  ? tc("team.rollupBlocked", { n: team.rollup.blocked })
                  : tc("team.rollupDone", { n: team.rollup.done_unread })}
              </span>
            )}
          </div>
        </div>

        {/* 主体：左对话（主会话 or 成员工作台）+ 右成员面板 */}
        <div className="flex min-h-0 flex-1">
          <div className="flex min-w-0 flex-1 flex-col">
            {view.kind === "main" ? (
              mainSession ? (
                <ChatStream session={mainSession} />
              ) : (
                <div className="flex flex-1 items-center justify-center text-xs text-faint">
                  {tc("team.mainSessionMissing")}
                </div>
              )
            ) : memberSession ? (
              <>
                {/* 成员工作台顶栏：提示这是独立 session + seen 语义 */}
                <div className="flex items-center gap-2 border-b border-line bg-base/40 px-4 py-1.5 text-[11px] text-faint">
                  <span className="text-violet">{tc("team.workspaceBanner")}</span>
                </div>
                <ChatStream session={memberSession} />
              </>
            ) : (
              <div className="flex flex-1 items-center justify-center text-xs text-faint">
                {tc("team.memberSessionMissing")}
              </div>
            )}
          </div>

          {team.member_rows ? (
            <MemberPanel
              team={team}
              activeInstanceId={view.kind === "member" ? view.instanceId : null}
              activeSessionId={view.kind === "member" ? view.sessionId : null}
              onOpenMember={openMember}
              onOpenMemberSession={openMemberSession}
              onMutate={afterMutate}
              onAddMember={() => setAddOpen(true)}
            />
          ) : (
            <MemberPanelSkeleton />
          )}
        </div>
      </div>

      {addOpen && (
        <AddMemberModal
          team={team}
          onClose={() => setAddOpen(false)}
          onAdded={async () => {
            setAddOpen(false);
            await afterMutate();
          }}
        />
      )}
      {newTeamOpen && (
        <NewTeamModal
          onClose={() => setNewTeamOpen(false)}
          onCreated={async (t) => {
            setNewTeamOpen(false);
            await afterMutate();
            setTeamId(t.id);
          }}
        />
      )}
    </div>
  );
}
