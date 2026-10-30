"use client";

/** Linked sessions chip(topbar-unification.md §3)。

像挂目录一样把独立会话挂进当前对话的视野:成员是 spawn/assign/watch/
delegation 的目标 + 手动挂载,不是父子从属 —— Detach 只解除视野关联。
徽标 = 在跑数(working+waiting),面板打开 2s / 关闭 5s 轮询(TaskCard
同款节奏);旧 sidecar 无此端点(响应无 ok)整个 chip 隐藏。 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { Users, X, Square, ExternalLink, Plus, Search } from "lucide-react";
import * as api from "@/lib/runtime";
import { useGinno } from "@/lib/store";
import type { LinkedMember, LinkedReport } from "@/lib/api/sessions";
import type { SessionMeta } from "@/lib/types";
import { TopBarFuncButton } from "./TopBarFuncButton";

// 状态点语义色:全走 --st-*(主题感知,与定时任务/状态位同源)——暗色青/
// 亮色深青表示在跑、紫=完成、玫红=失败、琥珀=等待。不用绿色(白底洗白)。
const DOT: Record<string, string> = {
  working: "var(--st-running)",
  waiting: "var(--st-missed)",
  done: "var(--st-ok)",
  failed: "var(--st-error)",
  stopped: "var(--st-error)",
  idle: "var(--st-skipped)",
  unknown: "var(--st-skipped)",
  missing: "var(--st-skipped)",
};

function fmtAgo(updated: number | null): string {
  if (!updated) return "";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - updated));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h) return `${h}h`;
  if (m) return `${m}m`;
  return `${s}s`;
}

export function LinkedSessionsChip({ session }: { session: SessionMeta | null }) {
  const g = useGinno();
  const tr = useTranslations("shell");
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [rep, setRep] = useState<LinkedReport | null>(null);
  // 旧 sidecar / 会话已删:端点回非 ok → 整 chip 隐藏(不做空面板)。
  const [gone, setGone] = useState(false);
  const [search, setSearch] = useState("");
  const [results, setResults] = useState<SessionMeta[]>([]);
  const [searching, setSearching] = useState(false);
  const sessionId = session?.id ?? null;
  const sidRef = useRef(sessionId);
  sidRef.current = sessionId;

  const refresh = useCallback(async () => {
    const sid = sidRef.current;
    if (!sid) return;
    try {
      const r = await api.getLinkedSessions(sid);
      if (sid !== sidRef.current) return; // 切会话竞态:丢弃过期响应
      if (r?.ok) setRep(r);
      else setGone(true);
    } catch {
      /* sidecar 不可达:保留上次数据,徽标不炸 */
    }
  }, []);

  // 徽标 5s / 面板 2s(设计 §3.2;subagent.status 推送白拿,轮询兜底)。
  useEffect(() => {
    if (!sessionId || gone) return;
    void refresh();
    const ms = open ? 2000 : 5000;
    const t = setInterval(() => void refresh(), ms);
    return () => clearInterval(t);
  }, [sessionId, open, gone, refresh]);

  useEffect(() => {
    if (!open) {
      setSearch("");
      setResults([]);
    }
  }, [open]);

  if (!sessionId || gone) return null;

  const members = rep?.members ?? [];
  const live = rep?.live ?? 0;
  const incoming = rep?.incoming ?? [];
  // 动态键过不了 next-intl 的字面量类型闸——switch 静态枚举(状态/关系名)。
  const statusLabel = (s: LinkedMember["live_status"]) => {
    switch (s) {
      case "working": return tr("links.status.working");
      case "waiting": return tr("links.status.waiting");
      case "idle": return tr("links.status.idle");
      case "done": return tr("links.status.done");
      case "failed": return tr("links.status.failed");
      case "stopped": return tr("links.status.stopped");
      case "unknown": return tr("links.status.unknown");
      default: return tr("links.status.missing");
    }
  };
  const relLabel = (r: LinkedMember["rel"] | "watching_me" | "assigned_me") => {
    switch (r) {
      case "spawned": return tr("links.rel.spawned");
      case "delegated": return tr("links.rel.delegated");
      case "assigned": return tr("links.rel.assigned");
      case "watching": return tr("links.rel.watching");
      case "mounted": return tr("links.rel.mounted");
      case "assigned_me": return tr("links.rel.assignedMe");
      default: return tr("links.rel.watchingMe");
    }
  };
  // tooltip(chipTitle = 按钮语义;有成员时换「前 3 个标题·状态 + +N」,设计 §2.3)
  const tipLines = members.slice(0, 3).map((m) => `${m.title} · ${statusLabel(m.live_status)}`);
  const tip = members.length
    ? [...tipLines, members.length > 3 ? tr("links.more", { n: members.length - 3 }) : ""].filter(Boolean).join("\n")
    : tr("links.chipTitle");

  function openSession(id: string) {
    setOpen(false);
    // 与通知/子代理卡同一条路径:切会话 + 落到最新消息。
    g.setActiveSession(id);
    if (window.location.pathname !== "/") router.push("/");
    window.dispatchEvent(new CustomEvent("ginno:focus-latest", { detail: id }));
  }

  async function stopMember(m: LinkedMember) {
    // stop 级联(含目标子孙)且破坏性 → confirm(设计 §6)。
    if (!window.confirm(tr("links.stopConfirm", { title: m.title }))) return;
    await api.stopSession(m.id);
    await refresh();
  }

  async function detach(m: LinkedMember) {
    const sid = sidRef.current;
    if (!sid) return;
    await api.detachLinkedSession(sid, m.id);
    await refresh();
  }

  async function runSearch(q: string) {
    setSearch(q);
    if (!q.trim()) {
      setResults([]);
      return;
    }
    setSearching(true);
    try {
      const r = await api.searchSessions(q.trim(), { limit: 8 });
      const memberIds = new Set(members.map((m) => m.id));
      setResults((r.sessions || []).filter((s) => s.id !== sessionId && !memberIds.has(s.id)));
    } catch {
      setResults([]);
    } finally {
      setSearching(false);
    }
  }

  async function mount(id: string) {
    const sid = sidRef.current;
    if (!sid) return;
    const r = await api.mountLinkedSession(sid, id);
    if (r?.ok) {
      setSearch("");
      setResults([]);
      await refresh();
    }
  }

  return (
    <div className="relative shrink-0">
      <TopBarFuncButton
        onClick={() => setOpen((o) => !o)}
        open={open}
        title={tip}
        count={members.length}
        active={live > 0 ? "var(--st-running)" : undefined}
      >
        <Users className="h-4 w-4" />
      </TopBarFuncButton>

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 z-50 mt-1.5 w-80 max-w-[min(20rem,calc(100vw-2rem))] overflow-hidden rounded-xl border border-line bg-card shadow-xl">
            <div className="flex items-baseline px-3 py-2 text-xs font-medium text-muted">
              {tr("links.header")}
              {live > 0 && (
                <span className="ml-auto text-[10.5px]" style={{ color: "var(--st-running)" }}>
                  {tr("links.liveCount", { n: live })}
                </span>
              )}
            </div>

            <div className="max-h-72 overflow-y-auto px-1.5 pb-1.5">
              {members.length === 0 && (
                <div className="px-2 py-3 text-xs text-faint">{tr("links.empty")}</div>
              )}
              {members.map((m) => {
                const active = m.live_status === "working" || m.live_status === "waiting";
                return (
                  <div
                    key={m.id}
                    className={`group flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-card2 ${m.terminal ? "opacity-50" : ""}`}
                  >
                    <span
                      className="h-2 w-2 shrink-0 rounded-full"
                      style={{ background: DOT[m.live_status] ?? "var(--st-skipped)" }}
                    />
                    <button onClick={() => openSession(m.id)} className="min-w-0 flex-1 text-left" title={tr("links.open")}>
                      <div className="truncate text-xs text-txt">{m.title}</div>
                      <div className="truncate text-[10px] text-faint">
                        <span style={{ color: "var(--st-ok)" }}>{relLabel(m.rel)}</span>
                        {" · "}
                        {statusLabel(m.live_status)}
                        {m.rel !== "mounted" ? ` · ${m.type}` : ""}
                      </div>
                    </button>
                    <span className="shrink-0 text-[10px] text-faint group-hover:hidden">
                      {fmtAgo(m.updated)}
                    </span>
                    <span className="hidden shrink-0 items-center gap-1 group-hover:flex">
                      {active && (
                        <button
                          onClick={() => void stopMember(m)}
                          title={tr("links.stopTitle")}
                          className="rounded border border-red/40 px-1.5 py-px text-[10.5px] text-red hover:bg-red/10"
                        >
                          <Square className="h-3 w-3" />
                        </button>
                      )}
                      <button
                        onClick={() => openSession(m.id)}
                        title={tr("links.open")}
                        className="rounded border border-line2 p-1 text-faint hover:text-txt"
                      >
                        <ExternalLink className="h-3 w-3" />
                      </button>
                      <button
                        onClick={() => void detach(m)}
                        title={tr("links.detachTitle")}
                        className="rounded border border-line2 p-1 text-faint hover:text-txt"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </span>
                  </div>
                );
              })}

              {incoming.length > 0 && (
                <>
                  <div className="mt-1 border-t border-line px-2 pb-1 pt-2 text-[10.5px] font-medium uppercase tracking-wide text-faint">
                    {tr("links.incomingHeader")}
                  </div>
                  {incoming.map((m) => (
                    <button
                      key={m.id}
                      onClick={() => openSession(m.id)}
                      className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-card2"
                    >
                      <span
                        className="h-2 w-2 shrink-0 rounded-full"
                        style={{ background: DOT[m.live_status] ?? "var(--st-skipped)" }}
                      />
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-xs text-muted">{m.title}</div>
                        <div className="truncate text-[10px] text-faint">
                          {relLabel(m.rel)}
                        </div>
                      </div>
                    </button>
                  ))}
                </>
              )}
            </div>

            {/* 手动挂载(P2):搜索任意会话挂入视野 */}
            <div className="border-t border-line px-2.5 py-2">
              {results.length === 0 ? (
                <div className="relative">
                  <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-faint" />
                  <input
                    value={search}
                    onChange={(e) => void runSearch(e.target.value)}
                    placeholder={tr("links.mountPlaceholder")}
                    className="field w-full py-1 pl-7 pr-2 text-xs"
                  />
                </div>
              ) : (
                <div className="max-h-40 overflow-y-auto">
                  {results.map((s) => (
                    <button
                      key={s.id}
                      onClick={() => void mount(s.id)}
                      className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-card2"
                    >
                      <Plus className="h-3 w-3 shrink-0 text-faint" />
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-xs text-txt">{s.title || s.id.slice(0, 8)}</div>
                        <div className="truncate font-mono text-[10px] text-faint">{s.id.slice(0, 8)}</div>
                      </div>
                    </button>
                  ))}
                  <button
                    onClick={() => {
                      setSearch("");
                      setResults([]);
                    }}
                    className="mt-0.5 w-full px-2 text-left text-[10.5px] text-faint hover:text-txt"
                  >
                    {searching ? tr("links.searching") : tr("links.clearSearch")}
                  </button>
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
