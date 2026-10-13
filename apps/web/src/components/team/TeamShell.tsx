"use client";

// TeamShell — AgentTeam 模式主界面（agent-team-design.md §5.1 / §6 P1）。
//
// 形态 = 方案 C「领队工坊」：主会话保留完整对话（ChatStream），右侧常驻成员
// 面板（MemberPanel，需求①）。点成员行 → 切到该成员的工作台（另一个
// ChatStream，独立 session）。回帖以 TeamReplyCard 渲染在主会话里（§5.2）。
//
// 自包含页（照 ScheduledPage 先例）：自己 fetch 团队列表 + 订阅主会话 WS 的
// team.changed 事件刷新状态，不侵入全局 store。ChatStream 完全由 session prop
// 驱动、自带 WS，所以主会话与成员工作台可以各挂一个实例（同页两个 socket，
// _SESSION_WS 是 list，支持多订阅）。
//
// 状态一律来自 runtime teams.status（§5.9 唯一权威来源），本页不自己判态。

import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft, Loader2, Plus, Users } from "lucide-react";
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
      // 默认选第一个团队（或保持当前选择）。
      setTeamId((cur) => (cur && list.some((t) => t.id === cur) ? cur : list[0]?.id ?? null));
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
  const openMember = useCallback((row: TeamMemberRow) => {
    setView({ kind: "member", instanceId: row.instance_id, sessionId: row.session_id });
    // 打开即标记 seen：done → idle（§3.3 收件箱语义）。
    if (teamId) void api.markTeamMemberSeen(teamId, row.instance_id).then(() => refresh());
  }, [teamId, refresh]);

  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as { sessionId?: string; instanceId?: string };
      if (detail?.sessionId && detail?.instanceId) {
        setView({ kind: "member", instanceId: detail.instanceId, sessionId: detail.sessionId });
        if (teamId) void api.markTeamMemberSeen(teamId, detail.instanceId).then(() => refresh());
      }
    };
    window.addEventListener("ginno:open-team-member", handler);
    return () => window.removeEventListener("ginno:open-team-member", handler);
  }, [teamId, refresh]);

  // ---- 当前视图要渲染的 session ----
  const sessionById = useCallback(
    (id: string | undefined | null): SessionMeta | null =>
      (id ? g.sessions.find((s) => s.id === id) ?? null : null),
    [g.sessions],
  );

  const mainSession = sessionById(team?.main_session_id);
  const memberSession =
    view.kind === "member" ? sessionById(view.sessionId) : null;

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
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      {/* 顶栏：团队选择器 + 视图切换 + 上卷摘要 */}
      <div className="flex h-12 shrink-0 items-center gap-3 border-b border-line bg-panel px-4">
        <Users className="h-4 w-4 shrink-0 text-violet" />
        {teams.length > 1 ? (
          <select
            value={team.id}
            onChange={(e) => {
              setTeamId(e.target.value);
              setView({ kind: "main" });
            }}
            className="rounded-md border border-line bg-card px-2 py-1 text-sm font-medium text-txt"
          >
            {teams.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        ) : (
          <span className="text-sm font-semibold">{team.name}</span>
        )}
        <span className="text-xs text-faint">
          {tc("team.memberCount", { count: team.member_rows?.length ?? 0 })}
        </span>

        {/* 视图切换：成员工作台时显示返回主会话 */}
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
          <button
            onClick={() => setNewTeamOpen(true)}
            className="rounded-md border border-line px-2 py-1 text-xs text-muted transition-colors hover:border-violet/50 hover:text-violet"
            title={tc("team.newTeam")}
          >
            <Plus className="h-3.5 w-3.5" />
          </button>
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
            onOpenMember={openMember}
            onAddMember={() => setAddOpen(true)}
          />
        ) : (
          <MemberPanelSkeleton />
        )}
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
