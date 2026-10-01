"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import {
  BookOpen,
  ChevronDown,
  Settings as SettingsIcon,
  Plus,
  Search,
  Workflow as WorkflowIcon,
  Pencil,
  Square,
  Trash2,
} from "lucide-react";
import { GoalEditor } from "@/components/shell/GoalChip";
import { useGinno, LAST_SESSION_KEY } from "@/lib/store";
import * as api from "@/lib/runtime";
import { agentHex } from "@/lib/theme";
import { relTime } from "@/lib/utils";
import { Icon } from "@/components/icons";
import { ConfirmModal } from "@/components/ConfirmModal";
import { applyTheme } from "@/components/settings/GeneralSettings";
import { TopBar } from "@/components/shell/TopBar";
import { SessionSearchModal } from "@/components/shell/SessionSearchModal";
import { ChatStream } from "@/components/chat/ChatStream";
import { SUBAGENT_STATUS_META, SubagentKindBadges } from "@/components/chat/blocks";
import { RunSubSessionView } from "@/components/chat/RunSubSessionView";
import { RUN_STATUS_META } from "@/components/chat/RunBlocks";
import { SheetViewer } from "@/components/chat/SheetViewer";
import { RightPanel } from "@/components/right/RightPanel";
import { RightDock } from "@/components/right/RightDock";
import type { SessionMeta, SessionUsage, WorkflowRun } from "@/lib/types";

