"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Search } from "lucide-react";
import { useGinno } from "@/lib/store";
import { relTime } from "@/lib/utils";
import { agentHex } from "@/lib/theme";
import { Icon } from "@/components/icons";
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
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const results = useMemo(() => {
    const sorted = [...g.sessions].sort((a, b) => (b.updated ?? 0) - (a.updated ?? 0));
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
        aria-label="搜索会话"
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
            placeholder="搜索会话标题，或直接输 Agent 名…"
            className="w-full bg-transparent py-3 text-sm text-txt outline-none placeholder:text-faint"
          />
        </div>
        <div className="max-h-[46vh] overflow-y-auto py-1">
          {results.length === 0 && (
            <div className="px-3 py-6 text-center text-xs text-faint">没有匹配的会话</div>
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
              <Icon
                name={s.icon || "message-square"}
                className="h-4 w-4 shrink-0"
                style={{ color: hex }}
              />
              <span className="min-w-0 flex-1 truncate">{s.title || "Untitled"}</span>
              {/* C+ 方案⑤：agent dot + name 小标签（agent 已删除时不渲染） */}
              {rowAgent && (
                <span
                  className="inline-flex shrink-0 items-center gap-1 rounded-full border px-1.5 text-[10px] leading-4"
                  style={{ borderColor: hex + "44", background: hex + "14", color: hex }}
                >
                  <span className="h-1 w-1 rounded-full" style={{ background: hex }} />
                  {rowAgent.name}
                </span>
              )}
              <span className="shrink-0 text-[11px] text-faint">{relTime(s.updated ?? s.created)}</span>
            </button>
            );
          })}
        </div>
        <div className="border-t border-line px-3 py-1.5 text-[10px] text-faint">
          ↑↓ 选择 · Enter 打开 · Esc 关闭
        </div>
      </div>
    </div>
  );
}
