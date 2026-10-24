"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import {
  AlarmClock,
  Archive,
  BookOpen,
  Cable,
  Check,
  ChevronDown,
  Settings as SettingsIcon,
  Plus,
  Search,
  Workflow as WorkflowIcon,
  Pencil,
  Pin,
  PinOff,
  Square,
  Trash2,
  Users,
} from "lucide-react";
import { GoalEditor } from "@/components/shell/GoalChip";
import { useGinno, LAST_SESSION_KEY, notifyScheduleRunFinished } from "@/lib/store";
import { openSocket, wsConnectorsUrl, wsScheduleUrl } from "@/lib/runtime";
import { setKeepAwake } from "@/lib/desktop";
import * as api from "@/lib/runtime";
import { agentHex } from "@/lib/theme";
import { relTime } from "@/lib/utils";
import { hiddenSessionIds } from "@/lib/sessionVisibility";
import { AgentIcon } from "@/components/icons";
import { ConfirmModal } from "@/components/ConfirmModal";
import { applyTheme } from "@/components/settings/GeneralSettings";
import { TopBar } from "@/components/shell/TopBar";
import { SessionSearchModal } from "@/components/shell/SessionSearchModal";
import { ChatStream } from "@/components/chat/ChatStream";
import {
  SUBAGENT_BUCKET_ORDER,
  SUBAGENT_STATUS_META,
  DuckStatusIcon,
  SubagentKindBadges,
  subagentSessionBucket,
  useStatusLabel,
} from "@/components/chat/blocks";
import { RunSubSessionView } from "@/components/chat/RunSubSessionView";
import { ScheduleRunView } from "@/components/chat/ScheduleRunView";
import { UnseenBadge } from "@/components/scheduled/UnseenBadge";
import { RUN_STATUS_META, useRunStatusLabel } from "@/components/chat/RunBlocks";
import { SheetViewer } from "@/components/chat/SheetViewer";
import { ModToastHost } from "@/components/chat/mod/ModToastHost";
import { NoticeHost } from "@/components/notice/NoticeHost";
import { RightPanel } from "@/components/right/RightPanel";
import { RightDock } from "@/components/right/RightDock";
import type { FolderEntry, ScheduleRun, SessionMeta, SessionUsage, WorkflowRun } from "@/lib/types";

// P1-1 自动归档：归档标记只有在该会话「新鲜度」过窗后才真正生效——7 天内有
// 新活动（updated 被 turn 顶上去了）的会话即使服务端 archived 还是 true，也
// 照常出现在正常分组里，等服务端回写 false。
const SIDEBAR_ACTIVE_WINDOW = 7 * 86400;
// 「一键归档」只碰超过 14 天没动静、且还没归档的非置顶会话。
const ARCHIVE_ALL_AFTER = 14 * 86400;
// 侧栏偏好（均 localStorage 持久化；读写都吞异常，隐私模式/无 storage 时退回默认）。
const ARCHIVED_OPEN_KEY = "ginno-sidebar-archived-open";
const GROUP_VIEW_KEY = "ginno-sidebar-group-view";

type TimeGroup = "Today" | "Yesterday" | "Last7" | "Earlier";