export function AppShell({ children }: { children: React.ReactNode }) {
  const g = useGinno();
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
  const setActiveSessionForNav = g.setActiveSession;
  const goHome = () => {
    setActiveSessionForNav(null);
    if (pathname !== "/") router.push("/");
  };
  const onNewSession = goHome;

  // apply persisted theme as early as the shell mounts
  useEffect(() => {
    let t = "dark";
    try {
      t = localStorage.getItem("ginno-theme") || "dark";
    } catch {
      /* ignore */
    }
    applyTheme(t);
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
    return () => {
      delete (window as unknown as { __ginnoOpenSession?: unknown }).__ginnoOpenSession;
      delete (window as unknown as { __ginnoOpenWorkflowRun?: unknown }).__ginnoOpenWorkflowRun;
    };
  }, [setActiveSession, setRightTab, setRightPanelOpenForBridge, router]);

  const onWorkspace = pathname === "/";
  const onSettings = pathname.startsWith("/settings");
  const onKb = pathname.startsWith("/kb");
  const onWorkflows = pathname.startsWith("/workflows");

  // Sidebar sessions: activity-day groups, newest activity first. `updated`
  // is bumped per turn server-side, so it tracks last use, not creation.
  // C+ 方案③：agent 筛选作用于分组前的列表，分组/排序逻辑不变。
  //
  // subagent 树（subagent-design.md §6.1）：先按活动日分主行，再把子会话
  // （type==="subagent"）按 parent_session_id 挂到父行下嵌套渲染。子会话不进
  // 天分组——它的活动不应顶起父会话的排序（设计文档开放问题 2 的 UI 侧答案）。
  const visibleSessions = g.sessions.filter(
    (s) => !agentFilter || s.agent_id === agentFilter,
  );
  // 父 id → 直属子会话（按创建时间正序）。
  const childrenOf = new Map<string, SessionMeta[]>();
  for (const s of visibleSessions) {
    if (s.type !== "subagent" || !s.parent_session_id) continue;
    const list = childrenOf.get(s.parent_session_id) ?? [];
    list.push(s);
    childrenOf.set(s.parent_session_id, list);
  }
  for (const list of childrenOf.values()) {
    list.sort((a, b) => (a.created ?? 0) - (b.created ?? 0));
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
    s.type === "subagent" && !!s.parent_session_id &&
    visibleSessions.some((p) => p.id === s.parent_session_id);
  const sortedSessions = [...visibleSessions]
    .filter((s) => !hasVisibleParent(s))
    .sort((a, b) => (b.updated ?? 0) - (a.updated ?? 0));
  const dayStart = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const todayMs = dayStart(new Date());
  const groupOf = (s: SessionMeta): "今天" | "昨天" | "更早" => {
    const day = dayStart(new Date((s.updated ?? s.created) * 1000));
    if (day >= todayMs) return "今天";
    if (day >= todayMs - 86400000) return "昨天";
    return "更早";
  };
  const sessionGroups: Array<["今天" | "昨天" | "更早", SessionMeta[]]> = [
    ["今天", sortedSessions.filter((s) => groupOf(s) === "今天")],
    ["昨天", sortedSessions.filter((s) => groupOf(s) === "昨天")],
    ["更早", sortedSessions.filter((s) => groupOf(s) === "更早")],
  ];

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
            <span className="shrink-0 text-[11px] leading-none" title={`状态：${meta.label}`}>
              {meta.emoji}
            </span>
          ) : (
            <WorkflowIcon className="h-4 w-4 shrink-0 text-muted" />
          )}
          <span className="truncate">{r.name || wfName || "Workflow"}</span>
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
              aria-label="取消运行"
              title="取消该运行"
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
              aria-label="删除运行记录"
              title="删除该运行记录"
              className="rounded p-1 text-muted opacity-0 transition-opacity hover:bg-card2 hover:text-red group-hover:opacity-100"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          )}
        </span>
      </div>
    );
  };

  const renderSessionRow = (s: SessionMeta, depth = 0, childRows: SessionMeta[] = []) => {
    const sel = onWorkspace && s.id === g.activeSessionId;
    const rowAgent = g.agents.find((a) => a.id === s.agent_id) ?? null;
    const hex = agentHex(rowAgent?.color);
    const editing = editingId === s.id;
    const isSub = s.type === "subagent";
    const subTypeName = isSub
      ? String((s.subagent as { agent_type?: unknown } | undefined)?.agent_type ?? "").trim()
      : "";
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
      g.setActiveSession(s.id);
      if (!onWorkspace) router.push("/");
    };
    return (
      <div key={s.id}>
        <div
          className={`nav-item group ${sel ? "text-txt" : ""} ${
            isSub && !subActive && !sel ? "opacity-60" : ""
          }`}
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
                  e.stopPropagation();
                  setEditTitle(s.title || "");
                  setEditingId(s.id);
                }}
                className="flex min-w-0 flex-1 items-center gap-2.5 text-left"
              >
                {isSub && subMeta ? (
                  // 子会话行：状态 emoji 取代会话图标（subagent-design.md §6.1）
                  <span
                    className="shrink-0 text-[11px] leading-none"
                    title={`状态：${subMeta.label}`}
                  >
                    {subMeta.glyph}
                  </span>
                ) : (
                  <Icon
                    name={s.icon || "message-square"}
                    className="h-4 w-4 shrink-0"
                    style={{ color: hex }}
                  />
                )}
                <span className="truncate">{s.title || "Untitled"}</span>
                {/* fork / 子代理类型徽标（P3 范围 3）：仅子会话行渲染 */}
                {isSub && <SubagentKindBadges sub={s.subagent} />}
                {/* 悬浮速聊窗创建的 quick 会话角标（floating-window-design.md §1.1） */}
                {s.type === "quick" && (
                  <span title="速聊会话（来自悬浮窗）" className="shrink-0 text-[10px] text-yellow">
                    ⚡
                  </span>
                )}
                {/* C+ 方案③：会话行 agent 名小标签（agent 已删除时不渲染）。
                    子会话例外：persona 是继承父会话的（永远是 Dev Agent），用它
                    标注没有信息量——改显示子代理类型（与主对话卡片、子会话顶栏
                    一致）；没有类型才退回 persona 名。 */}
                {isSub && subTypeName ? (
                  <span
                    className="shrink-0 rounded-full border border-violet/40 bg-violet/10 px-1.5 text-[10px] leading-4 text-violet"
                    title={`子代理类型：${subTypeName}`}
                  >
                    {subTypeName}
                  </span>
                ) : rowAgent ? (
                  <span
                    className="shrink-0 rounded-full border px-1.5 text-[10px] leading-4"
                    style={{ borderColor: hex + "44", background: hex + "14", color: hex }}
                  >
                    {rowAgent.name}
                  </span>
                ) : null}
                {/* 子树还有更深的后代：行尾 +N 尾标（设计 §6.1） */}
                {hasKids && descendantCount(s.id) > childRows.length && (
                  <span
                    className="shrink-0 text-[10px] text-faint"
                    title={`还有 ${descendantCount(s.id) - childRows.length} 个嵌套子任务`}
                  >
                    +{descendantCount(s.id) - childRows.length}
                  </span>
                )}
                <span className="ml-auto shrink-0 text-[10px] text-faint">
                  {relTime(s.updated ?? s.created)}
                </span>
              </button>
              {/* 父行徽标：运行中子代理/运行数量（设计 §6.1 的「● 2 agents」） */}
              {hasKids && activeKids > 0 && (
                <span
                  className="flex shrink-0 items-center gap-1 rounded-full border border-line2 px-1.5 text-[10px] leading-4 text-muted"
                  title={`${activeKids} 个子任务/运行进行中`}
                >
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-green" />
                  {activeKids}
                </span>
              )}
              <span className="flex shrink-0 items-center gap-0.5">
                {hasKids && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      // 存的是「新的折叠态」：当前展开 → 收起（true）。
                      g.setTreeCollapsed(s.id, expanded);
                    }}
                    aria-label={expanded ? "折叠子任务" : "展开子任务"}
                    title={expanded ? "折叠子任务" : "展开子任务"}
                    className="rounded p-1 text-muted hover:bg-card2 hover:text-txt"
                  >
                    <ChevronDown
                      className={`h-3.5 w-3.5 transition-transform ${expanded ? "" : "-rotate-90"}`}
                    />
                  </button>
                )}
                {isSub ? (
                  // 子会话行 hover 入口（P2 共享契约 4）：运行中/等待 → 停止
                  // （HTTP 端点，running 走协作式停止、waiting 级联停后代；
                  // 状态刷新依赖既有 subagent.status 事件）；结束态 → 清除行
                  // （删除该子会话）。
                  subActive ? (
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        void api.stopSession(s.id).catch(() => {
                          /* 端点不可达——状态仍以 subagent.status 事件为准 */
                        });
                      }}
                      aria-label="停止子任务"
                      title="停止子任务（含其运行中的后代）"
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
                      aria-label="清除已结束的子任务"
                      title="清除（删除该子会话行）"
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
                        setEditTitle(s.title || "");
                        setEditingId(s.id);
                      }}
                      aria-label="重命名会话"
                      title="重命名（也可双击标题）"
                      className="rounded p-1 text-muted hover:bg-card2 hover:text-txt"
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        setDeleteTarget(s);
                      }}
                      aria-label="删除会话"
                      title="删除会话"
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
            {childRows.map((c) => renderSessionRow(c, depth + 1, childrenOf.get(c.id) ?? []))}
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
        <div className="flex items-center gap-2.5 px-4 py-4">
          <img src="/icon.png" alt="" className="h-7 w-7" />
          <span className="text-[15px] font-semibold tracking-tight">GinnoWork</span>
        </div>

        <div className="flex-1 overflow-y-auto px-2.5 pb-2">
          {/* primary actions first (open-experience prototype) */}
          <div className="mb-1 space-y-0.5 border-b border-line pb-2.5">
            <button onClick={onNewSession} className="nav-item" title="回到着陆首页，会话在首次发送时创建">
              <Plus className="h-4 w-4 shrink-0" />
              <span className="truncate">新建会话</span>
              <kbd className="ml-auto shrink-0 rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-faint">⌘N</kbd>
            </button>
            <button onClick={() => setSearchOpen(true)} className="nav-item">
              <Search className="h-4 w-4 shrink-0" />
              <span className="truncate">搜索会话</span>
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
                  title={sel ? "取消筛选" : `只看 ${a.name} 的会话`}
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

          {/* sessions grouped by activity day */}
          {sessionGroups.map(([label, rows]) =>
            rows.length ? (
              <div key={label} className="mb-1">
                <div className="px-2.5 pb-1 pt-3 text-[11px] font-medium text-faint">{label}</div>
                <div className="space-y-0.5">
                  {rows.map((s) => renderSessionRow(s, 0, childrenOf.get(s.id) ?? []))}
                </div>
              </div>
            ) : null,
          )}
          {g.sessions.length === 0 && (
            <div className="px-2.5 py-2 text-xs leading-relaxed text-faint">
              还没有会话。
              <br />
              点「新建会话」或 ⌘N 开始第一个对话。
            </div>
          )}
          {/* C+ 方案③：有会话但当前筛选下为空——独立兜底文案，避免误以为没有会话 */}
          {g.sessions.length > 0 && sortedSessions.length === 0 && (
            <div className="px-2.5 py-2 text-xs leading-relaxed text-faint">
              该 Agent 下暂无会话。
              <br />
              再点一次高亮的筛选 chip 可取消筛选。
            </div>
          )}

          {g.sessionError && (
            <button
              onClick={() => router.push("/settings/model-api")}
              title="点击前往 设置 → 模型 API 配置"
              className="mx-1 mb-3 block rounded-md border border-yellow/40 bg-yellow/10 px-2 py-1.5 text-left text-[11px] leading-snug text-yellow hover:bg-yellow/15"
            >
              {g.sessionError}
            </button>
          )}
        </div>

        {/* footer nav */}
        <div className="border-t border-line px-2.5 py-3">
          <Link href="/kb" className={`nav-item ${onKb ? "nav-item-active" : ""}`}>
            <BookOpen className="h-4 w-4 shrink-0" />
            <span className="truncate">Knowledge Base</span>
          </Link>
          <Link href="/workflows" className={`nav-item ${onWorkflows ? "nav-item-active" : ""}`}>
            <WorkflowIcon className="h-4 w-4 shrink-0" />
            <span className="truncate">Workflows</span>
          </Link>
          <Link href="/settings/model-api" className={`nav-item ${onSettings ? "nav-item-active" : ""}`}>
            <SettingsIcon className="h-4 w-4 shrink-0" />
            <span className="truncate">Settings</span>
          </Link>

          <div className="px-2.5 pt-2 text-[10px] text-faint">© 2025 GinnoWork</div>
        </div>
      </aside>

      {/* main */}
      <main className="flex min-w-0 flex-1">
        {/* Workspace: always mounted so ChatStream refs (WS, store, error cards)
            survive navigating to /settings or /kb and back. Hidden off-route.
            打开 run 视图时同样用 hidden（不卸载）——会话流的 WS/草稿等 ref 状态
            在回看运行期间保持存活。 */}
        <div className={`flex min-w-0 flex-1 ${onWorkspace ? "" : "hidden"}`}>
          <div className={`flex min-w-0 flex-1 flex-col ${g.activeRunId ? "hidden" : ""}`}>
            {session && (
              <TopBar session={session} agent={agent} running={running} usage={usage} />
            )}
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
        {/* Non-workspace routes (settings, kb, workflows) */}
        {!onWorkspace && <div className="flex min-w-0 flex-1">{children}</div>}
      </main>

      {goalSessionModal && (
        <GoalEditor
          initial=""
          title="目标会话 — 设定长程目标"
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

      {deleteTarget && (
        <ConfirmModal
          title="删除会话"
          message={`确定删除会话「${deleteTarget.title || "Untitled"}」？其对话历史将被删除且无法恢复；会话产生的文件会保留，可在 设置 → 会话文件 中查看或清理。${
            descendantCount(deleteTarget.id) > 0
              ? ` 将同时删除它的 ${descendantCount(deleteTarget.id)} 个子 agent 会话（运行中的会先协作式停止）。`
              : ""
          }`}
          confirmLabel="删除"
          onConfirm={confirmDelete}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}
