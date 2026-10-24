"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Search } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import { relTime } from "@/lib/utils";
import { agentHex } from "@/lib/theme";
import { AgentIcon } from "@/components/icons";
import type { SessionMeta } from "@/lib/types";
import { hiddenSessionIds } from "@/lib/sessionVisibility";

/** ⌘K session search: non-empty queries hit the server-side search (P1-3,
 *  200ms debounce, 50 per page) so 179+ sessions stay responsive; an empty
 *  query keeps the local recent list. Endpoint failure falls back to the old
 *  in-memory title ∪ agent-name filter. ↑↓/Enter/click opens the session
 *  (open-experience redesign). */
export function SessionSearchModal({
  onClose,
  onOpen,
}: {
  onClose: () => void;
  onOpen: (sessionId: string) => void;
}) {
  const g = useGinno();
  const tr = useTranslations("shell");
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  // P1-3 服务端搜索结果：{sessions,total}；null = 没有可用结果（空查询/请求中）。
  const [server, setServer] = useState<{ sessions: SessionMeta[]; total: number } | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const needle = q.trim();

  // 非空查询走防抖 200ms 的服务端搜索；每次查询变化先清掉上一轮结果，避免在
  // 防抖窗内按 Enter 打开一个已经不属于当前关键词的会话。
  useEffect(() => {
    if (!needle) {
      setServer(null);
      setLoading(false);
      setFailed(false);
      return;
    }
    let alive = true;
    setServer(null);
    setLoading(true);
    const timer = setTimeout(async () => {
      try {
        const r = await api.searchSessions(needle, { limit: 50 });
        if (!alive) return;
        setServer({ sessions: r.sessions ?? [], total: r.total ?? 0 });
        setFailed(false);
      } catch {
        if (!alive) return;
        setFailed(true); // 端点不可用 → 退回本地过滤（见 results）
      } finally {
        if (alive) setLoading(false);
      }
    }, 200);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [needle]);

  const results = useMemo(() => {
    // 影子/团队空间会话整类不进搜索（scheduled-tasks-design §3.6/§10 决议 5），
    // 其子树（定时任务拉起的 subagent 等）随父级一并排除（sessionVisibility）。
    const hidden = hiddenSessionIds(g.sessions);
    const now = Math.floor(Date.now() / 1000);
    const visible = [...g.sessions]
      .filter((s) => !hidden.has(s.id))
      .sort((a, b) => (b.updated ?? 0) - (a.updated ?? 0));
    // 空查询的「最近会话」列表按 P1-1 的归档语义收口：archived 且 7 天无活动
    // 的不再占位；但搜索本身仍能命中它们（归档 = 藏起来，不是丢掉）。
    const recent = visible.filter(
      (s) => !(s.archived === true && now - (s.updated ?? 0) > 7 * 86400),
    );
    if (!needle) return recent;
    if (server) return server.sessions.filter((s) => !hidden.has(s.id));
    if (!failed) return []; // 首次请求在飞：交给 loading 态
    // 服务端搜索失败 → 客户端兜底：标题 ∪ Agent 名匹配（C+ 方案⑤，与旧行为
    // 一致——直接输「调研」能找到该 agent 经手的所有会话）。
    const agentName = (s: SessionMeta) =>
      (g.agents.find((a) => a.id === s.agent_id)?.name || "").toLowerCase();
    const ql = needle.toLowerCase();
    return visible.filter(
      (s) => (s.title || "").toLowerCase().includes(ql) || agentName(s).includes(ql),
    );
  }, [g.sessions, g.agents, needle, server, failed]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);
  useEffect(() => {
    setActive(0);
  }, [q, server]);
  // 结果集变短（服务端分页/关键词变化）时把高亮行夹回范围内。
  useEffect(() => {
    setActive((a) => Math.min(a, Math.max(0, results.length - 1)));
  }, [results.length]);

  const pick = (sid: string) => {
    onOpen(sid);
    onClose();
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 pt-[12vh]"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-label={tr("search.dialogLabel")}
        className="w-[560px] max-w-[90vw] overflow-hidden rounded-xl border border-line bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-line px-3">
          <Search className="h-4 w-4 shrink-0 text-faint" />
          <input
            ref={inputRef}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setActive((a) => Math.min(a + 1, Math.max(0, results.length - 1)));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setActive((a) => Math.max(a - 1, 0));
              } else if (e.key === "Enter" && results[active]) {
                e.preventDefault();
                pick(results[active].id);
              } else if (e.key === "Escape") {
                e.preventDefault();
                onClose();
              }
            }}
            placeholder={tr("search.placeholder")}
            className="w-full bg-transparent py-3 text-sm text-txt outline-none placeholder:text-faint"
          />
        </div>
        <div className="max-h-[46vh] overflow-y-auto py-1">
          {results.length === 0 && !loading && (
            <div className="px-3 py-6 text-center text-xs text-faint">{tr("search.empty")}</div>
          )}
          {results.length === 0 && loading && (
            <div className="px-3 py-6 text-center text-xs text-faint">{tr("search.loading")}</div>
          )}
          {results.map((s, i) => {
            const rowAgent = g.agents.find((a) => a.id === s.agent_id) ?? null;
            const hex = agentHex(rowAgent?.color);
            return (
            <button
              key={s.id}
              onMouseEnter={() => setActive(i)}
              onClick={() => pick(s.id)}
              className={`flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm transition-colors ${
                i === active ? "bg-card2 text-txt" : "text-muted"
              }`}
            >
              <AgentIcon
                name={rowAgent?.icon}
                className="h-4 w-4 shrink-0"
                style={{ color: hex }}
              />
              <span className="min-w-0 flex-1 truncate">{s.title || tr("session.untitled")}</span>
              <span className="shrink-0 text-[11px] text-faint">{relTime(s.updated ?? s.created)}</span>
            </button>
            );
          })}
        </div>
        <div className="flex items-center justify-between gap-2 border-t border-line px-3 py-1.5 text-[10px] text-faint">
          <span className="truncate">{tr("search.footer")}</span>
          {needle && (
            <span className="shrink-0">
              {loading
                ? tr("search.loading")
                : server
                  ? tr("search.total", { total: server.total })
                  : failed
                    ? tr("search.fallback")
                    : ""}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
