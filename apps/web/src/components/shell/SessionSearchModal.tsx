"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Search } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import { relTime } from "@/lib/utils";
import { agentHex } from "@/lib/theme";
import { AgentIcon } from "@/components/icons";
import type { SessionMeta } from "@/lib/types";

/** ⌘K session search: title ∪ agent-name substring filter over sessions
 *  sorted by last activity; ↑↓/Enter/click opens the session
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

  const results = useMemo(() => {
    // 影子会话整类不进搜索（scheduled-tasks-design §3.6/§10 决议 5）。
    const sorted = [...g.sessions]
      .filter((s) => s.type !== "scheduled")
      .sort((a, b) => (b.updated ?? 0) - (a.updated ?? 0));
    const needle = q.trim().toLowerCase();
    if (!needle) return sorted;
    // C+ 方案⑤：标题 ∪ Agent 名匹配——直接输「调研」能找到该 agent 经手的
    // 所有会话（agent 被删除的会话仍可凭标题命中）。
    const agentName = (s: SessionMeta) =>
      (g.agents.find((a) => a.id === s.agent_id)?.name || "").toLowerCase();
    return sorted.filter(
      (s) => (s.title || "").toLowerCase().includes(needle) || agentName(s).includes(needle),
    );
  }, [g.sessions, g.agents, q]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);
  useEffect(() => {
    setActive(0);
  }, [q]);

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
          {results.length === 0 && (
            <div className="px-3 py-6 text-center text-xs text-faint">{tr("search.empty")}</div>
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
        <div className="border-t border-line px-3 py-1.5 text-[10px] text-faint">
          {tr("search.footer")}
        </div>
      </div>
    </div>
  );
}
