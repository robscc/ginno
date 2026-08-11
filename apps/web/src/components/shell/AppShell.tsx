"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import {
  BookOpen,
  Settings as SettingsIcon,
  Plus,
  Search,
  Workflow as WorkflowIcon,
  Pencil,
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
import { SheetViewer } from "@/components/chat/SheetViewer";
import { RightPanel } from "@/components/right/RightPanel";
import { RightDock } from "@/components/right/RightDock";
import type { SessionMeta, SessionUsage } from "@/lib/types";

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
    if (deleteTarget) g.removeSession(deleteTarget.id);
    setDeleteTarget(null);
  };

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
  const modelLabel = session?.model || session?.provider || g.defaultProvider || "model";
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
  const sortedSessions = [...g.sessions].sort((a, b) => (b.updated ?? 0) - (a.updated ?? 0));
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

  const renderSessionRow = (s: SessionMeta) => {
    const sel = onWorkspace && s.id === g.activeSessionId;
    const hex = agentHex(g.agents.find((a) => a.id === s.agent_id)?.color);
    const editing = editingId === s.id;
    return (
      <div
        key={s.id}
        className={`nav-item group ${sel ? "text-txt" : ""}`}
        style={sel ? { background: "rgba(99,102,241,0.14)" } : undefined}
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
              onClick={() => {
                g.setActiveSession(s.id);
                if (!onWorkspace) router.push("/");
              }}
              onDoubleClick={(e) => {
                e.stopPropagation();
                setEditTitle(s.title || "");
                setEditingId(s.id);
              }}
              className="flex min-w-0 flex-1 items-center gap-2.5 text-left"
            >
              <Icon
                name={s.icon || "message-square"}
                className="h-4 w-4 shrink-0"
                style={{ color: hex }}
              />
              <span className="truncate">{s.title || "Untitled"}</span>
              <span className="ml-auto shrink-0 text-[10px] text-faint">
                {relTime(s.updated ?? s.created)}
              </span>
            </button>
            <span className="flex shrink-0 items-center gap-0.5">
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
            </span>
          </>
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
              <Plus className="h-4 w-4" />
              <span>新建会话</span>
              <kbd className="ml-auto rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-faint">⌘N</kbd>
            </button>
            <button onClick={() => setSearchOpen(true)} className="nav-item">
              <Search className="h-4 w-4" />
              <span>搜索会话</span>
              <kbd className="ml-auto rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-faint">⌘K</kbd>
            </button>
          </div>

          {/* sessions grouped by activity day */}
          {sessionGroups.map(([label, rows]) =>
            rows.length ? (
              <div key={label} className="mb-1">
                <div className="px-2.5 pb-1 pt-3 text-[11px] font-medium text-faint">{label}</div>
                <div className="space-y-0.5">{rows.map(renderSessionRow)}</div>
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
            <BookOpen className="h-4 w-4" />
            <span>Knowledge Base</span>
          </Link>
          <Link href="/workflows" className={`nav-item ${onWorkflows ? "nav-item-active" : ""}`}>
            <WorkflowIcon className="h-4 w-4" />
            <span>Workflows</span>
          </Link>
          <Link href="/settings/model-api" className={`nav-item ${onSettings ? "nav-item-active" : ""}`}>
            <SettingsIcon className="h-4 w-4" />
            <span>Settings</span>
          </Link>

          <div className="px-2.5 pt-2 text-[10px] text-faint">© 2025 GinnoWork</div>
        </div>
      </aside>

      {/* main */}
      <main className="flex min-w-0 flex-1">
        {/* Workspace: always mounted so ChatStream refs (WS, store, error cards)
            survive navigating to /settings or /kb and back. Hidden off-route. */}
        <div className={`flex min-w-0 flex-1 ${onWorkspace ? "" : "hidden"}`}>
          <div className="flex min-w-0 flex-1 flex-col">
            {session && (
              <TopBar session={session} agent={agent} running={running} modelLabel={modelLabel} usage={usage} />
            )}
            <ChatStream
              session={session}
              onRunningChange={setRunning}
              onUsageChange={setUsage}
              onOpenGoal={() => setGoalSessionModal(true)}
            />
          </div>
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
          message={`确定删除会话「${deleteTarget.title || "Untitled"}」？其对话历史将被删除且无法恢复；会话产生的文件会保留，可在 设置 → 会话文件 中查看或清理。`}
          confirmLabel="删除"
          onConfirm={confirmDelete}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}
