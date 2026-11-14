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
import { ArrowLeft, ArchiveRestore, Loader2, PanelRight, PenLine, Plus, Sparkles, Trash2, Users, WifiOff, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { t } from "@/i18n/provider";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { SessionMeta, Team, TeamMemberRow } from "@/lib/types";
import { ChatStream } from "@/components/chat/ChatStream";
import { MemberPanel, MemberPanelSkeleton } from "./MemberPanel";
import { AddMemberModal } from "./AddMemberModal";
import { NewTeamModal } from "./NewTeamModal";
import { TeamFromPromptModal, type TeamCreateMeta } from "./TeamFromPromptModal";
import { TeamMemoryModal } from "./TeamMemoryModal";
import { useEscClose } from "./useEscClose";
import { consumeOpenMemberIntent } from "./openMemberIntent";

function fmtAway(s: number): string {
  if (!s || s < 0) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${Math.round(s % 60)}s`;
  return `${Math.floor(m / 60)}h${m % 60}m`;
}

/** P2-1：GET /teams 的附加引用完整性字段（runtime 侧后加，旧 runtime 无）。 */
type TeamPayload = Team & { mainSessionMissing?: boolean };
const mainSessionMissing = (t: Team | null | undefined): boolean =>
  t ? (t as TeamPayload).mainSessionMissing === true : false;

/** P2-4 新建团队引导卡的「已读」标记（按团队 id 持久化）。 */
const GUIDE_DISMISS_KEY = (teamId: string) => `ginno-team-guide-dismissed:${teamId}`;
/** P2-15 未读汇总条：收起状态按团队持久化；「上次查看时间」是 away 时长
 *  的口径（成员 last_active 在成员跑着时恒≈1min，会误导）。 */
const DIGEST_COLLAPSE_KEY = (teamId: string) => `ginno-team-digest-collapsed:${teamId}`;
const LAST_VIEW_KEY = (teamId: string) => `ginno-team-last-view:${teamId}`;
/** P1-19 成员面板开合的手动覆盖（"1"/"0"；无记录 = 随窗口宽度自动）。 */
const PANEL_OPEN_KEY = "ginno-team-panel-open";

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
  // P2-2：一句话建队（先预览后创建）与创建回执（成员失败 / 首个任务投递）。
  const [promptOpen, setPromptOpen] = useState(false);
  const [createNotice, setCreateNotice] = useState<TeamCreateMeta | null>(null);
  // 团队管理：rename（输入态）与 disband（两步确认）都在行内完成，不引大弹窗。
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameText, setRenameText] = useState("");
  const [confirmDisbandId, setConfirmDisbandId] = useState<string | null>(null);
  const [archivedOpen, setArchivedOpen] = useState(false);
  const [menuId, setMenuId] = useState<string | null>(null);
  // 记忆分层（§5.4）：TEAM.md 编辑（团队级）与 notes.md 编辑（实例级）。
  const [mdEditorFor, setMdEditorFor] = useState<string | null>(null);
  const [notesEditorFor, setNotesEditorFor] = useState<string | null>(null);
  // 团队 uuid 复制反馈（对齐 session TurnIdChip 的「✓ 已复制」模式）
  const [idCopied, setIdCopied] = useState(false);
  // 总览过滤（设计稿视图 2：全部 / 仅活跃 / 需要我）+ 未读横幅收起
  const [ovFilter, setOvFilter] = useState<"all" | "active" | "needs">("all");
  // P2-15：收起按团队持久化；awaySince = 上次查看本团队的时间（秒）。
  const [digestCollapsed, setDigestCollapsed] = useState<Record<string, boolean>>({});
  const [awaySince, setAwaySince] = useState<number | null>(null);
  // P2-18：用户可见的错误提示（rename/patch/解散/刷新失败）+ 离线横幅。
  const [notice, setNotice] = useState<string | null>(null);
  const [offline, setOffline] = useState(false);
  // P1-19：窄窗口（<1280px）成员面板抽屉化；手动开合持久化覆盖自动判定。
  const [wide, setWide] = useState(true);
  const [panelOverride, setPanelOverride] = useState<boolean | null>(null);
  // P2-4：新建团队引导卡按团队 id 记录「已读」（localStorage，跨启动保留）
  const [guideDismissed, setGuideDismissed] = useState<Record<string, boolean>>({});

  const team = useMemo(
    () => teams.find((t) => t.id === teamId) ?? null,
    [teams, teamId],
  );

  // 切换团队时从 localStorage 恢复该团队的引导卡「已读」状态（私密窗口
  // 可能抛错 → 当作未读，功能不依赖存储）。
  useEffect(() => {
    if (!teamId) return;
    let dismissed = false;
    try {
      dismissed = localStorage.getItem(GUIDE_DISMISS_KEY(teamId)) === "1";
    } catch {
      /* 存储不可用 */
    }
    setGuideDismissed((prev) =>
      prev[teamId] === dismissed ? prev : { ...prev, [teamId]: dismissed },
    );
  }, [teamId]);

  const dismissGuide = useCallback((id: string) => {
    setGuideDismissed((prev) => ({ ...prev, [id]: true }));
    try {
      localStorage.setItem(GUIDE_DISMISS_KEY(id), "1");
    } catch {
      /* 存储不可用——本次会话内仍然生效 */
    }
  }, []);

  // P2-17：Esc 关行内菜单（模态各自的 Esc 在各模态组件里，栈顶优先）。
  useEscClose(() => setMenuId(null), menuId !== null);

  // ---- P2-15：未读汇总条的「离开时长」与收起持久化 ----
  // away 口径 = 上次查看本团队的时间（进入时读旧值、记录本次；离开/切团队/
  // 页面隐藏时刷新时间戳）。首次查看无旧值 → awaySince=null，标题换无时长版。
  useEffect(() => {
    if (!teamId) return;
    let collapsed = false;
    let lastView = 0;
    try {
      collapsed = localStorage.getItem(DIGEST_COLLAPSE_KEY(teamId)) === "1";
      lastView = Number(localStorage.getItem(LAST_VIEW_KEY(teamId)) || 0);
    } catch {
      /* 存储不可用 */
    }
    setDigestCollapsed((prev) =>
      prev[teamId] === collapsed ? prev : { ...prev, [teamId]: collapsed },
    );
    setAwaySince(lastView > 0 ? lastView : null);
    const stamp = () => {
      try {
        localStorage.setItem(LAST_VIEW_KEY(teamId), String(Math.floor(Date.now() / 1000)));
      } catch {
        /* noop */
      }
    };
    stamp(); // 本次查看开始
    const onHide = () => {
      if (document.visibilityState === "hidden") stamp();
    };
    document.addEventListener("visibilitychange", onHide);
    window.addEventListener("beforeunload", stamp);
    return () => {
      stamp(); // 本次查看结束（切团队 / 离开页面）
      document.removeEventListener("visibilitychange", onHide);
      window.removeEventListener("beforeunload", stamp);
    };
  }, [teamId]);

  const setDigestOpenFor = useCallback((id: string, open: boolean) => {
    setDigestCollapsed((prev) => ({ ...prev, [id]: !open }));
    try {
      localStorage.setItem(DIGEST_COLLAPSE_KEY(id), open ? "0" : "1");
    } catch {
      /* 存储不可用——本次会话内仍然生效 */
    }
  }, []);

  // ---- P1-19：窗口宽度判定 + 成员面板开合 ----
  // minWidth 900 − 侧栏 288 − 团队左栏 − 面板 300 会把对话列挤到 ~100px；
  // <1280px 时面板默认收起、打开走抽屉（overlay），对话列永远可用。
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1280px)");
    const apply = () => setWide(mq.matches);
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, []);

  useEffect(() => {
    try {
      const v = localStorage.getItem(PANEL_OPEN_KEY);
      if (v === "1" || v === "0") setPanelOverride(v === "1");
    } catch {
      /* 存储不可用——跟随宽度自动判定 */
    }
  }, []);

  const panelOpen = panelOverride ?? wide;
  const setPanel = useCallback((open: boolean) => {
    setPanelOverride(open);
    try {
      localStorage.setItem(PANEL_OPEN_KEY, open ? "1" : "0");
    } catch {
      /* noop */
    }
  }, []);

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
  const refresh = useCallback(async (): Promise<Team[]> => {
    try {
      const r = await api.listTeams();
      const list = r.teams ?? [];
      setTeams(list);
      setOffline(false);
      // 默认选第一个未归档团队（或保持当前选择）。
      setTeamId((cur) =>
        cur && list.some((t) => t.id === cur) ? cur : list.find((t) => !t.archived)?.id ?? list[0]?.id ?? null,
      );
      return list;
    } catch {
      // P2-18：sidecar 未起——保留上次数据，但不再静默：离线横幅 +
      // 错误提示告诉用户「这是缓存」，可手动重试。
      setOffline(true);
      setNotice(tc("team.refreshFailed"));
      return [];
    } finally {
      setLoaded(true);
    }
  }, [tc]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 成员/团队增删后，会话列表也要刷新（成员 session 是真实 session）。
  const afterMutate = useCallback(async () => {
    await Promise.all([refresh(), g.reloadSessions()]);
  }, [refresh, g]);

  // P2-2：一句话建队成功后——关弹窗、刷新列表、选中新团队、留下回执。
  // 后端通常直接回 team；若旧 runtime 没带，就按名称从刷新后的列表里找。
  const handlePromptCreated = useCallback(
    async (created: Team | null, meta: TeamCreateMeta) => {
      setPromptOpen(false);
      const list = await refresh();
      void g.reloadSessions();
      const picked = created ?? list.find((t) => !t.archived && t.name === meta.name) ?? null;
      if (picked) setTeamId(picked.id);
      setView({ kind: "main" });
      setCreateNotice(meta);
    },
    [refresh, g],
  );

  // ---- 团队管理动作 ----
  const patchTeamLocal = useCallback(
    async (id: string, patch: Record<string, unknown>) => {
      // P2-18：rename/archive 失败不再裸奔——ok:false 与异常都可见。
      try {
        const r = await api.patchTeam(id, patch);
        if (!r.ok) {
          setNotice(r.error || tc("team.patchFailed"));
          return;
        }
        await refresh();
      } catch {
        setNotice(tc("team.patchFailed"));
      }
    },
    [refresh, tc],
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
        if (!r.ok) {
          // P1-16：防呆回执改用内联错误条——alert() 在 Tauri webview 不可靠
          // （ConfirmModal docstring 的仓库纪律）。点名正在运行的成员。
          setNotice(
            r.live?.length && !force
              ? tc("team.disbandBlocked", { names: r.live.join(t(", ", "、")) })
              : r.error || tc("team.disbandFailed"),
          );
          return;
        }
      } catch {
        setNotice(tc("team.disbandFailed"));
        return;
      }
      if (teamId === id) setTeamId(null);
      await afterMutate();
    },
    [teamId, afterMutate, tc],
  );

  // ---- 订阅主会话 WS 的 team.changed（状态回流，§5.3）----
  // 只订阅当前团队主会话；事件里带完整 team payload，直接替换本地那份。
  // P1-11：断线自动重连（指数退避 1s→30s 封顶，照 useChatStreamEngine 的
  // onclose→setTimeout 重连范式）；重连成功后全量重拉团队数据——断线期间
  // 错过的 team.changed 无法补帧，只有重拉能对账。sidecar 重启不再永久 stale。
  const mainSid = team?.main_session_id;
  useEffect(() => {
    if (!mainSid) return;
    // 守卫后固化成 string：TS 不把外层 narrowing 带进下面嵌套的 connect()
    // 闭包，直接用 mainSid 会报 string|undefined（真实 tsc 抓到）。
    const sid: string = mainSid;
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;
    let attempt = 0;
    let dropped = false; // 是否断过线（重连成功才需要全量重拉）

    function scheduleReconnect() {
      if (disposed) return;
      const delay = Math.min(30_000, 1000 * 2 ** attempt);
      attempt += 1;
      timer = setTimeout(connect, delay);
    }

    function connect() {
      if (disposed) return;
      try {
        ws = api.openSessionSocket(sid);
      } catch {
        scheduleReconnect(); // 预渲染等无 WebSocket 环境
        return;
      }
      ws.onopen = () => {
        attempt = 0;
        setOffline(false);
        if (dropped) void refresh();
      };
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
      ws.onerror = () => {
        try {
          ws?.close();
        } catch {
          /* noop */
        }
      };
      ws.onclose = () => {
        if (disposed) return;
        dropped = true;
        setOffline(true);
        scheduleReconnect();
      };
    }

    connect();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      try {
        ws?.close();
      } catch {
        /* noop */
      }
    };
  }, [mainSid, refresh]);

  // ---- 成员工作台跳转（TeamReplyCard 的「进入工作台」也走这个事件）----
  const openMemberSession = useCallback(
    (instanceId: string, sessionId: string, teamIdOverride?: string) => {
      // P2-25：跨团队跳转（回帖卡带 team_id）先选中团队再进工作台。
      const tid = teamIdOverride ?? teamId;
      if (teamIdOverride && teamIdOverride !== teamId) setTeamId(teamIdOverride);
      setView({ kind: "member", instanceId, sessionId });
      // 打开即标记 seen：done → idle（§3.3 收件箱语义）。P2-18：seen 失败
      // 不阻塞跳转（下次刷新会对账），不再裸 .then。
      if (tid) {
        void api
          .markTeamMemberSeen(tid, instanceId)
          .then(() => refresh())
          .catch(() => {});
      }
    },
    [teamId, refresh],
  );

  const openMember = useCallback(
    (row: TeamMemberRow) => openMemberSession(row.instance_id, row.session_id),
    [openMemberSession],
  );

  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as {
        sessionId?: string;
        instanceId?: string;
        teamId?: string;
      };
      if (detail?.sessionId && detail?.instanceId) {
        openMemberSession(detail.instanceId, detail.sessionId, detail.teamId);
      }
    };
    window.addEventListener("ginno:open-team-member", handler);
    return () => window.removeEventListener("ginno:open-team-member", handler);
  }, [openMemberSession]);

  // P2-25：在 /team 之外点「进入工作台」——TeamReplyCard 先存一次性意图
  // 再导航过来；挂载时消费（sessionStorage，读后即清，不会误重放）。
  useEffect(() => {
    const intent = consumeOpenMemberIntent();
    if (!intent) return;
    if (intent.teamId) setTeamId(intent.teamId);
    setView({ kind: "member", instanceId: intent.instanceId, sessionId: intent.sessionId });
    if (intent.teamId) {
      void api
        .markTeamMemberSeen(intent.teamId, intent.instanceId)
        .then(() => refresh())
        .catch(() => {});
    }
    // 只在挂载时消费一次；refresh 变化不得导致意图重放。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- 当前视图要渲染的 session ----
  const sessionById = useCallback(
    (id: string | undefined | null): SessionMeta | null =>
      (id ? g.sessions.find((s) => s.id === id) ?? null : null),
    [g.sessions],
  );

  const mainSession = sessionById(team?.main_session_id);
  const memberSession =
    view.kind === "member" ? sessionById(view.sessionId) : null;
  // P2-1/P2-3：主会话引用完整性 + 共享工作区（都在 header/横幅里展示）。
  const mainMissing = mainSessionMissing(team);
  const wsPath = (team?.shared_workspace?.workspace ?? "").trim();

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
              if (e.key === "Escape") {
                e.stopPropagation();
                setRenamingId(null);
              }
            }}
            className="w-full rounded border border-violet/50 bg-card px-1.5 py-1 text-xs text-txt outline-none"
          />
        </div>
      );
    }
    return (
      <div
        key={t.id}
        role="button"
        tabIndex={0}
        aria-current={sel ? "true" : undefined}
        title={`${t.name} · ${t.id}`}
        className={`group relative flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1.5 text-xs outline-none transition-colors focus-visible:ring-1 focus-visible:ring-violet/60 ${
          sel ? "bg-violet/15 text-txt" : "text-muted hover:bg-card"
        } ${t.archived ? "opacity-55" : ""}`}
        onClick={() => {
          setTeamId(t.id);
          setView({ kind: "main" });
          setMenuId(null);
        }}
        onKeyDown={(e) => {
          // P2-17：键盘选中团队行。只响应行自身聚焦时的按键——行内按钮
          // （⋯ 菜单等）的 Enter/Space 冒泡上来不得误触发选团队。
          if (e.target !== e.currentTarget) return;
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setTeamId(t.id);
            setView({ kind: "main" });
            setMenuId(null);
          }
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
        {/* 行内菜单（hover / 键盘 focus 都可见——P2-17） */}
        <button
          onClick={(e) => {
            e.stopPropagation();
            setMenuId(menuId === t.id ? null : t.id);
          }}
          className={`shrink-0 rounded p-0.5 text-faint opacity-0 transition-opacity hover:text-txt focus-visible:opacity-100 group-hover:opacity-100 group-focus-within:opacity-100 ${menuId === t.id ? "opacity-100" : ""}`}
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
                setMdEditorFor(t.id);
              }}
            >
              <span className="w-3 text-center text-[10px]">📝</span>
              {tc("team.teamMd")}
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
        <div className="flex items-center gap-2">
          <button
            onClick={() => setNewTeamOpen(true)}
            className="flex items-center gap-1.5 rounded-md border border-line2 px-3 py-1.5 text-xs text-txt transition-colors hover:border-violet/50 hover:text-violet"
          >
            <Plus className="h-3.5 w-3.5" />
            {tc("team.newTeam")}
          </button>
          <button
            onClick={() => setPromptOpen(true)}
            className="flex items-center gap-1.5 rounded-md border border-line2 px-3 py-1.5 text-xs text-txt transition-colors hover:border-violet/50 hover:text-violet"
          >
            <Sparkles className="h-3.5 w-3.5" />
            {tc("team.promptCreate")}
          </button>
        </div>
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
        {promptOpen && (
          <TeamFromPromptModal
            onClose={() => setPromptOpen(false)}
            onCreated={handlePromptCreated}
          />
        )}
      </div>
    );
  }

  return (
    <div className="flex min-h-0 min-w-0 flex-1">
      {/* 左栏：团队列表（本空间的导航与会话管理入口）。P1-19：窄窗口
          收窄到 176px，给对话列多让 32px。 */}
      <aside className="flex w-44 shrink-0 flex-col border-r border-line bg-panel xl:w-52">
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
          {/* P2-3：共享工作区必须可见——空配置时成员会各写各的目录，
              团队协作语义不成立，这里给出可补救的提示。 */}
          {wsPath ? (
            <span
              className="hidden max-w-[280px] truncate rounded border border-line px-1.5 py-0.5 text-[10px] text-faint lg:inline-block"
              title={tc("team.workspacePathTitle", { path: wsPath })}
            >
              📁 {wsPath}
            </span>
          ) : (
            <span
              className="hidden rounded border border-dashed border-line px-1.5 py-0.5 text-[10px] text-faint lg:inline-block"
              title={tc("team.workspaceUnsetHint")}
            >
              {tc("team.workspaceUnset")}
            </span>
          )}
          {/* 团队共享记忆非空 → 显示已注入 chip（点击进编辑） */}
          {team.has_team_md && (
            <button
              onClick={() => setMdEditorFor(team.id)}
              className="rounded-full border border-line px-2 py-0.5 text-[10px] text-faint transition-colors hover:border-violet/40 hover:text-violet"
              title={tc("team.teamMdChipTitle")}
            >
              👥 TEAM.md
            </button>
          )}

          {view.kind === "member" && (
            <button
              onClick={() => setView({ kind: "main" })}
              className="ml-2 flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs text-muted transition-colors hover:border-violet/50 hover:text-violet"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              {tc("team.backToMain")}
            </button>
          )}

          <button
            onClick={() => setPromptOpen(true)}
            className="flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[11px] text-muted transition-colors hover:border-violet/50 hover:text-violet"
            title={tc("team.promptTitle")}
          >
            <Sparkles className="h-3.5 w-3.5" />
            {tc("team.promptCreate")}
          </button>

          <div className="ml-auto flex items-center gap-2">
            {team.rollup?.needs_action && (
              <span className="rounded-full border border-yellow-500/40 bg-yellow-500/10 px-2 py-0.5 text-[11px] text-yellow-500">
                {team.rollup.blocked > 0
                  ? tc("team.rollupBlocked", { n: team.rollup.blocked })
                  : tc("team.rollupDone", { n: team.rollup.done_unread })}
              </span>
            )}
            {/* P1-19：成员面板开合（窄窗口下是抽屉开关；宽窗口可收起让
                对话列更宽）。手动选择持久化，覆盖宽度自动判定。 */}
            <button
              onClick={() => setPanel(!panelOpen)}
              aria-pressed={panelOpen}
              className={`flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] transition-colors ${
                panelOpen
                  ? "border-violet/50 bg-violet/10 text-violet"
                  : "border-line text-muted hover:border-violet/50 hover:text-violet"
              }`}
              title={tc("team.togglePanel")}
            >
              <PanelRight className="h-3.5 w-3.5" />
              <span className="tabular-nums">
                {team.member_rows?.length ?? team.members?.length ?? 0}
              </span>
            </button>
          </div>
        </div>

        {/* P2-18：操作失败的内联错误条（替代 alert，可手动关闭）。 */}
        {notice && (
          <div className="mx-4 mt-2 flex items-start gap-2 rounded-lg border border-red/40 bg-red/10 px-3 py-2 text-xs text-red">
            <span className="min-w-0 flex-1 leading-relaxed">{notice}</span>
            <button
              onClick={() => setNotice(null)}
              className="shrink-0 transition-opacity hover:opacity-70"
              title={tc("team.guideDismiss")}
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        )}

        {/* P2-18/P1-11：离线横幅——WS 断开或拉取失败时明示「正在重连」，
            数据是缓存；自动重连之外给一个手动重试。 */}
        {offline && (
          <div className="mx-4 mt-2 flex items-center gap-2 rounded-lg border border-yellow-500/40 bg-yellow-500/10 px-3 py-1.5 text-[11px] text-yellow-500">
            <WifiOff className="h-3.5 w-3.5 shrink-0" />
            <span className="min-w-0 flex-1">{tc("team.offlineBanner")}</span>
            <button
              onClick={() => void refresh()}
              className="shrink-0 rounded border border-yellow-500/40 px-2 py-0.5 transition-colors hover:bg-yellow-500/10"
            >
              {tc("team.retry")}
            </button>
          </div>
        )}

        {/* P2-1：主会话被删 → 团队协作不可用。横幅常驻在顶栏下方，三个视图
            都能看到；不静默打开一个空会话。 */}
        {mainMissing && (
          <div className="mx-4 mt-2 rounded-lg border border-red/40 bg-red/10 px-3 py-2 text-xs leading-relaxed text-red">
            <div className="font-medium">{tc("team.mainMissingBanner")}</div>
            <div className="mt-0.5 text-[11px] text-red/80">
              {tc("team.mainMissingHint")}
            </div>
          </div>
        )}

        {/* P2-2 创建回执：成员失败与首个任务是否投递都是可行动信息，不静默
            吞掉；可手动关闭。 */}
        {createNotice && (
          <div className="mx-4 mt-2 flex items-start gap-2 rounded-lg border border-green/30 bg-green/5 px-3 py-2 text-xs">
            <div className="min-w-0 flex-1 space-y-0.5">
              <div className="font-medium text-green">
                {tc("team.promptCreatedNotice", { name: createNotice.name })}
              </div>
              {createNotice.memberErrors.length > 0 && (
                <div className="text-yellow">
                  {tc("team.promptMemberErrors", { errors: createNotice.memberErrors.join("; ") })}
                </div>
              )}
              {createNotice.firstTaskRequested && (
                <div className={createNotice.firstTaskDelivered ? "text-muted" : "text-yellow"}>
                  {tc(
                    createNotice.firstTaskDelivered
                      ? "team.promptFirstTaskDelivered"
                      : "team.promptFirstTaskNotDelivered",
                  )}
                </div>
              )}
            </div>
            <button
              onClick={() => setCreateNotice(null)}
              className="shrink-0 text-[11px] text-faint transition-colors hover:text-muted"
            >
              {tc("team.guideDismiss")}
            </button>
          </div>
        )}

        {/* 主体：左对话（主会话 or 成员工作台）+ 右成员面板。
            P1-19：relative 容器——窄窗口下面板以绝对定位抽屉覆盖，
            不再作为 flex 兄弟挤占对话列宽度。 */}
        <div className="relative flex min-h-0 flex-1">
          <div className="flex min-w-0 flex-1 flex-col">
            {view.kind === "main" ? (
              <>
                {/* 未读汇总横幅（设计稿视图 1 顶部）：done 未读 + blocked
                    需要你，一屏收拢「谁在等我」。P2-15：away 时长用「上次
                    查看本团队的时间」（成员 last_active 在跑时恒≈1min，误导）；
                    收起按团队持久化，收起后留一行可重新展开的细条。 */}
                {(() => {
                  const rows = team.member_rows ?? [];
                  const notables = rows.filter(
                    (r) => r.status === "done" || r.status === "blocked",
                  );
                  if (notables.length === 0) return null;
                  if (digestCollapsed[team.id]) {
                    return (
                      <div className="mx-4 mt-2 flex items-center gap-2 rounded-lg border border-line bg-card px-3 py-1.5 text-xs">
                        <span>⏱</span>
                        <span className="min-w-0 flex-1 truncate text-muted">
                          {tc("team.digestCollapsedLine", { n: notables.length })}
                        </span>
                        <button
                          onClick={() => setDigestOpenFor(team.id, true)}
                          className="shrink-0 text-[11px] text-violet hover:underline"
                        >
                          {tc("team.digestExpand")}
                        </button>
                      </div>
                    );
                  }
                  const awayMin = awaySince
                    ? Math.max(1, Math.round((Date.now() / 1000 - awaySince) / 60))
                    : null;
                  return (
                    <div className="mx-4 mt-2 rounded-lg border border-line bg-card px-3 py-2 text-xs">
                      <div className="flex items-center gap-2">
                        <span>⏱</span>
                        <span className="flex-1 font-medium text-txt">
                          {awayMin === null
                            ? tc("team.digestTitleNoAway")
                            : tc("team.digestTitle", { min: awayMin })}
                        </span>
                        <button
                          onClick={() => setDigestOpenFor(team.id, false)}
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
                                ? tc("team.digestBlocked", {
                                    time: fmtAway(
                                      r.last_active > 0
                                        ? Math.max(0, Date.now() / 1000 - r.last_active)
                                        : r.elapsed_s,
                                    ),
                                  })
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
                {/* P2-4：新建团队引导——空会话里用户不知道下一步做什么。
                    成员为空或团队创建不足 1 天时出现，可按团队 id 关掉。 */}
                {!guideDismissed[team.id] &&
                  ((team.member_rows?.length ?? team.members?.length ?? 0) === 0 ||
                    (!!team.created && Date.now() / 1000 - team.created < 86400)) && (
                    <div className="mx-4 mt-2 flex items-start gap-2 rounded-lg border border-violet/30 bg-violet/5 px-3 py-2 text-xs">
                      <span className="min-w-0 flex-1 leading-relaxed text-muted">
                        {tc("team.guideCard", { name: team.name })}
                      </span>
                      <button
                        onClick={() => dismissGuide(team.id)}
                        className="shrink-0 text-[11px] text-faint transition-colors hover:text-muted"
                      >
                        {tc("team.guideDismiss")}
                      </button>
                    </div>
                  )}
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
                        {row?.has_notes && (
                          <span title={tc("team.notesChipTitle")}>📝 {tc("team.notesChip")}</span>
                        )}
                      </>
                    );
                  })()}
                  <button
                    onClick={() => setNotesEditorFor(view.instanceId)}
                    className="ml-auto rounded border border-line px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:border-violet/40 hover:text-violet"
                    title={tc("team.notesEditTitle")}
                  >
                    📝 {tc("team.notesEdit")}
                  </button>
                </div>
                <ChatStream session={memberSession} />
              </>
            ) : (
              <div className="flex flex-1 items-center justify-center text-xs text-faint">
                {tc("team.memberSessionMissing")}
              </div>
            )}
          </div>

          {/* P1-19：宽窗口 = 常驻右栏；窄窗口 = 抽屉（遮罩 + 右滑面板，
              点遮罩/关闭按钮收起）。面板收起时对话列独占全宽。 */}
          {panelOpen &&
            (team.member_rows ? (
              wide ? (
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
                <div className="absolute inset-0 z-30 flex justify-end">
                  <button
                    className="min-w-0 flex-1 bg-black/40"
                    onClick={() => setPanel(false)}
                    aria-label={tc("team.closePanel")}
                  />
                  <MemberPanel
                    team={team}
                    activeInstanceId={view.kind === "member" ? view.instanceId : null}
                    activeSessionId={view.kind === "member" ? view.sessionId : null}
                    onOpenMember={openMember}
                    onOpenMemberSession={openMemberSession}
                    onMutate={afterMutate}
                    onAddMember={() => setAddOpen(true)}
                    overlay
                    onClose={() => setPanel(false)}
                  />
                </div>
              )
            ) : (
              wide && <MemberPanelSkeleton />
            ))}
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
      {promptOpen && (
        <TeamFromPromptModal
          onClose={() => setPromptOpen(false)}
          onCreated={handlePromptCreated}
        />
      )}
      {mdEditorFor && (
        <TeamMemoryModal
          title={tc("team.teamMdTitle")}
          hint={tc("team.teamMdHint")}
          load={async () => (await api.getTeamMd(mdEditorFor)).text ?? ""}
          save={async (text) => (await api.putTeamMd(mdEditorFor, text)).ok}
          onClose={() => {
            setMdEditorFor(null);
            void refresh();
          }}
        />
      )}
      {notesEditorFor && (
        <TeamMemoryModal
          title={tc("team.notesTitle", {
            name:
              team.member_rows?.find((r) => r.instance_id === notesEditorFor)
                ?.display_name ?? notesEditorFor,
          })}
          hint={tc("team.notesHint")}
          load={async () => (await api.getMemberNotes(team.id, notesEditorFor)).text ?? ""}
          save={async (text) => (await api.putMemberNotes(team.id, notesEditorFor, text)).ok}
          onClose={() => {
            setNotesEditorFor(null);
            void refresh();
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
  // P2-16：总览卡的 elapsed 同样本地走字（快照值一到前端就冻结）。
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, []);
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
                  {fmtAway(
                    r.last_active > 0
                      ? Math.max(0, Date.now() / 1000 - r.last_active)
                      : r.elapsed_s,
                  )}
                </span>
              </div>
              {/* 最近活动摘要（权威 status_detail，不前端二次判态）
                 注意：整张卡是 <button>，WebKit 里 button 内的文字不可选中，
                 data-selectable 在这里是无效声明，故不加；要可复制需把卡片
                 改成 div + 键盘语义，超出本次改造范围。 */}
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

      {/* P2-19：教育性脚注——@ 的投递语义（原型 @ 菜单脚注的同款文案，
          @ 菜单本体 ComposerMenu 在本批次边界外，先落在总览/成员面板）。 */}
      <div className="mt-3 text-[10px] leading-relaxed text-faint">
        {tc("team.atDeliveryNote")}
      </div>
    </div>
  );
}
