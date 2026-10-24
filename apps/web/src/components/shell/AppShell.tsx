"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { GoalEditor } from "@/components/shell/GoalChip";
import { useGinno, LAST_SESSION_KEY, notifyScheduleRunFinished } from "@/lib/store";
import { openSocket, wsConnectorsUrl, wsScheduleUrl } from "@/lib/runtime";
import { setKeepAwake } from "@/lib/desktop";
import * as api from "@/lib/runtime";
import { applyTheme } from "@/components/settings/GeneralSettings";
import { TopBar } from "@/components/shell/TopBar";
import { SessionSearchModal } from "@/components/shell/SessionSearchModal";
import { rememberMessageTarget } from "@/components/shell/searchShared";
import { ChatStream } from "@/components/chat/ChatStream";
import { RunSubSessionView } from "@/components/chat/RunSubSessionView";
import { ScheduleRunView } from "@/components/chat/ScheduleRunView";
import { SheetViewer } from "@/components/chat/SheetViewer";
import { ModToastHost } from "@/components/chat/mod/ModToastHost";
import { NoticeHost } from "@/components/notice/NoticeHost";
import { RightPanel } from "@/components/right/RightPanel";
import { RightDock } from "@/components/right/RightDock";
import { SidebarArchive } from "@/components/shell/SidebarArchive";
import { SidebarBulkBar } from "@/components/shell/SidebarBulkBar";
import type { BulkSkippedItem } from "@/components/shell/SidebarBulkBar";
import { SidebarConfirmModals } from "@/components/shell/SidebarConfirmModals";
import { SidebarFooter } from "@/components/shell/SidebarFooter";
import { SidebarHeader } from "@/components/shell/SidebarHeader";
import { SidebarRunRow } from "@/components/shell/SidebarRunRow";
import { SidebarSessionList } from "@/components/shell/SidebarSessionList";
import { SidebarSessionRow } from "@/components/shell/SidebarSessionRow";
import { useSessionGrouping } from "@/components/shell/useSessionGrouping";
import type { TimeGroup } from "@/components/shell/useSessionGrouping";
import type { FolderEntry, ScheduleRun, SessionMeta, SessionUsage, WorkflowRun } from "@/lib/types";

// 侧栏偏好（均 localStorage 持久化；读写都吞异常，隐私模式/无 storage 时退回默认）。
const ARCHIVED_OPEN_KEY = "ginno-sidebar-archived-open";
const GROUP_VIEW_KEY = "ginno-sidebar-group-view";

