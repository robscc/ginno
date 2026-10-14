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

function fmtAway(s: number): string {
  if (!s || s < 0) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${Math.round(s % 60)}s`;
  return `${Math.floor(m / 60)}h${m % 60}m`;
}

type View =
  | { kind: "main" }
  | { kind: "overview" }
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
  // 团队 uuid 复制反馈（对齐 session TurnIdChip 的「✓ 已复制」模式）
  const [idCopied, setIdCopied] = useState(false);
  // 总览过滤（设计稿视图 2：全部 / 仅活跃 / 需要我）+ 未读横幅收起
  const [ovFilter, setOvFilter] = useState<"all" | "active" | "needs">("all");
  const [digestOpen, setDigestOpen] = useState(true);

  const team = useMemo(
    () => teams.find((t) => t.id === teamId) ?? null,
    [teams, teamId],
  );

  const copyTeamId = async (id: string) => {
    try {
      await navigator.clipboard.writeText(id);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = id;
      ta.style.cssText = "position:fixed;opacity:0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    setIdCopied(true);
    setTimeout(() => setIdCopied(false), 1200);
  };

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
          <span className="shrink-0 rounded-full border border-yellow-500/40 bg-yellow-500/10 px-1.5 text-[10px] leading-4 text-yellow-500">
            {tc("team.rowNeeds", { n: (rollup?.blocked ?? 0) + (rollup?.done_unread ?? 0) })}
          </span>
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
          {/* 视图 tab（设计稿三视图之二：主会话 / 团队总览；成员工作台从
              面板/总览卡进入，返回按钮保留） */}
          <div className="ml-1 flex items-center gap-1 rounded-lg border border-line p-0.5">
            {(["main", "overview"] as const).map((k) => (
              <button
                key={k}
                onClick={() => setView({ kind: k })}
                className={`rounded-md px-2.5 py-1 text-xs transition-colors ${
                  (view.kind === "main" && k === "main") || view.kind === k
                    ? "bg-violet/15 text-violet"
                    : "text-faint hover:text-muted"
                }`}
              >
                {tc(k === "main" ? "team.tabMain" : "team.tabOverview")}
              </button>
            ))}
          </div>
          {/* 团队 uuid（2026-10-14 用户需求）：排障对日志用，点击复制 */}
          <button
            onClick={() => void copyTeamId(team.id)}
            className={`rounded px-1 py-0.5 font-mono text-[10px] transition-colors hover:bg-card2 hover:text-muted ${
              idCopied ? "text-green" : "text-faint"
            }`}
            title={tc("team.idCopyTitle", { id: team.id })}
          >
            {idCopied ? tc("team.idCopied") : team.id}
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
              <>
                {/* 未读汇总横幅（设计稿视图 1 顶部）：done 未读 + blocked
                    需要你，一屏收拢「谁在等我」；收起记忆到本组件。 */}
                {(() => {
                  const rows = team.member_rows ?? [];
                  const notables = rows.filter(
                    (r) => r.status === "done" || r.status === "blocked",
                  );
                  if (!digestOpen || notables.length === 0) return null;
                  const last = Math.max(
                    0,
                    ...rows.map((r) => r.last_active || 0),
                  );
                  const awayMin = last
                    ? Math.max(1, Math.round((Date.now() / 1000 - last) / 60))
                    : 0;
                  return (
                    <div className="mx-4 mt-2 rounded-lg border border-line bg-card px-3 py-2 text-xs">
                      <div className="flex items-center gap-2">
                        <span>⏱</span>
                        <span className="flex-1 font-medium text-txt">
                          {tc("team.digestTitle", { min: awayMin })}
                        </span>
                        <button
                          onClick={() => setDigestOpen(false)}
                          className="text-[11px] text-faint hover:text-muted"
                        >
                          {tc("team.digestCollapse")}
                        </button>
                      </div>
                      <div className="mt-1.5 flex flex-col gap-1">
                        {notables.map((r) => (
                          <button
                            key={r.instance_id}
                            onClick={() => openMember(r)}
                            className="flex items-center gap-2 rounded px-1 py-0.5 text-left transition-colors hover:bg-card2"
                          >
                            <span className="w-16 shrink-0 truncate text-muted">
                              {r.display_name}
                            </span>
                            <span
                              className={
                                r.status === "blocked"
                                  ? "shrink-0 text-yellow-500"
                                  : "shrink-0 text-green-500"
                              }
                            >
                              {r.status === "blocked"
                                ? tc("team.digestBlocked", { time: fmtAway(r.elapsed_s) })
                                : tc("team.digestUnread")}
                            </span>
                            <span className="min-w-0 flex-1 truncate text-faint" title={r.status_detail}>
                              {r.status_detail}
                            </span>
                          </button>
                        ))}
                      </div>
                    </div>
                  );
                })()}
                {mainSession ? (
                  <ChatStream session={mainSession} />
                ) : (
                  <div className="flex flex-1 items-center justify-center text-xs text-faint">
                    {tc("team.mainSessionMissing")}
                  </div>
                )}
              </>
            ) : view.kind === "overview" ? (
              <TeamOverview
                team={team}
                filter={ovFilter}
                onFilter={setOvFilter}
                onOpen={openMemberSession}
              />
            ) : memberSession ? (
              <>
                {/* 成员工作台顶栏（设计稿视图 3）：独立 session 说明 + 身份
                    chips（实例/模型/共享 workspace），停止由 ChatStream 自带
                    的运行控件承担。 */}
                <div className="flex flex-wrap items-center gap-2 border-b border-line bg-base/40 px-4 py-1.5 text-[11px] text-faint">
                  <span className="text-violet">{tc("team.workspaceBanner")}</span>
                  {(() => {
                    const row = (team.member_rows ?? []).find(
                      (x) => x.instance_id === view.instanceId,
                    );
                    return (
                      <>
                        {row && <span className="font-mono">{row.instance_id}</span>}
                        {row?.model && <span>· {row.model}</span>}
                        <span>· {tc("team.workspaceShared")}</span>
                      </>
                    );
                  })()}
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

/** 团队总览（设计稿视图 2，方案 B 降本版）：状态卡网格——不挂多路 WS，
 *  只吃 team.changed 聚合快照 + 每卡 status_detail 摘要。状态语义与
 *  MemberPanel 同源（teams.status 权威），卡是「谁在干什么/谁卡住了/谁
 *  需要我」的一屏答案；要看全文点卡进成员工作台。 */
function TeamOverview({
  team,
  filter,
  onFilter,
  onOpen,
}: {
  team: Team;
  filter: "all" | "active" | "needs";
  onFilter: (f: "all" | "active" | "needs") => void;
  onOpen: (instanceId: string, sessionId: string) => void;
}) {
  const tc = useTranslations("chat");
  const rows = team.member_rows ?? [];
  const active = rows.filter((r) => r.status === "working" || r.status === "blocked" || r.status === "waiting" || r.status === "unknown");
  const needs = rows.filter((r) => r.status === "blocked" || r.status === "done");
  const shown = filter === "all" ? rows : filter === "active" ? active : needs;

  const STATUS_CHIP: Record<string, string> = {
    working: "border-blue-400/40 bg-blue-400/10 text-blue-400",
    blocked: "border-yellow-500/40 bg-yellow-500/10 text-yellow-500",
    waiting: "border-violet/40 bg-violet/10 text-violet",
    done: "border-green/40 bg-green/10 text-green",
    error: "border-red/40 bg-red/10 text-red",
    unknown: "border-line bg-card2/60 text-faint",
    idle: "border-line bg-card2/60 text-faint",
  };

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
      {/* 过滤 chips（设计稿：全部/仅活跃/需要我，带计数） */}
      <div className="flex items-center gap-1.5">
        {([
          ["all", rows.length],
          ["active", active.length],
          ["needs", needs.length],
        ] as const).map(([k, n]) => (
          <button
            key={k}
            onClick={() => onFilter(k)}
            className={`rounded-full border px-2.5 py-1 text-xs transition-colors ${
              filter === k
                ? "border-violet/50 bg-violet/10 text-violet"
                : "border-line text-muted hover:text-txt"
            }`}
          >
            {tc(`team.ovFilter.${k}`)} ({n})
          </button>
        ))}
        <span className="ml-auto text-[11px] text-faint">
          {tc("team.ovHint")}
        </span>
      </div>

      <div
        className="mt-3 grid gap-3"
        style={{ gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))" }}
      >
        {shown.map((r) => {
          const sids = r.session_ids?.length ? r.session_ids : [r.session_id].filter(Boolean);
          const chip = STATUS_CHIP[r.status] ?? STATUS_CHIP.idle;
          return (
            <button
              key={r.instance_id}
              onClick={() => onOpen(r.instance_id, r.session_id)}
              className="flex flex-col rounded-xl border border-line bg-card p-3 text-left transition-colors hover:border-violet/40"
            >
              <div className="flex items-center gap-2">
                <span className="min-w-0 flex-1 truncate text-sm font-medium text-txt">
                  {r.instance_id === team.leader_instance_id ? "👑 " : ""}{r.display_name}
                </span>
                <span className={`shrink-0 rounded-full border px-1.5 py-px text-[10px] ${chip}`}>
                  {tc(`team.status.${r.status}`)}
                </span>
                <span className="shrink-0 text-[10px] tabular-nums text-faint">
                  {fmtAway(r.elapsed_s)}
                </span>
              </div>
              {/* 最近活动摘要（权威 status_detail，不前端二次判态） */}
              <div
                className={`mt-1.5 line-clamp-3 text-[11px] leading-relaxed ${
                  r.status === "blocked" ? "text-yellow-500/90" : "text-muted"
                }`}
                title={r.status_detail}
              >
                {r.status_detail || tc("team.noDetail")}
              </div>
              <div className="mt-auto flex items-center gap-2 pt-2 text-[10px] text-faint">
                <span className="truncate font-mono">{r.agent_id}</span>
                {r.model && <span className="truncate">· {r.model}</span>}
                <span>· {tc("team.ovSessions", { n: sids.length })}</span>
                <span className="ml-auto shrink-0 text-violet">{tc("team.ovOpen")}</span>
              </div>
            </button>
          );
        })}
        {shown.length === 0 && (
          <div className="col-span-full py-10 text-center text-xs text-faint">
            {tc("team.ovEmpty")}
          </div>
        )}
      </div>
    </div>
  );
}