export function AppShell({ children }: { children: React.ReactNode }) {
  const g = useGinno();
  // shell 域文案（侧栏项目列表/导航按钮/窗口控件提示/菜单/空态引导）
  const t = useTranslations("shell");
  // 状态枚举的 key 渲染（chat.status.* 命中即译）：侧栏 workflow 运行行与
  // 子会话行的状态标签不再直读 meta 表的英文 label，经 hook 翻译后拼进
  // 既有 statusPrefix title 模板（meta 表本身保留英文导出做回退）。
  const runStatusLabel = useRunStatusLabel();
  const subStatusLabel = useStatusLabel();
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
  const [bulkNotice, setBulkNotice] = useState<
    Array<{ id: string; title: string; references?: string[] }>
  >([]);
  const [bulkError, setBulkError] = useState<string | null>(null);
  const [archiveAllConfirm, setArchiveAllConfirm] = useState(false);
  const [bulkDeleteConfirm, setBulkDeleteConfirm] = useState(false);
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

  // Sidebar sessions: activity-day groups, newest activity first. `updated`
  // is bumped per turn server-side, so it tracks last use, not creation.
  // C+ 方案③：agent 筛选作用于分组前的列表，分组/排序逻辑不变。
  //
  // subagent 树（subagent-design.md §6.1）：先按活动日分主行，再把子会话
  // （type==="subagent"）按 parent_session_id 挂到父行下嵌套渲染。子会话不进
  // 天分组——它的活动不应顶起父会话的排序（设计文档开放问题 2 的 UI 侧答案）。
  // 影子/团队空间会话与其整条子树一并隐藏（sessionVisibility）：定时任务拉起
  // 的 subagent 父行是隐藏的影子会话，若走下面孤儿回退会漏进主侧栏。
  const hiddenIds = hiddenSessionIds(g.sessions);
  const visibleSessions = g.sessions.filter(
    (s) =>
      !hiddenIds.has(s.id) &&
      (!agentFilter || s.agent_id === agentFilter),
  );
  // 父 id → 直属子会话（按创建时间倒序——最新的在最上，与主列表
  // updated 倒序的阅读方向一致；2026-10-12 用户定稿）。状态分节在渲染
  // 层保序，即每节内也是新→旧。
  const childrenOf = new Map<string, SessionMeta[]>();
  for (const s of visibleSessions) {
    if ((s.type !== "subagent" && s.type !== "delegation") || !s.parent_session_id) continue;
    const list = childrenOf.get(s.parent_session_id) ?? [];
    list.push(s);
    childrenOf.set(s.parent_session_id, list);
  }
  for (const list of childrenOf.values()) {
    list.sort((a, b) => (b.created ?? 0) - (a.created ?? 0));
  }
  // workflow 运行伪条目：把 present_in_session_id 指向可见父会话的运行挂在
  // 父行下（孤儿运行/被筛选掉的父会话不渲染），按开始时间正序，与子会话行
  // 的排列方式一致。
  const runsOf = new Map<string, WorkflowRun[]>();
  for (const r of g.workflowRuns) {
    if (!r.present_in_session_id) continue;
    if (!visibleSessions.some((p) => p.id === r.present_in_session_id)) continue;
    const list = runsOf.get(r.present_in_session_id) ?? [];
    list.push(r);
    runsOf.set(r.present_in_session_id, list);
  }
  for (const list of runsOf.values()) {
    list.sort((a, b) => a.started - b.started);
  }
  // 全部后代数（嵌套子代理的 +N 尾标与级联删除确认文案共用）。
  const descendantCount = (id: string): number => {
    let n = 0;
    const walk = (pid: string) => {
      for (const c of childrenOf.get(pid) ?? []) {
        n++;
        walk(c.id);
      }
    };
    walk(id);
    return n;
  };
  // 父在本列表可见（未被筛选掉/未删除）的子会话才嵌套；孤儿子会话退回
  // 天分组顶层渲染，避免凭空消失。
  const hasVisibleParent = (s: SessionMeta) =>
    (s.type === "subagent" || s.type === "delegation") &&
    !!s.parent_session_id &&
    visibleSessions.some((p) => p.id === s.parent_session_id);
  const sortedSessions = [...visibleSessions]
    .filter((s) => !hasVisibleParent(s))
    .sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned) || (b.updated ?? 0) - (a.updated ?? 0));
  // 显式置顶的会话独立成组，排在天分组之上，不受活动日分组影响。
  const pinnedSessions = sortedSessions.filter((s) => s.pinned);
  const unpinnedSessions = sortedSessions.filter((s) => !s.pinned);
  // P1-1 自动归档：archived=true 且超过 7 天没活动才算归档；新 turn 会把
  // updated 顶上来，于是它立刻回到正常分组（不必等服务端翻标记）。
  const nowSec = Math.floor(Date.now() / 1000);
  const isArchived = (s: SessionMeta) =>
    s.archived === true && nowSec - (s.updated ?? 0) > SIDEBAR_ACTIVE_WINDOW;
  const activeUnpinned = unpinnedSessions.filter((s) => !isArchived(s));
  const archivedSessions = unpinnedSessions.filter(isArchived);
  const dayStart = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const todayMs = dayStart(new Date());
  const groupOf = (s: SessionMeta): TimeGroup => {
    const day = dayStart(new Date((s.updated ?? s.created) * 1000));
    if (day >= todayMs) return "Today";
    if (day >= todayMs - 86400000) return "Yesterday";
    // 2–7 天：仍算「最近」，8 天以上落到 Earlier（再交给归档判断）。
    if (day >= todayMs - SIDEBAR_ACTIVE_WINDOW * 1000) return "Last7";
    return "Earlier";
  };
  const sessionGroups: Array<[TimeGroup, SessionMeta[]]> = [
    ["Today", activeUnpinned.filter((s) => groupOf(s) === "Today")],
    ["Yesterday", activeUnpinned.filter((s) => groupOf(s) === "Yesterday")],
    ["Last7", activeUnpinned.filter((s) => groupOf(s) === "Last7")],
    ["Earlier", activeUnpinned.filter((s) => groupOf(s) === "Earlier")],
  ];
  // 天分组标题的本地化展示名（内部 key 保持英文，分组/比较逻辑不受影响）。
  const groupLabel: Record<TimeGroup, string> = {
    Today: t("groups.today"),
    Yesterday: t("groups.yesterday"),
    Last7: t("groups.last7"),
    Earlier: t("groups.earlier"),
  };

  // P1-2 项目视图：按 primary_folder 对应的文件夹名分组（名字来自 listFolders
  // 缓存）；没挂载或库里已不存在的 id 归「未分组」。activeUnpinned 已按活动
  // 倒序，Map 的插入顺序因此就是「最近有活动的项目在前」，未分组永远垫底。
  const ungroupedLabel = t("groups.ungrouped");
  const folderNames = new Map((folders ?? []).map((f) => [f.id, f.name || f.path || f.id]));
  const projectGroups: Array<[string, SessionMeta[]]> = (() => {
    if (groupView !== "project") return [];
    const byName = new Map<string, SessionMeta[]>();
    for (const s of activeUnpinned) {
      const name = s.primary_folder ? folderNames.get(s.primary_folder) : undefined;
      const key = name || ungroupedLabel;
      const list = byName.get(key) ?? [];
      list.push(s);
      byName.set(key, list);
    }
    const entries = [...byName.entries()];
    const ungroupedAt = entries.findIndex(([name]) => name === ungroupedLabel);
    if (ungroupedAt >= 0) entries.push(...entries.splice(ungroupedAt, 1));
    return entries;
  })();

  // P1-1 一键归档的候选：Earlier 里超过 14 天无活动、还没归档的非置顶会话。
  const archiveCandidates = activeUnpinned.filter(
    (s) => s.archived !== true && nowSec - (s.updated ?? 0) > ARCHIVE_ALL_AFTER,
  );

  // P1-4 批量动作：归档/取消归档/删除共用。删除被引用时后端会跳过并回报
  // skipped —— 这里把标题和引用落成侧栏提示，其余情况清空旧提示。
  const runBulk = async (action: "archive" | "unarchive" | "delete") => {
    const ids = [...selected];
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

  // workflow 运行行（侧栏运行伪条目）：glyph/标签来自 RUN_STATUS_META，标题
  // 优先 run.name、缺省退回 workflow 定义名；点击进入中央 run 视图（与选中
  // 会话行同款高亮）。hover 入口对齐子会话行：运行中/暂停 → 取消，结束态 →
  // 删除运行记录（正在查看时一并退出 run 视图）。
  const renderRunRow = (r: WorkflowRun, depth: number) => {
    const sel = onWorkspace && g.activeRunId === r.id;
    const meta = RUN_STATUS_META[r.status];
    const active = r.status === "running" || r.status === "paused";
    const wfName = g.workflows.find((w) => w.id === r.workflow_id)?.name;
    return (
      <div
        key={r.id}
        className={`nav-item group ${sel ? "text-txt" : ""} ${active ? "" : "opacity-60"}`}
        style={{
          ...(sel ? { background: "rgba(99,102,241,0.14)" } : undefined),
          ...(depth > 0 ? { paddingLeft: `${10 + depth * 16}px` } : undefined),
        }}
      >
        <button
          onClick={() => {
            g.openRunView(r.id);
            if (!onWorkspace) router.push("/");
          }}
          className="flex min-w-0 flex-1 items-center gap-2.5 text-left"
        >
          {meta ? (
            <span
              className="shrink-0 text-[11px] leading-none"
              title={t("session.statusPrefix", { status: runStatusLabel(r.status) })}
            >
              {meta.emoji}
            </span>
          ) : (
            <WorkflowIcon className="h-4 w-4 shrink-0 text-muted" />
          )}
          <span className="truncate">{r.name || wfName || t("run.fallbackName")}</span>
          <span className="ml-auto shrink-0 text-[10px] text-faint">{relTime(r.started)}</span>
        </button>
        <span className="flex shrink-0 items-center gap-0.5">
          {active ? (
            <button
              onClick={(e) => {
                e.stopPropagation();
                void api
                  .cancelWorkflowRun(r.id)
                  .then(() => g.reloadWorkflowRuns())
                  .catch(() => {
                    /* 端点不可达——状态以 run.status 事件/轮询为准 */
                  });
              }}
              aria-label={t("run.cancel")}
              title={t("run.cancelHint")}
              className="rounded p-1 text-muted opacity-0 transition-opacity hover:bg-card2 hover:text-yellow group-hover:opacity-100"
            >
              <Square className="h-3 w-3" />
            </button>
          ) : (
            <button
              onClick={(e) => {
                e.stopPropagation();
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
              aria-label={t("run.delete")}
              title={t("run.deleteHint")}
              className="rounded p-1 text-muted opacity-0 transition-opacity hover:bg-card2 hover:text-red group-hover:opacity-100"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          )}
        </span>
      </div>
    );
  };

  // dimmed：归档区的行整体压暗（仍可点击打开/恢复）。只在归档区顶层行传，
  // 子会话行沿用各自的状态压暗规则。
  const renderSessionRow = (
    s: SessionMeta,
    depth = 0,
    childRows: SessionMeta[] = [],
    dimmed = false,
  ) => {
    const sel = onWorkspace && s.id === g.activeSessionId;
    const checked = selectMode && selected.has(s.id);
    const rowAgent = g.agents.find((a) => a.id === s.agent_id) ?? null;
    const hex = agentHex(rowAgent?.color);
    const editing = editingId === s.id;
    const isSub = s.type === "subagent";
    const subStatus = s.subagent?.status;
    const subMeta = subStatus ? SUBAGENT_STATUS_META[subStatus] : null;
    const subActive = subStatus === "running" || subStatus === "waiting";
    const runRows = runsOf.get(s.id) ?? [];
    const hasKids = childRows.length > 0 || runRows.length > 0;
    const activeKids =
      childRows.filter(
        (c) => c.subagent?.status === "running" || c.subagent?.status === "waiting",
      ).length +
      runRows.filter((r) => r.status === "running" || r.status === "paused").length;
    // 默认展开有子会话的子树——已完成的子会话继续留在列表里可供回看
    // （用户反馈 2026-10-01：全部跑完后子树自动折叠、子会话像消失了一样）；
    // 折叠只在用户点过 chevron 后生效（覆盖持久化在 store）。
    const expanded = g.treeCollapsed[s.id] === undefined ? hasKids : !g.treeCollapsed[s.id];
    const openChild = () => {
      // 多选模式下行点击 = 勾选；打开会话要退出多选后再点。
      if (selectMode) {
        toggleSelect(s.id);
        return;
      }
      g.setActiveSession(s.id);
      if (!onWorkspace) router.push("/");
    };
    return (
      <div key={s.id}>
        <div
          className={`nav-item group ${sel ? "text-txt" : ""} ${
            isSub && !subActive && !sel ? "opacity-60" : ""
          } ${dimmed ? "opacity-60" : ""}`}
          style={{
            ...(sel ? { background: "rgba(99,102,241,0.14)" } : undefined),
            ...(depth > 0 ? { paddingLeft: `${10 + depth * 16}px` } : undefined),
          }}
        >
          {editing ? (
            <input
              autoFocus
              value={editTitle}
              onChange={(e) => setEditTitle(e.target.value)}
              onBlur={() => {
                if (cancelRename.current) {
                  cancelRename.current = false;
                  setEditingId(null);
                  return;
                }
                g.renameSession(s.id, editTitle);
                setEditingId(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  g.renameSession(s.id, editTitle);
                  setEditingId(null);
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  cancelRename.current = true; // suppress the onBlur commit
                  setEditingId(null);
                }
              }}
              onClick={(e) => e.stopPropagation()}
              className="min-w-0 flex-1 rounded border border-line2 bg-base/60 px-1 text-sm text-txt outline-none focus:border-violet"
            />
          ) : (
            <>
              <button
                onClick={openChild}
                onDoubleClick={(e) => {
                  if (selectMode) return; // 多选模式下双击不进入重命名
                  e.stopPropagation();
                  setEditTitle(s.title || "");
                  setEditingId(s.id);
                }}
                className="flex min-w-0 flex-1 items-center gap-2.5 text-left"
              >
                {/* 多选勾选框（P1-4）：纯指示器，点击事件由整行承接。 */}
                {selectMode && (
                  <span
                    aria-hidden
                    className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors ${
                      checked ? "border-violet bg-violet text-white" : "border-line2 text-transparent"
                    }`}
                  >
                    <Check className="h-3 w-3" />
                  </span>
                )}
                {/* 会话行归属：行首图标即 agent 图标（按 agent 色着色），取代原先的
                    agent 名文字标签 —— 归属一眼可见、不占标题宽度。子会话行例外：
                    persona 是继承父会话的（永远是 Dev Agent），标它没有信息量——
                    子会话行仍用状态 emoji + SubagentKindBadges 表达身份。 */}
                {isSub && subMeta ? (
                  // 子会话行：状态 emoji 取代会话图标（subagent-design.md §6.1）
                  <span
                    className="shrink-0 text-[11px] leading-none"
                    title={t("session.statusPrefix", { status: subStatusLabel(subStatus) })}
                  >
                    {subMeta.glyph}
                  </span>
                ) : (
                  // 归属由行首图标表达：agent 配置了 icon 就显示它（按 agent 色
                  // 着色），未配置（或 agent 已删除）统一回退到品牌鸭。
                  <span className="shrink-0" style={{ color: hex }} title={rowAgent?.name}>
                    <AgentIcon name={rowAgent?.icon} className="h-4 w-4" />
                  </span>
                )}
                <span className="truncate">{s.title || t("session.untitled")}</span>
                {/* fork / 子代理类型徽标（P3 范围 3）：仅子会话行渲染 */}
                {isSub && <SubagentKindBadges sub={s.subagent} />}
                {/* 悬浮速聊窗创建的 quick 会话角标（floating-window-design.md §1.1） */}
                {s.type === "quick" && (
                  <span title={t("session.quickBadge")} className="shrink-0 text-[10px] text-yellow">
                    ⚡
                  </span>
                )}
                {/* 子树还有更深的后代：行尾 +N 尾标（设计 §6.1） */}
                {hasKids && descendantCount(s.id) > childRows.length && (
                  <span
                    className="shrink-0 text-[10px] text-faint"
                    title={t("session.moreNested", { count: descendantCount(s.id) - childRows.length })}
                  >
                    +{descendantCount(s.id) - childRows.length}
                  </span>
                )}
                {/* 未读小圆点：turn 在别处跑完、还没回来读过 → 标题右侧紫点，
                    打开该会话即清除（会话行选中态本身意味着已读）。内存乐观层
                    或服务端装饰（s.unread，session_unread 单一事实源）任一命中。 */}
                <span className="ml-auto flex shrink-0 items-center gap-1.5">
                  {(g.unreadSessions[s.id] !== undefined || s.unread) && !sel && (
                    <span
                      className="h-1.5 w-1.5 rounded-full bg-violet"
                      title={t("session.unreadHint")}
                    />
                  )}
                  <span className="text-[10px] text-faint">
                    {relTime(s.updated ?? s.created)}
                  </span>
                </span>
              </button>
              {/* 父行徽标：运行中子代理/运行数量（设计 §6.1 的「● 2 agents」） */}
              {hasKids && activeKids > 0 && (
                <span
                  className="flex shrink-0 items-center gap-1 rounded-full border border-line2 px-1.5 text-[10px] leading-4 text-muted"
                  title={t("session.activeKids", { count: activeKids })}
                >
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-green" />
                  {activeKids}
                </span>
              )}
              {/* 行内动作（展开/置顶/改名/删除）在多选模式下整体隐去，避免
                  误触单条删除；子会话的停止/清除同理。 */}
              <span className={`flex shrink-0 items-center gap-0.5 ${selectMode ? "hidden" : ""}`}>
                {hasKids && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      // 存的是「新的折叠态」：当前展开 → 收起（true）。
                      g.setTreeCollapsed(s.id, expanded);
                    }}
                    aria-label={expanded ? t("session.collapse") : t("session.expand")}
                    title={expanded ? t("session.collapse") : t("session.expand")}
                    className="rounded p-1 text-muted hover:bg-card2 hover:text-txt"
                  >
                    <ChevronDown
                      className={`h-3.5 w-3.5 transition-transform ${expanded ? "" : "-rotate-90"}`}
                    />
                  </button>
                )}
                {isSub || s.type === "delegation" ? (
                  // 子会话行 hover 入口（P2 共享契约 4）：运行中/等待 → 停止
                  // （HTTP 端点，running 走协作式停止、waiting 级联停后代；
                  // 状态刷新依赖既有 subagent.status 事件）；结束态 → 清除行
                  // （删除该子会话）。delegation 回放同属子会话家族——不进
                  // 父会话的 pin/重命名/删除组（pin 对子会话是死按钮，
                  // 2026-10-12 用户定稿子会话一律无 pin）。
                  subActive ? (
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        void api.stopSession(s.id).catch(() => {
                          /* 端点不可达——状态仍以 subagent.status 事件为准 */
                        });
                      }}
                      aria-label={t("session.stop")}
                      title={t("session.stopHint")}
                      className="rounded p-1 text-muted opacity-0 transition-opacity hover:bg-card2 hover:text-yellow group-hover:opacity-100"
                    >
                      <Square className="h-3 w-3" />
                    </button>
                  ) : (
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        void g.removeSession(s.id);
                      }}
                      aria-label={t("session.clearSub")}
                      title={t("session.clearSubHint")}
                      className="rounded p-1 text-muted opacity-0 transition-opacity hover:bg-card2 hover:text-red group-hover:opacity-100"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  )
                ) : (
                  <>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        void g.pinSession(s.id, !s.pinned);
                      }}
                      aria-label={s.pinned ? t("session.unpin") : t("session.pin")}
                      title={s.pinned ? t("session.unpinHint") : t("session.pinHint")}
                      className={`rounded p-1 hover:bg-card2 ${
                        s.pinned ? "text-violet" : "text-muted hover:text-txt"
                      }`}
                    >
                      {s.pinned ? <PinOff className="h-3.5 w-3.5" /> : <Pin className="h-3.5 w-3.5" />}
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        setEditTitle(s.title || "");
                        setEditingId(s.id);
                      }}
                      aria-label={t("session.rename")}
                      title={t("session.renameHint")}
                      className="rounded p-1 text-muted hover:bg-card2 hover:text-txt"
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        setDeleteTarget(s);
                      }}
                      aria-label={t("session.delete")}
                      title={t("session.delete")}
                      className="rounded p-1 text-muted hover:bg-card2 hover:text-red"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </>
                )}
              </span>
            </>
          )}
        </div>
        {hasKids && expanded && (
          <div className="space-y-0.5">
            {/* 子会话三档分节收纳（2026-10-12）：运行中 → 失败 → 成功，空节不
                渲染；运行中/失败默认展开，成功默认收起（节头计数保证收起仍可
                见——不是 2026-10-01 那种「跑完即消失」）。单子会话不分节。
                孙代递归不受影响：节内行仍走 renderSessionRow(depth+1)。 */}
            {childRows.length > 1
              ? SUBAGENT_BUCKET_ORDER.map((bk) => {
                  const group = childRows.filter((c) => subagentSessionBucket(c) === bk);
                  if (!group.length) return null;
                  const key = `${s.id}:${bk}`;
                  const open = subGroupFold[key] ?? bk !== "done";
                  return (
                    <div key={bk} style={depth > 0 ? { marginLeft: `${depth * 16}px` } : undefined}>
                      <button
                        onClick={() => setSubGroupFold((m) => ({ ...m, [key]: !open }))}
                        title={t("session.statusPrefix", { status: subStatusLabel(bk) })}
                        className="flex w-full items-center gap-1.5 rounded px-1 py-0.5 text-[10px] text-faint transition-colors hover:bg-card2/40 hover:text-muted"
                      >
                        <ChevronDown
                          className={`h-3 w-3 shrink-0 transition-transform ${open ? "" : "-rotate-90"}`}
                        />
                        <DuckStatusIcon status={bk} className="shrink-0" />
                        <span>{subStatusLabel(bk)}</span>
                        <span>· {group.length}</span>
                      </button>
                      {open &&
                        group.map((c) => renderSessionRow(c, depth + 1, childrenOf.get(c.id) ?? []))}
                    </div>
                  );
                })
              : childRows.map((c) => renderSessionRow(c, depth + 1, childrenOf.get(c.id) ?? []))}
            {runRows.map((r) => renderRunRow(r, depth + 1))}
          </div>
        )}
      </div>
    );
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
        {/* brand */}
        <div className="flex items-center gap-2.5 px-4 pb-2 pt-4">
          <img src="/icon.png" alt="" className="h-7 w-7" />
          <span className="text-[15px] font-semibold tracking-tight">GinnoWork</span>
        </div>

        {/* 侧栏工具条（P1-1/P1-2/P1-4）：时间/项目视图切换、一键归档、多选入口 */}
        <div className="flex items-center gap-1 px-3 pb-2">
          <div className="flex items-center gap-0.5 rounded-md border border-line p-0.5">
            {(["time", "project"] as const).map((v) => (
              <button
                key={v}
                onClick={() => setGroupView(v)}
                className={`rounded px-1.5 py-0.5 text-[11px] transition-colors ${
                  groupView === v
                    ? "bg-card2 text-txt"
                    : "text-faint hover:text-txt"
                }`}
              >
                {v === "time" ? t("view.time") : t("view.project")}
              </button>
            ))}
          </div>
          <span className="ml-auto flex items-center gap-1">
            <button
              onClick={() => setArchiveAllConfirm(true)}
              disabled={!archiveCandidates.length || bulkBusy}
              aria-label={t("bulk.archiveAll")}
              title={
                archiveCandidates.length
                  ? t("bulk.archiveAllHint", { count: archiveCandidates.length })
                  : t("bulk.archiveAllEmpty")
              }
              className="rounded p-1 text-muted transition-colors hover:bg-card2 hover:text-txt disabled:cursor-default disabled:opacity-40 disabled:hover:bg-transparent"
            >
              <Archive className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={() => {
                if (selectMode) {
                  exitSelect();
                } else {
                  setEditingId(null);
                  setBulkNotice([]);
                  setBulkError(null);
                  setSelectMode(true);
                }
              }}
              title={selectMode ? t("bulk.cancel") : t("bulk.selectHint")}
              className={`rounded px-1.5 py-0.5 text-[11px] transition-colors ${
                selectMode
                  ? "bg-card2 text-violet"
                  : "text-muted hover:bg-card2 hover:text-txt"
              }`}
            >
              {selectMode ? t("bulk.cancel") : t("bulk.select")}
            </button>
          </span>
        </div>

        <div className="flex-1 overflow-y-auto px-2.5 pb-2">
          {/* primary actions first (open-experience prototype) */}
          <div className="mb-1 space-y-0.5 border-b border-line pb-2.5">
            <button onClick={onNewSession} className="nav-item" title={t("nav.newSessionHint")}>
              <Plus className="h-4 w-4 shrink-0" />
              <span className="truncate">{t("nav.newSession")}</span>
              <kbd className="ml-auto shrink-0 rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-faint">⌘N</kbd>
            </button>
            <button onClick={() => setSearchOpen(true)} className="nav-item">
              <Search className="h-4 w-4 shrink-0" />
              <span className="truncate">{t("nav.searchSessions")}</span>
              <kbd className="ml-auto shrink-0 rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-faint">⌘K</kbd>
            </button>
          </div>

          {/* C+ 方案③：agent 筛选 chips（样式同 composer chip 行，点击已选中的取消） */}
          <div className="flex flex-wrap gap-1 px-1 pb-2 pt-1.5">
            {g.agents.map((a) => {
              const sel = agentFilter === a.id;
              const hex = agentHex(a.color);
              return (
                <button
                  key={a.id}
                  onClick={() => setAgentFilter(sel ? null : a.id)}
                  title={sel ? t("filter.clear") : t("filter.showOnly", { name: a.name })}
                  className={`flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] transition-colors ${
                    sel ? "" : "border-line bg-card text-muted hover:border-line2 hover:text-txt"
                  }`}
                  style={sel ? { borderColor: hex, background: hex + "1a", color: hex } : undefined}
                >
                  <span className="h-1.5 w-1.5 rounded-full" style={{ background: hex }} />
                  {a.name}
                </button>
              );
            })}
          </div>

          {/* sessions grouped by activity day（置顶组在最上） */}
          {pinnedSessions.length > 0 && (
            <div className="mb-1">
              <div className="px-2.5 pb-1 pt-3 text-[11px] font-medium text-faint">
                {t("groups.pinned")}
              </div>
              <div className="space-y-0.5">
                {pinnedSessions.map((s) => renderSessionRow(s, 0, childrenOf.get(s.id) ?? []))}
              </div>
            </div>
          )}
          {groupView === "time"
            ? sessionGroups.map(([label, rows]) =>
                rows.length ? (
                  <div key={label} className="mb-1">
                    <div className="px-2.5 pb-1 pt-3 text-[11px] font-medium text-faint">
                      {groupLabel[label]}
                    </div>
                    <div className="space-y-0.5">
                      {rows.map((s) => renderSessionRow(s, 0, childrenOf.get(s.id) ?? []))}
                    </div>
                  </div>
                ) : null,
              )
            : projectGroups.map(([name, rows]) => (
                <div key={name} className="mb-1">
                  <div className="px-2.5 pb-1 pt-3 text-[11px] font-medium text-faint">{name}</div>
                  <div className="space-y-0.5">
                    {rows.map((s) => renderSessionRow(s, 0, childrenOf.get(s.id) ?? []))}
                  </div>
                </div>
              ))}

          {/* 已归档（P1-1）：默认收起，标题带计数；行压暗但仍可点开/恢复。
              两种视图下都固定挂在最底部。 */}
          {archivedSessions.length > 0 && (
            <div className="mb-1">
              <button
                onClick={() => setArchivedOpen((v) => !v)}
                title={t("groups.archivedHint")}
                className="flex w-full items-center gap-1 px-2.5 pb-1 pt-3 text-left text-[11px] font-medium text-faint transition-colors hover:text-muted"
              >
                <ChevronDown
                  className={`h-3 w-3 shrink-0 transition-transform ${
                    archivedOpen ? "" : "-rotate-90"
                  }`}
                />
                {t("groups.archived", { count: archivedSessions.length })}
              </button>
              {archivedOpen && (
                <div className="space-y-0.5">
                  {archivedSessions.map((s) =>
                    renderSessionRow(s, 0, childrenOf.get(s.id) ?? [], true),
                  )}
                </div>
              )}
            </div>
          )}
          {/* 首屏骨架：ready 之前 sessions 还是空数组，直接渲染空态会在每次冷启动/
              刷新时闪一下「还没有会话」（SSR 也含这句文案）。 */}
          {!g.ready && (
            <div className="space-y-2 px-2.5 py-3" aria-hidden>
              {[0, 1, 2].map((i) => (
                <div key={i} className="flex items-center gap-2">
                  <span className="h-3.5 w-3.5 shrink-0 animate-pulse rounded bg-line2/70" />
                  <span className="h-3 flex-1 animate-pulse rounded bg-line2/50" />
                </div>
              ))}
            </div>
          )}
          {g.ready && g.sessions.length === 0 && (
            <div className="px-2.5 py-2 text-xs leading-relaxed text-faint">
              {t("empty.noSessions")}
              <br />
              {t("empty.noSessionsHint")}
            </div>
          )}
          {/* C+ 方案③：有会话但当前筛选下为空——独立兜底文案，避免误以为没有会话 */}
          {g.ready && g.sessions.length > 0 && sortedSessions.length === 0 && (
            <div className="px-2.5 py-2 text-xs leading-relaxed text-faint">
              {t("empty.filtered")}
              <br />
              {t("empty.filteredHint")}
            </div>
          )}

          {g.sessionError && (
            <button
              onClick={() => router.push("/settings/connections/model-api")}
              title={t("session.errorHint")}
              className="mx-1 mb-3 block rounded-md border border-yellow/40 bg-yellow/10 px-2 py-1.5 text-left text-[11px] leading-snug text-yellow hover:bg-yellow/15"
            >
              {g.sessionError}
            </button>
          )}
        </div>

        {/* 批量删除被引用拒绝的会话（P1-4）：后端按引用跳过，这里列出标题 +
            前 2 个引用来源，便于用户先去解除引用。 */}
        {bulkNotice.length > 0 && (
          <div className="mx-2 mb-2 rounded-md border border-yellow/40 bg-yellow/10 px-2 py-1.5 text-[11px] leading-snug text-yellow">
            <div className="flex items-start gap-1">
              <span className="flex-1">{t("bulk.skipped", { count: bulkNotice.length })}</span>
              <button
                onClick={() => setBulkNotice([])}
                aria-label={t("bulk.dismiss")}
                title={t("bulk.dismiss")}
                className="shrink-0 text-yellow/70 hover:text-yellow"
              >
                ✕
              </button>
            </div>
            <ul className="mt-1 space-y-0.5">
              {bulkNotice.map((it) => (
                <li
                  key={it.id}
                  className="truncate"
                  title={`${it.id}${it.references?.length ? ` · ${it.references.join(", ")}` : ""}`}
                >
                  {it.title}
                  {it.references?.length
                    ? ` · ${it.references.slice(0, 2).join(", ")}`
                    : ""}
                </li>
              ))}
            </ul>
          </div>
        )}
        {bulkError && (
          <div className="mx-2 mb-2 flex items-start gap-1 rounded-md border border-red/40 bg-red/10 px-2 py-1.5 text-[11px] text-red">
            <span className="flex-1">{bulkError}</span>
            <button
              onClick={() => setBulkError(null)}
              aria-label={t("bulk.dismiss")}
              className="shrink-0 text-red/70 hover:text-red"
            >
              ✕
            </button>
          </div>
        )}

        {/* 多选操作条（P1-4）：贴侧栏底部，计数 + 归档/取消归档/删除/取消 */}
        {selectMode && (
          <div className="border-t border-line bg-panel px-2.5 py-2">
            <div className="mb-1.5 flex items-center justify-between text-[11px]">
              <span className="text-muted">{t("bulk.selected", { count: selected.size })}</span>
              <button onClick={exitSelect} className="text-faint hover:text-txt">
                {t("bulk.cancel")}
              </button>
            </div>
            <div className="flex items-center gap-1">
              <button
                onClick={() => void runBulk("archive")}
                disabled={!selected.size || bulkBusy}
                className="flex-1 rounded-md border border-line2 px-2 py-1 text-[11px] text-muted transition-colors hover:text-txt disabled:cursor-default disabled:opacity-40"
              >
                {t("bulk.archive")}
              </button>
              <button
                onClick={() => void runBulk("unarchive")}
                disabled={!selected.size || bulkBusy}
                className="flex-1 rounded-md border border-line2 px-2 py-1 text-[11px] text-muted transition-colors hover:text-txt disabled:cursor-default disabled:opacity-40"
              >
                {t("bulk.unarchive")}
              </button>
              <button
                onClick={() => setBulkDeleteConfirm(true)}
                disabled={!selected.size || bulkBusy}
                className="flex-1 rounded-md border border-red/40 px-2 py-1 text-[11px] text-red transition-colors hover:bg-red/10 disabled:cursor-default disabled:opacity-40"
              >
                {t("bulk.delete")}
              </button>
            </div>
          </div>
        )}

        {/* footer nav */}
        <div className="border-t border-line px-2.5 py-3">
          <Link href="/team" className={`nav-item ${onTeam ? "nav-item-active" : ""}`}>
            <Users className="h-4 w-4 shrink-0" />
            <span className="truncate">{t("nav.team")}</span>
          </Link>
          <Link href="/connectors" className={`nav-item ${onConnectors ? "nav-item-active" : ""}`}>
            <Cable className="h-4 w-4 shrink-0" />
            <span className="truncate">{t("nav.connectors")}</span>
            {connectorDot === "error" ? (
              <span className="ml-auto h-2 w-2 shrink-0 rounded-full bg-red-500" title={t("navDots.connectorError")} />
            ) : connectorDot === "warn" ? (
              <span className="ml-auto h-2 w-2 shrink-0 rounded-full bg-yellow-500" title={t("navDots.connectorWarn")} />
            ) : null}
          </Link>
          {/* 定时任务（scheduled-tasks-design §3.1）：Connectors 之下、KB 之上 */}
          <Link href="/scheduled" className={`nav-item ${onScheduled ? "nav-item-active" : ""}`}>
            <AlarmClock className="h-4 w-4 shrink-0" />
            <span className="truncate">{t("nav.scheduled")}</span>
            {scheduleDot === "error" ? (
              <UnseenBadge
                n={scheduleUnseen.total}
                tone={scheduleUnseen.error > 0 || scheduleUnseen.missed > 0 ? "red" : "violet"}
                title={t("navDots.scheduleError")}
                className="ml-auto"
              />
            ) : scheduleDot === "off" ? (
              <span className="ml-auto h-2 w-2 shrink-0 rounded-full bg-faint" title={t("navDots.scheduleOff")} />
            ) : null}
          </Link>
          <Link href="/kb" className={`nav-item ${onKb ? "nav-item-active" : ""}`}>
            <BookOpen className="h-4 w-4 shrink-0" />
            <span className="truncate">{t("nav.kb")}</span>
          </Link>
          <Link href="/workflows" className={`nav-item ${onWorkflows ? "nav-item-active" : ""}`}>
            <WorkflowIcon className="h-4 w-4 shrink-0" />
            <span className="truncate">{t("nav.workflows")}</span>
          </Link>
          <Link href="/settings/connections/model-api" className={`nav-item ${onSettings ? "nav-item-active" : ""}`}>
            <SettingsIcon className="h-4 w-4 shrink-0" />
            <span className="truncate">{t("nav.settings")}</span>
          </Link>

          <div className="px-2.5 pt-2 text-[10px] text-faint">© 2026 GinnoWork</div>
        </div>
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

      {/* P1-1 一键归档确认：候选 = Earlier 里 >14 天无活动且未归档的非置顶会话 */}
      {archiveAllConfirm && (
        <ConfirmModal
          title={t("bulk.confirmArchiveTitle")}
          message={t("bulk.confirmArchiveMessage", { count: archiveCandidates.length })}
          confirmLabel={t("bulk.confirmArchiveConfirm")}
          onConfirm={() => void archiveAll()}
          onCancel={() => setArchiveAllConfirm(false)}
        />
      )}

      {/* P1-4 批量删除确认：后端会跳过仍被引用的会话并在侧栏提示 */}
      {bulkDeleteConfirm && (
        <ConfirmModal
          title={t("bulk.confirmDeleteTitle")}
          message={t("bulk.confirmDeleteMessage", { count: selected.size })}
          confirmLabel={t("bulk.confirmDeleteConfirm")}
          onConfirm={() => {
            setBulkDeleteConfirm(false);
            void runBulk("delete");
          }}
          onCancel={() => setBulkDeleteConfirm(false)}
        />
      )}

      {deleteTarget && (
        <ConfirmModal
          title={t("deleteModal.title")}
          message={`${t("deleteModal.message", {
            name: deleteTarget.title || t("session.untitled"),
          })}${
            descendantCount(deleteTarget.id) > 0
              ? ` ${t("deleteModal.cascade", { count: descendantCount(deleteTarget.id) })}`
              : ""
          }`}
          confirmLabel={t("deleteModal.confirm")}
          onConfirm={confirmDelete}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}
