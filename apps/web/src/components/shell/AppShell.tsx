"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import {
  ChevronDown,
  BookOpen,
  Settings as SettingsIcon,
  Plus,
  Target,
  Workflow as WorkflowIcon,
  Pencil,
  Trash2,
} from "lucide-react";
import { GoalEditor } from "@/components/shell/GoalChip";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import { agentHex } from "@/lib/theme";
import { Icon } from "@/components/icons";
import { ConfirmModal } from "@/components/ConfirmModal";
import { applyTheme } from "@/components/settings/GeneralSettings";
import { TopBar } from "@/components/shell/TopBar";
import { ChatStream } from "@/components/chat/ChatStream";
import { SheetViewer } from "@/components/chat/SheetViewer";
import { RightPanel } from "@/components/right/RightPanel";
import type { SessionMeta, SessionUsage } from "@/lib/types";

function SectionHeader({
  icon,
  label,
  onAdd,
  onGoal,
}: {
  icon: React.ReactNode;
  label: string;
  onAdd?: () => void;
  onGoal?: () => void;
}) {
  return (
    <div className="mb-1.5 flex items-center gap-1.5 px-2.5 text-xs font-medium text-faint">
      {icon}
      <span>{label}</span>
      <span className="ml-auto flex items-center gap-1">
        {onGoal && (
          <button
            onClick={onGoal}
            title="目标会话（Agent 自主多轮推进）"
            className="rounded p-0.5 text-faint transition-colors hover:bg-card hover:text-violet"
          >
            <Target className="h-3.5 w-3.5" />
          </button>
        )}
        {onAdd && (
          <button
            onClick={onAdd}
            title="新建会话"
            className="rounded p-0.5 text-faint transition-colors hover:bg-card hover:text-txt"
          >
            <Plus className="h-3.5 w-3.5" />
          </button>
        )}
        <ChevronDown className="h-3.5 w-3.5" />
      </span>
    </div>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const g = useGinno();
  const pathname = usePathname();
  const router = useRouter();
  const active = g.sessions.find((s) => s.id === g.activeSessionId) ?? null;
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
    if (g.sessions.length) {
      if (!g.activeSessionId) g.setActiveSession(g.sessions[0].id);
      didInit.current = true;
    } else {
      didInit.current = true;
      g.newSession(g.agents[0]?.id);
    }
  }, [g.ready, g.sessions, g.agents, g.activeSessionId, g]);

  const session = g.sessions.find((s) => s.id === g.activeSessionId) ?? g.sessions[0] ?? null;
  const agent = session ? g.agents.find((a) => a.id === session.agent_id) ?? null : null;
  const modelLabel = session?.model || session?.provider || g.defaultProvider || "model";
  // ─────────────────────────────────────────────────────────────────────────

  const onNewSession = async () => {
    const s = await g.newSession(g.agents[0]?.id);
    if (s) router.push("/"); // success -> show the new session (error, if any, is in g.sessionError)
  };

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

  const onWorkspace = pathname === "/";
  const onSettings = pathname.startsWith("/settings");
  const onKb = pathname.startsWith("/kb");
  const onWorkflows = pathname.startsWith("/workflows");

  return (
    <div className="flex h-screen w-full overflow-hidden bg-base text-txt">
      {/* left nav */}
      <aside className="flex w-64 shrink-0 flex-col border-r border-line bg-panel">
        {/* brand */}
        <div className="flex items-center gap-2.5 px-4 py-4">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-violet/15 text-violet">
            <Icon name="star" className="h-4 w-4 fill-violet" />
          </div>
          <span className="text-[15px] font-semibold tracking-tight">GinnoWork</span>
        </div>

        <div className="flex-1 overflow-y-auto px-2.5 pb-2">
          {/* sessions */}
          <SectionHeader
            icon={<Icon name="message-square" className="h-3.5 w-3.5" />}
            label="Sessions"
            onAdd={onNewSession}
            onGoal={() => setGoalSessionModal(true)}
          />
          <div className="mb-4 space-y-0.5">
            {g.sessions.length === 0 && (
              <div className="px-2.5 py-1 text-xs text-faint">No sessions yet</div>
            )}
            {g.sessions.map((s) => {
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
            })}
          </div>

          {g.sessionError && (
            <button
              onClick={() => router.push("/settings/model-api")}
              title="点击前往 设置 → 模型 API 配置"
              className="mx-1 mb-3 block rounded-md border border-yellow/40 bg-yellow/10 px-2 py-1.5 text-left text-[11px] leading-snug text-yellow hover:bg-yellow/15"
            >
              {g.sessionError}
            </button>
          )}

          {/* agents */}
          <SectionHeader icon={<Icon name="boxes" className="h-3.5 w-3.5" />} label="Agents" />
          <div className="space-y-0.5">
            {g.agents.map((a) => {
              const isActive =
                a.status === "running" || a.status === "active" || a.id === active?.agent_id;
              const hex = agentHex(a.color);
              return (
                <div key={a.id} className="nav-item cursor-default">
                  <span
                    className="flex h-5 w-5 items-center justify-center rounded-md"
                    style={{ background: hex + "22", color: hex }}
                  >
                    <Icon name={a.icon} className="h-3.5 w-3.5" />
                  </span>
                  <span className="truncate text-txt">{a.name}</span>
                  <span className="ml-auto flex items-center gap-1 text-[11px]">
                    <span
                      className="h-1.5 w-1.5 rounded-full"
                      style={{ background: isActive ? "#22c55e" : "#52525b" }}
                    />
                    <span style={{ color: isActive ? "#4ade80" : "#71717a" }}>
                      {isActive ? "Active" : "Idle"}
                    </span>
                  </span>
                </div>
              );
            })}
          </div>
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
            <TopBar session={session} agent={agent} running={running} modelLabel={modelLabel} usage={usage} />
            <ChatStream session={session} onRunningChange={setRunning} onUsageChange={setUsage} />
          </div>
          <RightPanel />
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
