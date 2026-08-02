"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import {
  ChevronDown,
  BookOpen,
  Settings as SettingsIcon,
  Plus,
  Workflow as WorkflowIcon,
  Pencil,
  Trash2,
} from "lucide-react";
import { useGinno } from "@/lib/store";
import { agentHex } from "@/lib/theme";
import { Icon } from "@/components/icons";
import { ConfirmModal } from "@/components/ConfirmModal";
import { applyTheme } from "@/components/settings/GeneralSettings";
import type { SessionMeta } from "@/lib/types";

function SectionHeader({
  icon,
  label,
  onAdd,
}: {
  icon: React.ReactNode;
  label: string;
  onAdd?: () => void;
}) {
  return (
    <div className="mb-1.5 flex items-center gap-1.5 px-2.5 text-xs font-medium text-faint">
      {icon}
      <span>{label}</span>
      <span className="ml-auto flex items-center gap-1">
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
  // Escape cancels the rename, but the input's onBlur (which commits) can fire on
  // unmount in some browsers (Firefox); this ref lets onBlur skip the commit.
  const cancelRename = useRef(false);
  // custom in-app delete confirmation (window.confirm is unreliable in the Tauri
  // webview; a React-rendered modal works in both Tauri and the browser)
  const [deleteTarget, setDeleteTarget] = useState<SessionMeta | null>(null);
  const confirmDelete = () => {
    if (deleteTarget) g.removeSession(deleteTarget.id);
    setDeleteTarget(null);
  };

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
      <main className="flex min-w-0 flex-1">{children}</main>

      {deleteTarget && (
        <ConfirmModal
          title="删除会话"
          message={`确定删除会话「${deleteTarget.title || "Untitled"}」？其对话历史将一并删除，且无法恢复。`}
          confirmLabel="删除"
          onConfirm={confirmDelete}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}