export function AppShell({ children }: { children: React.ReactNode }) {
  const g = useGinno();
  // shell 域文案（侧栏项目列表/导航按钮/窗口控件提示/菜单/空态引导）
  const t = useTranslations("shell");
  const pathname = usePathname();
  const router = useRouter();
  // inline session rename (double-click title or pencil icon)
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const cancelRename = useRef(false);
  const [deleteTarget, setDeleteTarget] = useState<SessionMeta | null>(null);
  const confirmDelete = () => {
    // 有后代时级联删除（subagent-design.md §5.8）：后端沿 parent_session_id
    // 一并停止/删除；store 的乐观移除同步清掉后代行。
    if (deleteTarget) {
      g.removeSession(deleteTarget.id, { cascade: descendantCount(deleteTarget.id) > 0 });
    }
    setDeleteTarget(null);
  };
  // C+ 方案③：侧边栏按 agent 筛选会话（null = 全部，再点一次取消）
  const [agentFilter, setAgentFilter] = useState<string | null>(null);
  // 子会话三档分节的折叠覆盖（2026-10-12 收纳优化）：key = "<父会话>:<桶>"。
  // 父级展开/折叠本身在 store（treeCollapsed）；节级折叠是会话内的 UI 态，
  // 刷新后回到默认（运行中/失败展开、成功收起）即可。单子会话不分节。
  const [subGroupFold, setSubGroupFold] = useState<Record<string, boolean>>({});
  // 子会话树的展开/折叠覆盖（subagent-design.md §6.1）已上收到 store
  // （g.treeCollapsed / g.setTreeCollapsed，ginno-sidebar-tree 持久化）；
  // 语义不变：undefined = 跟随默认，true = 用户折叠，false = 用户显式展开。

  // Goal-first session (goal-design.md P2): create a session titled by the
  // objective and immediately set it as the active goal so the driver starts.
  const [goalSessionModal, setGoalSessionModal] = useState(false);
  const onGoalSession = async (objective: string) => {
    const title = objective.length > 40 ? objective.slice(0, 40) + "…" : objective;
    const s = await g.newSession(g.agents[0]?.id, { title });
    if (s) {
      await g.setGoalObjective(s.id, objective);
      setGoalSessionModal(false);
      router.push("/");
    }
  };

  // ── Workspace state lifted here so ChatStream is always mounted ──────────
  // ChatStream holds per-session WebSocket connections, message store, error
  // cards, and draft text in useRef. If it lived inside the "/" page it would
  // unmount whenever the user navigates to /settings or /kb, wiping all that
  // state (including the in-flight retry affordance on error cards). By keeping
  // the workspace here and toggling visibility with `hidden`, ChatStream's refs
  // survive any route change.
  const [running, setRunning] = useState(false);
  // Session-cumulative model usage (world-state-plan D2/D3), pushed up from
  // the chat socket and rendered as a small counter in the TopBar.
  const [usage, setUsage] = useState<SessionUsage | null>(null);
  const didInit = useRef(false);

  // Usage counters are per session: pull the session's accumulated stats on
  // switch so the TopBar counter is correct immediately (live `usage` WS
  // events keep updating it during turns).

  useEffect(() => {
    let alive = true;
    const check = async () => {
      try {
        const h = await api.health();
        if (alive) setRuntimeStale(!!(h as { runtime_stale?: boolean }).runtime_stale);
      } catch {
        /* sidecar down — the reconnect banner covers this case */
      }
    };
    void check();
    const id = setInterval(check, 60_000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    let alive = true;
    setUsage(null);
    if (g.activeSessionId) {
      api
        .getSessionUsage(g.activeSessionId)
        .then((r) => {
          if (alive) setUsage(r?.usage ?? null);
        })
        .catch(() => {
          /* sidecar down — live events will populate later */
        });
    }
    return () => {
      alive = false;
    };
  }, [g.activeSessionId]);

  // Sidebar ordering (reactivated sessions float up): the runtime bumps each
  // session's `updated` on every invoke, but a session reactivated from the
  // PIN window or another client bumps only the server copy — this window's
  // list would sit in its old day-group until restart. Cheap re-fetch on
  // focus (the list is one JSON read).
  useEffect(() => {
    const onFocus = () => void g.reloadSessions();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [g.reloadSessions]);

  useEffect(() => {
    if (didInit.current) return;
    if (!g.ready) return;
    didInit.current = true;
    // Restore the last-used session; otherwise stay on the landing home —
    // sessions are created lazily on first send (open-experience redesign).
    if (!g.activeSessionId && g.sessions.length) {
      let last: string | null = null;
      try {
        last = localStorage.getItem(LAST_SESSION_KEY);
      } catch {
        /* storage unavailable */
      }
      if (last && g.sessions.some((s) => s.id === last)) g.setActiveSession(last);
    }
  }, [g.ready, g.sessions, g.activeSessionId, g]);

  const session = g.sessions.find((s) => s.id === g.activeSessionId) ?? null;
  const agent = session ? g.agents.find((a) => a.id === session.agent_id) ?? null : null;
  // ─────────────────────────────────────────────────────────────────────────

  // "New session" = go home; the session itself is created on first send.
  const [searchOpen, setSearchOpen] = useState(false);
  // P0-2 运行时更新提示：bundle 在进程启动后被替换 → 提示重启。
  const [runtimeStale, setRuntimeStale] = useState(false);

  // ── 侧栏组织方式（P1-1/P1-2/P1-4）────────────────────────────────────────
  // 分组视图（时间/项目）与「已归档」折叠态都持久化。首帧用默认值避免 SSR/
  // 水合不一致，挂载后再读 storage；写入跳过首轮，免得默认值覆盖已存偏好。
  const [groupView, setGroupView] = useState<"time" | "project">("time");
  const [archivedOpen, setArchivedOpen] = useState(false);
  // P0-2 归档检索：已归档区展开时的本地搜索框，200ms 防抖后走
  // /api/search?scope=archived（标题命中 + 消息片段命中）。关键词本身留在
  // 这里——批量栏「恢复所选」与删除确认都会清空它；检索结果/防抖在
  // SidebarArchive 内部。
  const [archivedQuery, setArchivedQuery] = useState("");
  const sidebarPrefsLoaded = useRef(false);
  useEffect(() => {
    if (!sidebarPrefsLoaded.current) {
      sidebarPrefsLoaded.current = true;
      try {
        if (localStorage.getItem(ARCHIVED_OPEN_KEY) === "1") setArchivedOpen(true);
        if (localStorage.getItem(GROUP_VIEW_KEY) === "project") setGroupView("project");
      } catch {
        /* storage unavailable */
      }
      return;
    }
    try {
      localStorage.setItem(ARCHIVED_OPEN_KEY, archivedOpen ? "1" : "0");
      localStorage.setItem(GROUP_VIEW_KEY, groupView);
    } catch {
      /* storage unavailable */
    }
  }, [archivedOpen, groupView]);

  // 项目视图的 id → 名称映射：切到项目视图时按需拉一次文件夹库并缓存
  // （listFolders 失败就当作全部未分组，不阻塞侧栏）。
  const [folders, setFolders] = useState<FolderEntry[] | null>(null);
  useEffect(() => {
    if (groupView !== "project" || folders !== null) return;
    let alive = true;
    api
      .listFolders()
      .then((r) => {
        if (alive) setFolders(r.folders ?? []);
      })
      .catch(() => {
        if (alive) setFolders([]);
      });
    return () => {
      alive = false;
    };
  }, [groupView, folders]);

  // 多选清理（P1-4）：selectMode 下点行 = 勾选，不再打开会话。
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  // 删除被引用拒绝的会话（后端 skipped）：批量动作结束后仍留在侧栏提示。
  const [bulkNotice, setBulkNotice] = useState<BulkSkippedItem[]>([]);
  const [bulkError, setBulkError] = useState<string | null>(null);
  const [archiveAllConfirm, setArchiveAllConfirm] = useState(false);
  const [bulkDeleteConfirm, setBulkDeleteConfirm] = useState(false);
  // 已归档选中项的「删除所选」确认（只作用于归档子集，不动普通选中行）。
  const [archivedDeleteConfirm, setArchivedDeleteConfirm] = useState(false);
  const toggleSelect = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const exitSelect = () => {
    setSelectMode(false);
    setSelected(new Set());
  };
  const setActiveSessionForNav = g.setActiveSession;
  const goHome = () => {
    setActiveSessionForNav(null);
    if (pathname !== "/") router.push("/");
  };
  const onNewSession = goHome;

  // apply persisted theme as early as the shell mounts
  useEffect(() => {
    let theme = "dark";
    try {
      theme = localStorage.getItem("ginno-theme") || "dark";
    } catch {
      /* ignore */
    }
    applyTheme(theme);
  }, []);

  // Tauri shell bridges: clicking a native notification fires one of these via
  // webview.eval (apps/desktop/src/lib.rs) — same convention as ChatStream's
  // __ginnoFileDrop. AppShell stays mounted for the app's lifetime, including
  // while the window is hidden, so the globals are always registered.
  // Stable setters destructured out of `g` so the effect doesn't re-arm on
  // every provider render; the route is read live inside the handlers.
  const setActiveSession = g.setActiveSession;
  const setRightTab = g.setRightTab;
  const setRightPanelOpenForBridge = g.setRightPanelOpen;
  // 定时任务完成通知的点击回跳数据：run_finished 事件携带的行与用户点击通知
  // 之间可能相隔数分钟，按 run_id 缓存最近 20 条供回跳查；webview 重载后缓存
  // 为空 → 退回 /scheduled 执行记录页（Notification Center 里的旧通知仍可用）。
  const scheduleNotifyRowsRef = useRef<Map<string, ScheduleRun>>(new Map());
  const openScheduleRunFromNotifyRef = useRef<(runId: string) => void>(() => {});
  openScheduleRunFromNotifyRef.current = (runId: string) => {
    const row = scheduleNotifyRowsRef.current.get(runId);
    if (row && row.target_type === "prompt" && row.session_id) {
      // 与 RunsPanel.openRun 同语义：点开 = 已读（后端广播 runs_seen，状态点
      // 即时消除），并挂 ScheduleRunView 影子会话回放（不离开当前路由）。
      void api.markScheduleRunsSeen({ run_id: runId }).catch(() => {});
      g.openScheduleRun(row);
      return;
    }
    // 找不到行（重载后旧通知）或 workflow 目标：落到执行记录列表
    if (window.location.pathname !== "/scheduled") router.push("/scheduled");
  };
  useEffect(() => {
    const openSession = (sid: string) => {
      if (!sid) return;
      setActiveSession(sid);
      if (window.location.pathname !== "/") router.push("/");
      // ChatStream arms stick-to-bottom and scrolls on this event (the
      // session's history may still be loading — its switch effect and the
      // [messages] auto-scroll finish the job).
      window.dispatchEvent(new CustomEvent("ginno:focus-latest", { detail: sid }));
    };
    const openWorkflowRun = () => {
      // The right panel only renders on the workspace route.
      if (window.location.pathname !== "/") router.push("/");
      setRightTab("workflow"); // manual open also clears the panel badge
      setRightPanelOpenForBridge(true);
    };
    (window as unknown as { __ginnoOpenSession?: (sid: string) => void }).__ginnoOpenSession =
      openSession;
    (window as unknown as { __ginnoOpenWorkflowRun?: () => void }).__ginnoOpenWorkflowRun =
      openWorkflowRun;
    (window as unknown as { __ginnoOpenScheduleRun?: (runId: string) => void })
      .__ginnoOpenScheduleRun = (runId: string) => openScheduleRunFromNotifyRef.current(runId);
    return () => {
      delete (window as unknown as { __ginnoOpenSession?: unknown }).__ginnoOpenSession;
      delete (window as unknown as { __ginnoOpenWorkflowRun?: unknown }).__ginnoOpenWorkflowRun;
      delete (window as unknown as { __ginnoOpenScheduleRun?: unknown }).__ginnoOpenScheduleRun;
    };
  }, [setActiveSession, setRightTab, setRightPanelOpenForBridge, router]);

  const onWorkspace = pathname === "/";
  const onSettings = pathname.startsWith("/settings");
  const onKb = pathname.startsWith("/kb");
  const onWorkflows = pathname.startsWith("/workflows");
  const onConnectors = pathname.startsWith("/connectors");
  const onScheduled = pathname.startsWith("/scheduled");
  const onTeam = pathname.startsWith("/team");

  // Connector 聚合状态点(connector-module §2.1):有断连→黄,有错误→红,
  // 全部正常/未启用→不显示。10s 轮询即可——dot 只是个入口提示。
  const [connectorDot, setConnectorDot] = useState("");
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const r = await import("@/lib/runtime").then((m) => m.listConnectors());
        if (alive) setConnectorDot(r.aggregateDot || "");
      } catch { /* sidecar 未起时静默 */ }
    };
    tick();
    const iv = setInterval(tick, 10000);
    return () => {
      alive = false;
      clearInterval(iv);
    };
  }, []);

  // 定时回放不跟随路由(ScheduleRunView 挂在 main 顶层)——切到 Connectors/KB
  // 等其他页面时自动收起,否则回放挡住新页面、看起来像「跳转失灵」。打开回放
  // 本身不换路由,此 effect 不会误伤;workflow 分流的 push("/") 在 openRunView
  // 里已互斥清掉回放。ref 防止 store 重建导致的意外收起。
  const closeScheduleRunRef = useRef(g.closeScheduleRun);
  closeScheduleRunRef.current = g.closeScheduleRun;
  useEffect(() => {
    closeScheduleRunRef.current();
  }, [pathname]);

  // 连接器事件通道(#6「提示一次」):B 轨 fallback 首次发生时弹一条 toast,
  // 用户知情后可在 连接器 设置里改 off/auto。WS 断了静默——提示不是关键路径。
  const [fallbackToast, setFallbackToast] = useState<string | null>(null);
  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    try {
      ws = openSocket(wsConnectorsUrl());
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data) as { type: string; hint?: string };
          if (msg.type === "browser_fallback_used" && msg.hint) {
            setFallbackToast(msg.hint);
            // 8s 自动消失;一次性提示(sessionStorage 防重复打扰)
            if (!sessionStorage.getItem("ginno-fb-toast")) {
              sessionStorage.setItem("ginno-fb-toast", "1");
            }
            timer = setTimeout(() => setFallbackToast(null), 8000);
          }
        } catch { /* 非 JSON 帧 */ }
      };
      ws.onclose = () => { /* 提示非关键路径,不重连 */ };
    } catch { /* WebSocket 不可用(静态导出预渲染) */ }
    return () => {
      closed = true;
      if (timer) clearTimeout(timer);
      try { ws?.close(); } catch { /* already closed */ }
    };
  }, []);

  // 定时任务状态点（scheduled-tasks-design §3.1/§6 + schedule-unseen）：红 =
  // 存在未查看的执行结果（实心红圆 + 白色数字徽章，点开记录/全部已读后
  // 消除）；灰 = 全局关闭；无点 = 无未读。冷数据 GET 兜底（60s），运行中由
  // /api/ws/schedule 的 run_finished/missed/task_updated/runs_seen 事件即时
  // 刷新；挂这一条轻量 WS 只为状态点，页面级订阅归 /scheduled 自己（§6）。
  const [scheduleDot, setScheduleDot] = useState<"" | "off" | "error">("");
  const [scheduleUnseen, setScheduleUnseen] = useState<{ total: number; error: number; missed: number }>({
    total: 0,
    error: 0,
    missed: 0,
  });
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const cfg = await api.listSchedule();
        if (!alive) return;
        setScheduleUnseen(cfg.unseen ?? { total: 0, error: 0, missed: 0 });
        if (!cfg?.enabled) {
          setScheduleDot("off");
          return;
        }
        setScheduleDot((cfg.unseen?.total ?? 0) > 0 ? "error" : "");
      } catch {
        /* sidecar 未起时静默——保持上次状态 */
      }
    };
    tick();
    const iv = setInterval(tick, 60000);
    let ws: WebSocket | null = null;
    try {
      ws = openSocket(wsScheduleUrl());
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data) as { type?: string; run?: ScheduleRun };
          if (msg.type === "run_finished" && msg.run) {
            // 点击回跳数据先落缓存（通知点击可能晚于事件数分钟），再发通知
            // ——闸门/排除项（workflow 目标、skipped_overlap、窗口可见）在
            // notifyScheduleRunFinished 内统一判定。
            scheduleNotifyRowsRef.current.set(msg.run.run_id, msg.run);
            if (scheduleNotifyRowsRef.current.size > 20) {
              const oldest = scheduleNotifyRowsRef.current.keys().next().value;
              if (oldest !== undefined) scheduleNotifyRowsRef.current.delete(oldest);
            }
            notifyScheduleRunFinished(msg.run, (runId) =>
              openScheduleRunFromNotifyRef.current(runId),
            );
          }
          if (
            msg.type === "run_finished" ||
            msg.type === "missed" ||
            msg.type === "task_updated" ||
            msg.type === "runs_seen"
          ) {
            void tick();
          }
        } catch {
          /* 非 JSON 帧 */
        }
      };
      ws.onclose = () => {
        /* 状态点非关键路径，不重连（60s 轮询兜底） */
      };
    } catch {
      /* WebSocket 不可用（静态导出预渲染） */
    }
    return () => {
      alive = false;
      clearInterval(iv);
      try {
        ws?.close();
      } catch {
        /* already closed */
      }
    };
  }, []);

  // keep_awake 状态同步（scheduled-tasks-design §3.3）：runtime 的
  // schedules.json 是唯一真值；壳重启而 runtime 未动时由 web 启动加载后向壳
  // 同步一次。纯 web/dev 无 Tauri 时 setKeepAwake 直接返回 false，优雅降级。
  useEffect(() => {
    if (!g.ready) return;
    api
      .listSchedule()
      .then((cfg) => {
        // 无条件断言到真值（command 幂等）：false 时顺带清掉壳内可能残留的
        // caffeinate 子进程。
        if (cfg) void setKeepAwake(!!cfg.keep_awake);
      })
      .catch(() => {
        /* sidecar 未起 */
      });
  }, [g.ready]);

  // 侧栏分组派生数据（天分组 / 项目分组 / 归档 / 批量候选）——纯计算，
  // 逻辑细节见 useSessionGrouping。
  const {
    hiddenIds,
    childrenOf,
    runsOf,
    descendantCount,
    sortedSessions,
    pinnedSessions,
    sessionGroups,
    projectGroups,
    archivedSessions,
    archivedMonthGroups,
    archiveCandidates,
    selectedArchivedIds,
  } = useSessionGrouping({
    sessions: g.sessions,
    workflowRuns: g.workflowRuns,
    agentFilter,
    folders,
    groupView,
    selected,
    ungroupedLabel: t("groups.ungrouped"),
  });

  // 天分组标题的本地化展示名（内部 key 保持英文，分组/比较逻辑不受影响）。
  const groupLabel: Record<TimeGroup, string> = {
    Today: t("groups.today"),
    Yesterday: t("groups.yesterday"),
    Last7: t("groups.last7"),
    Earlier: t("groups.earlier"),
  };

  // P1-4 批量动作：归档/取消归档/删除共用。删除被引用时后端会跳过并回报
  // skipped —— 这里把标题和引用落成侧栏提示，其余情况清空旧提示。
  const runBulk = async (action: "archive" | "unarchive" | "delete", idsOverride?: string[]) => {
    const ids = idsOverride ?? [...selected];
    if (!ids.length || bulkBusy) return;
    // 删除后 store 会把行移除，先快照标题供 skipped 提示使用。
    const titles = new Map(g.sessions.map((s) => [s.id, s.title || t("session.untitled")]));
    // 正在看的会话**真的被删掉**时，reload 不会自动清 activeSessionId（单条
    // 删除走 removeSession 才清）——这里主动回首页，避免停在已不存在的会话上。
    // 被引用跳过（skipped）的会话不算。
    const currentId = g.activeSessionId;
    setBulkBusy(true);
    setBulkError(null);
    try {
      const r = await api.bulkSessions(action, ids);
      const deletedActive =
        action === "delete" && !!currentId && (r.done ?? []).includes(currentId);
      if (action === "delete" && r.skipped?.length) {
        setBulkNotice(
          r.skipped.map((k) => ({
            id: k.id,
            title: titles.get(k.id) ?? k.id,
            references: k.references,
          })),
        );
      } else {
        setBulkNotice([]);
      }
      exitSelect();
      await g.reloadSessions();
      if (deletedActive) {
        g.setActiveSession(null);
        if (pathname !== "/") router.push("/");
      }
    } catch {
      setBulkError(t("bulk.failed"));
    } finally {
      setBulkBusy(false);
    }
  };
  const archiveAll = async () => {
    const ids = archiveCandidates.map((s) => s.id);
    setArchiveAllConfirm(false);
    if (!ids.length || bulkBusy) return;
    setBulkBusy(true);
    setBulkError(null);
    try {
      await api.bulkSessions("archive", ids);
      setBulkNotice([]);
      await g.reloadSessions();
    } catch {
      setBulkError(t("bulk.failed"));
    } finally {
      setBulkBusy(false);
    }
  };

  // 打开会话（侧栏行/归档命中共用）：必要时路由回工作区。
  const openSessionFromSidebar = (id: string) => {
    g.setActiveSession(id);
    if (!onWorkspace) router.push("/");
  };

  // workflow 运行行（侧栏运行伪条目）：事件语义在 SidebarRunRow 内保持与原先
  // 一致——取消/删除运行记录（正在查看时一并退出 run 视图）。
  const renderRunRow = (r: WorkflowRun, depth: number) => (
    <SidebarRunRow
      key={r.id}
      run={r}
      depth={depth}
      selected={onWorkspace && g.activeRunId === r.id}
      workflowName={g.workflows.find((w) => w.id === r.workflow_id)?.name}
      onOpen={() => {
        g.openRunView(r.id);
        if (!onWorkspace) router.push("/");
      }}
      onCancel={() => {
        void api
          .cancelWorkflowRun(r.id)
          .then(() => g.reloadWorkflowRuns())
          .catch(() => {
            /* 端点不可达——状态以 run.status 事件/轮询为准 */
          });
      }}
      onDelete={() => {
        void api
          .deleteWorkflowRun(r.id)
          .then(async () => {
            if (g.activeRunId === r.id) g.closeRunView();
            await g.reloadWorkflowRuns();
          })
          .catch(() => {
            /* ignore — reconcile on next reload */
          });
      }}
    />
  );

  const renderSessionRow = (
    s: SessionMeta,
    depth = 0,
    childRows?: SessionMeta[],
    dimmed = false,
  ) => (
    <SidebarSessionRow
      key={s.id}
      s={s}
      depth={depth}
      // 调用方不传时按父会话的直属子会话补全（归档区就是这么用的）。
      childRows={childRows ?? childrenOf.get(s.id) ?? []}
      dimmed={dimmed}
      agents={g.agents}
      activeSessionId={g.activeSessionId}
      onWorkspace={onWorkspace}
      selectMode={selectMode}
      selected={selected}
      unreadSessions={g.unreadSessions}
      treeCollapsed={g.treeCollapsed}
      editingId={editingId}
      editTitle={editTitle}
      cancelRename={cancelRename}
      subGroupFold={subGroupFold}
      childrenOf={childrenOf}
      runsOf={runsOf}
      descendantCount={descendantCount}
      onToggleSelect={toggleSelect}
      onSelectSession={openSessionFromSidebar}
      onStartRename={(row) => {
        setEditTitle(row.title || "");
        setEditingId(row.id);
      }}
      onEditTitleChange={setEditTitle}
      onFinishEditing={() => setEditingId(null)}
      onRename={(id, title) => void g.renameSession(id, title)}
      onPin={(id, pinned) => void g.pinSession(id, pinned)}
      onRemoveSession={(id) => void g.removeSession(id)}
      onSetTreeCollapsed={g.setTreeCollapsed}
      // 存的是「新的折叠态」：当前展开 → 收起（true）。
      onToggleSubGroupFold={(key, open) => setSubGroupFold((m) => ({ ...m, [key]: !open }))}
      onDelete={setDeleteTarget}
      renderRunRow={renderRunRow}
    />
  );

  // 归档检索结果的打开语义：标题命中 = 普通打开；消息命中 = 记下锚点再打开
  // （目标就是当前会话时直接广播滚动事件，TranscriptList 自己定位）。
  const openFromSearch = (sid: string, messageId?: string) => {
    if (messageId) {
      if (sid === g.activeSessionId) {
        window.dispatchEvent(new CustomEvent("ginno:scroll-to-message", { detail: messageId }));
      } else {
        rememberMessageTarget(messageId);
      }
    }
    g.setActiveSession(sid);
    if (!onWorkspace) router.push("/");
  };

  // Toggle the right panel with ⌘\ / Ctrl+\ (right-panel-redesign.md §3.3).
  // Workspace-only: on settings/kb/workflows routes there is no panel.
  // Stable refs destructured out of `g` so the listener isn't re-armed on
  // every provider render.
  const rightPanelOpen = g.rightPanelOpen;
  const setRightPanelOpen = g.setRightPanelOpen;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!onWorkspace) return;
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key === "\\") {
        e.preventDefault();
        setRightPanelOpen(!rightPanelOpen);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onWorkspace, rightPanelOpen, setRightPanelOpen]);

  // ⌘N → home (new session is created lazily on first send); ⌘K → session
  // search. Global on purpose: reachable from settings/kb/workflows too.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // The /pin window mounts AppShell only for its hooks — navigation
      // shortcuts must not hijack it (⌘↵/Esc belong to PinApp there).
      if (window.location.pathname === "/pin") return;
      if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if (k === "n") {
        e.preventDefault();
        setActiveSessionForNav(null);
        if (window.location.pathname !== "/") router.push("/");
      } else if (k === "k") {
        e.preventDefault();
        setSearchOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setActiveSessionForNav, router]);

  // Backspace 不触发页面后退（2026-10-11 用户反馈）。macOS WKWebView 的
  // 默认行为：非可编辑焦点处按 Backspace = history.back()，在 Tauri 壳里
  // 会把用户「弹」回上一个路由。输入框/文本域/contentEditable（含
  // CodeMirror 类编辑器）里的退格是删字符，必须放行；带修饰键的组合
  // （Cmd/Ctrl/Alt+Backspace）不是后退路径，也不拦。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Backspace" || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Floating quick-chat window (docs/floating-window-design.md §4 Phase 3):
  // the /pin route renders PinApp bare — no sidebar, workspace, or ChatStream.
  // Placed after every hook so the rules-of-hooks order is untouched; the
  // effects above are inert on /pin (workspace-gated or bridge registration
  // the pin window can also use).
  if (pathname === "/pin") return <>{children}</>;

  return (
    <div className="flex h-screen w-full overflow-hidden bg-base text-txt">
      {/* left nav */}
      <aside className="flex w-64 shrink-0 flex-col border-r border-line bg-panel">
        <SidebarHeader
          groupView={groupView}
          onGroupViewChange={setGroupView}
          archiveCandidatesCount={archiveCandidates.length}
          bulkBusy={bulkBusy}
          onArchiveAll={() => setArchiveAllConfirm(true)}
          selectMode={selectMode}
          onToggleSelectMode={() => {
            if (selectMode) {
              exitSelect();
            } else {
              setEditingId(null);
              setBulkNotice([]);
              setBulkError(null);
              setSelectMode(true);
            }
          }}
          onNewSession={onNewSession}
          onSearch={() => setSearchOpen(true)}
          agents={g.agents}
          agentFilter={agentFilter}
          onAgentFilterChange={setAgentFilter}
        >
          <SidebarSessionList
            pinnedSessions={pinnedSessions}
            sessionGroups={sessionGroups}
            projectGroups={projectGroups}
            groupView={groupView}
            groupLabel={groupLabel}
            childrenOf={childrenOf}
            renderSessionRow={renderSessionRow}
            ready={g.ready}
            sessionsCount={g.sessions.length}
            sortedCount={sortedSessions.length}
            sessionError={g.sessionError}
            onOpenSessionError={() => router.push("/settings/connections/model-api")}
          >
            {/* 已归档（P1-1）：默认收起，标题带计数；行压暗但仍可点开/恢复。
                两种视图下都固定挂在最底部。P0-2：展开后带本地检索框（标题 +
                消息片段），有搜索时用结果替代普通列表；无搜索时按 YYYY-MM
                分月、最新在前。 */}
            <SidebarArchive
              archivedSessions={archivedSessions}
              archivedMonthGroups={archivedMonthGroups}
              hiddenIds={hiddenIds}
              open={archivedOpen}
              onToggle={() => setArchivedOpen((v) => !v)}
              query={archivedQuery}
              onQueryChange={setArchivedQuery}
              onOpenFromSearch={openFromSearch}
              renderSessionRow={renderSessionRow}
            />
          </SidebarSessionList>
        </SidebarHeader>

        {/* 批量动作提示 + 多选操作条（P1-4） */}
        <SidebarBulkBar
          selectMode={selectMode}
          selectedCount={selected.size}
          selectedArchivedCount={selectedArchivedIds.length}
          bulkBusy={bulkBusy}
          bulkNotice={bulkNotice}
          bulkError={bulkError}
          onDismissNotice={() => setBulkNotice([])}
          onDismissError={() => setBulkError(null)}
          onExitSelect={exitSelect}
          onArchive={() => void runBulk("archive")}
          onUnarchive={() => void runBulk("unarchive")}
          onRequestDelete={() => setBulkDeleteConfirm(true)}
          onRestoreArchived={() => {
            const ids = selectedArchivedIds;
            setArchivedQuery("");
            void runBulk("unarchive", ids);
          }}
          onRequestArchivedDelete={() => setArchivedDeleteConfirm(true)}
        />

        <SidebarFooter
          onTeam={onTeam}
          onConnectors={onConnectors}
          onScheduled={onScheduled}
          onKb={onKb}
          onWorkflows={onWorkflows}
          onSettings={onSettings}
          connectorDot={connectorDot}
          scheduleDot={scheduleDot}
          scheduleUnseen={scheduleUnseen}
        />
      </aside>

      {/* main */}
      <main className="flex min-w-0 flex-1">
        {/* Workspace: always mounted so ChatStream refs (WS, store, error cards)
            survive navigating to /settings or /kb and back. Hidden off-route.
            打开 run 视图时同样用 hidden（不卸载）——会话流的 WS/草稿等 ref 状态
            在回看运行期间保持存活。 */}
        <div className={`flex min-w-0 flex-1 ${onWorkspace ? "" : "hidden"}`}>
          <div className={`flex min-w-0 flex-1 flex-col ${g.activeRunId || g.activeScheduleRunId ? "hidden" : ""}`}>
            {runtimeStale && (
              <div className="flex items-center gap-2 border-b border-red/40 bg-red/[0.04] px-4 py-2 text-xs">
                <span className="font-medium text-red">{t("runtimeStale.title")}</span>
                <span className="text-muted">{t("runtimeStale.hint")}</span>
                <button
                  onClick={() => setRuntimeStale(false)}
                  className="ml-auto shrink-0 rounded border border-line px-2 py-0.5 text-faint hover:text-txt"
                >
                  {t("runtimeStale.dismiss")}
                </button>
              </div>
            )}
            {session && (
              <TopBar session={session} agent={agent} running={running} usage={usage} />
            )}
            {/* 全局通知中心（notice-center-design.md）：零高度定位锚紧跟 TopBar，
                通知因此始终贴在顶栏正下方、不参与布局（不顶动内容）。absolute
                右对齐，而右侧栏是本列的兄弟节点 → 天然不会被通知覆盖。 */}
            <div className="relative z-30 h-0">
              <NoticeHost />
            </div>
            <ChatStream
              session={session}
              onRunningChange={setRunning}
              onUsageChange={setUsage}
              onOpenGoal={() => setGoalSessionModal(true)}
            />
          </div>
          {/* run 视图：workflow 运行的中央回看区（RunSubSessionView 自取数据） */}
          {g.activeRunId && onWorkspace && (
            <div className="flex min-w-0 flex-1 flex-col">
              <RunSubSessionView runId={g.activeRunId} />
            </div>
          )}
          {/* Right panel or its collapsed edge dock (right-panel-redesign.md) */}
          {g.rightPanelOpen ? <RightPanel /> : <RightDock />}
          <SheetViewer />
        </div>
        {/* Non-workspace routes (settings, kb, workflows, scheduled)。定时回放期间
            用 CSS hidden（不卸载）——/scheduled 的页签/过滤/时间条日期在「返回」后
            无缝还原（同工作区的 hidden 不卸载骨架，§3.6）。 */}
        {!onWorkspace && (
          <div className={`flex min-w-0 flex-1 ${g.activeScheduleRunId ? "hidden" : ""}`}>{children}</div>
        )}
        {/* 定时任务回放（scheduled-tasks-design §3.6）：与 run 视图互斥，挂载在
            main 顶层——/scheduled 里点执行记录也直接全屏回看，不离开当前路由；
            返回即还原（children/工作区都只是被 hidden，状态无损）。 */}
        {g.activeScheduleRunId && g.activeScheduleRun && (
          <div className="flex min-w-0 flex-1 flex-col">
            <ScheduleRunView run={g.activeScheduleRun} />
          </div>
        )}
      </main>

      {/* B 轨 fallback「提示一次」toast(connector #6) */}
      {fallbackToast && (
        <div className="fixed bottom-5 right-5 z-50 max-w-xs rounded-xl border border-violet-500/40 bg-violet-500/10 px-4 py-3 text-xs leading-relaxed text-txt shadow-lg">
          <div className="font-medium">{t("fallbackToast.title")}</div>
          <div className="mt-0.5 text-faint">{fallbackToast}</div>
        </div>
      )}

      {/* Claude Code Mods toast 宿主(claude-code-mods-design.md §7.2):消费
          engine 转发的 mod.toast 帧,右下角堆叠、8s 自动消失。 */}
      <ModToastHost />

      {goalSessionModal && (
        <GoalEditor
          initial=""
          title={t("goalSession.title")}
          onClose={() => setGoalSessionModal(false)}
          onSubmit={onGoalSession}
        />
      )}

      {searchOpen && (
        <SessionSearchModal
          onClose={() => setSearchOpen(false)}
          onOpen={(sid) => {
            g.setActiveSession(sid);
            if (pathname !== "/") router.push("/");
            window.dispatchEvent(new CustomEvent("ginno:focus-latest", { detail: sid }));
          }}
        />
      )}

      <SidebarConfirmModals
        archiveAllConfirm={archiveAllConfirm}
        archiveCandidateCount={archiveCandidates.length}
        onCancelArchiveAll={() => setArchiveAllConfirm(false)}
        onConfirmArchiveAll={() => void archiveAll()}
        bulkDeleteConfirm={bulkDeleteConfirm}
        selectedCount={selected.size}
        onCancelBulkDelete={() => setBulkDeleteConfirm(false)}
        onConfirmBulkDelete={() => {
          setBulkDeleteConfirm(false);
          void runBulk("delete");
        }}
        archivedDeleteConfirm={archivedDeleteConfirm}
        selectedArchivedCount={selectedArchivedIds.length}
        onCancelArchivedDelete={() => setArchivedDeleteConfirm(false)}
        onConfirmArchivedDelete={() => {
          const ids = selectedArchivedIds;
          setArchivedDeleteConfirm(false);
          setArchivedQuery("");
          void runBulk("delete", ids);
        }}
        deleteTarget={deleteTarget}
        descendantCount={descendantCount}
        onCancelDelete={() => setDeleteTarget(null)}
        onConfirmDelete={confirmDelete}
      />
    </div>
  );
}
