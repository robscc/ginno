"use client";

// P0-1/P0-2 共享层：消息片段行 / 标题命中行 / snippet 高亮 + 消息锚点工具。
// SearchModal（⌘K）与 AppShell 侧栏「已归档」搜索共用同一套紧凑行与高亮，
// 避免两处渲染漂移。

import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import { relTime } from "@/lib/utils";
import { agentHex } from "@/lib/theme";
import { AgentIcon } from "@/components/icons";
import type { SearchMessageHit } from "@/lib/runtime";
import type { SessionMeta } from "@/lib/types";

/** sessionStorage key shared by the search picks and TranscriptList. */
export const SCROLL_TO_MESSAGE_KEY = "ginno-scroll-to-message";

/** Remember the message to land on before switching sessions (the target
 *  transcript may still be loading when the click lands). */
export function rememberMessageTarget(messageId: string): void {
  try {
    sessionStorage.setItem(SCROLL_TO_MESSAGE_KEY, messageId);
  } catch {
    /* storage unavailable — the session still opens, just without anchoring */
  }
}

/** Read AND clear the pending target. Safe in private windows / no storage. */
export function consumeMessageTarget(): string | null {
  try {
    const id = sessionStorage.getItem(SCROLL_TO_MESSAGE_KEY);
    if (id) sessionStorage.removeItem(SCROLL_TO_MESSAGE_KEY);
    return id || null;
  } catch {
    return null;
  }
}

function selectorEscape(id: string): string {
  try {
    if (typeof CSS !== "undefined" && typeof CSS.escape === "function") return CSS.escape(id);
  } catch {
    /* fall through to the minimal escape */
  }
  return id.replace(/["\\]/g, "\\$&");
}

/** Scroll the row carrying data-msg-id=<messageId> into the middle of the
 *  viewport. Returns the element (or null when that message has no row — e.g.
 *  a search hit on an intermediate AI step, see TranscriptList's mapping
 *  note). Never throws. */
export function scrollToMessageAnchor(messageId: string): HTMLElement | null {
  try {
    const el = document.querySelector(`[data-msg-id="${selectorEscape(messageId)}"]`);
    if (!(el instanceof HTMLElement)) return null;
    el.scrollIntoView({ block: "center" });
    return el;
  } catch {
    return null;
  }
}

/** Temporary violet ring so the landed-on row is obvious (2s). */
export function flashMessageAnchor(el: HTMLElement | null): void {
  if (!el) return;
  try {
    el.classList.add("ring-2", "ring-violet/60", "rounded-xl");
    window.setTimeout(() => {
      try {
        // 只撤掉 ring；rounded-xl 留在元素上（assistant 行本来就有，用户行无
        // 背景色时不可见），避免影响原有圆角样式。
        el.classList.remove("ring-2", "ring-violet/60");
      } catch {
        /* detached */
      }
    }, 2000);
  } catch {
    /* ignore */
  }
}

/** Wrap every case-insensitive occurrence of `needle` in a <mark>. Falls back
 *  to the raw string when the needle is empty / absent (old index rows). */
export function highlightSnippet(text: string, needle: string): ReactNode {
  const q = needle.trim();
  if (!q) return text;
  const lower = text.toLowerCase();
  const ql = q.toLowerCase();
  const parts: ReactNode[] = [];
  let from = 0;
  let at = lower.indexOf(ql);
  let key = 0;
  while (at >= 0) {
    if (at > from) parts.push(text.slice(from, at));
    parts.push(
      <mark key={key++} className="rounded bg-yellow/20 px-0.5 text-txt">
        {text.slice(at, at + q.length)}
      </mark>,
    );
    from = at + q.length;
    at = lower.indexOf(ql, from);
  }
  if (!parts.length) return text;
  if (from < text.length) parts.push(text.slice(from));
  return parts;
}

// 服务端 search_index 存的是 LangChain 的 message.type："human" / "ai" / "tool"；
// 回放老数据也可能已经是 UI 角色名。统一映射成三档徽标。
export function RoleBadge({ role }: { role: string }) {
  const t = useTranslations("shell");
  const key =
    role === "user" || role === "human"
      ? "search.role.user"
      : role === "assistant" || role === "ai"
        ? "search.role.assistant"
        : "search.role.tool";
  return (
    <span className="shrink-0 rounded border border-line2 px-1 text-[10px] leading-4 text-faint">
      {t(key)}
    </span>
  );
}

/** Title hit row (⌘K list + archived-search list). Existing modal markup,
 *  extracted so both surfaces render identically. */
export function SessionHitRow({
  session,
  active = false,
  dense = false,
  onOpen,
  onMouseEnter,
}: {
  session: SessionMeta;
  active?: boolean;
  dense?: boolean;
  onOpen: () => void;
  onMouseEnter?: () => void;
}) {
  const g = useGinno();
  const t = useTranslations("shell");
  const rowAgent = g.agents.find((a) => a.id === session.agent_id) ?? null;
  const hex = agentHex(rowAgent?.color);
  return (
    <button
      onMouseEnter={onMouseEnter}
      onClick={onOpen}
      className={`flex w-full items-center text-left transition-colors ${
        dense ? "gap-1.5 px-2 py-1 text-[12px]" : "gap-2.5 px-3 py-2 text-sm"
      } ${active ? "bg-card2 text-txt" : "text-muted hover:bg-card2/60"}`}
    >
      <AgentIcon
        name={rowAgent?.icon}
        className={dense ? "h-3.5 w-3.5 shrink-0" : "h-4 w-4 shrink-0"}
        style={{ color: hex }}
      />
      <span className="min-w-0 flex-1 truncate">{session.title || t("session.untitled")}</span>
      <span className="shrink-0 text-[11px] text-faint">
        {relTime(session.updated ?? session.created)}
      </span>
    </button>
  );
}

/** Message-content hit row: session title (id[:8] fallback), role badge,
 *  highlighted snippet, optional archived chip. */
export function MessageHitRow({
  hit,
  needle,
  active = false,
  dense = false,
  onOpen,
  onMouseEnter,
}: {
  hit: SearchMessageHit;
  needle: string;
  active?: boolean;
  dense?: boolean;
  onOpen: () => void;
  onMouseEnter?: () => void;
}) {
  const t = useTranslations("shell");
  return (
    <button
      onMouseEnter={onMouseEnter}
      onClick={onOpen}
      className={`flex w-full flex-col items-start gap-0.5 text-left transition-colors ${
        dense ? "px-2 py-1" : "px-3 py-1.5"
      } ${active ? "bg-card2" : "hover:bg-card2/60"}`}
    >
      <span className="flex w-full items-center gap-1.5">
        <span
          className={`min-w-0 flex-1 truncate text-muted ${
            dense ? "text-[11px]" : "text-xs"
          }`}
        >
          {hit.session_title || hit.session_id.slice(0, 8)}
        </span>
        <RoleBadge role={hit.role} />
        {hit.archived && (
          <span className="shrink-0 rounded border border-line2 px-1 text-[9px] leading-4 text-faint">
            {t("search.archivedChip")}
          </span>
        )}
      </span>
      <span
        className={`line-clamp-2 leading-snug text-faint ${dense ? "text-[11px]" : "text-xs"}`}
      >
        {highlightSnippet(hit.snippet, needle)}
      </span>
    </button>
  );
}
