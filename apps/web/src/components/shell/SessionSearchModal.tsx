"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Search } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { SearchMessageHit } from "@/lib/runtime";
import type { SessionMeta } from "@/lib/types";
import { hiddenSessionIds } from "@/lib/sessionVisibility";
import {
  MessageHitRow,
  SessionHitRow,
  rememberMessageTarget,
} from "@/components/shell/searchShared";

/** ⌘K search (V2 P0-1): non-empty queries hit the unified /api/search
 *  endpoint (200ms debounce) and render TWO groups — session-title hits and
 *  message-content snippets. An empty query keeps the local recent list, and
 *  endpoint failure falls back to the old in-memory title ∪ agent-name filter.
 *  ↑↓/Enter/click opens the session; a message hit also anchors its row in the
 *  transcript (see TranscriptList). */
const MESSAGE_ROWS = 8;

type SearchAll = {
  sessions: SessionMeta[];
  messages: SearchMessageHit[];
  indexed: number;
  indexing: boolean;
};

type Hit = { kind: "session"; session: SessionMeta } | { kind: "message"; hit: SearchMessageHit };

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
  // 服务端统一搜索结果；null = 没有可用结果（空查询/请求中/失败）。
  const [server, setServer] = useState<SearchAll | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [showAllMessages, setShowAllMessages] = useState(false);
  const needle = q.trim();

  // 非空查询走防抖 200ms 的统一搜索；每次查询变化先清掉上一轮结果，避免在
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
        const r = await api.searchAll(needle, { limit: 20 });
        if (!alive) return;
        setServer({
          sessions: r.sessions ?? [],
          messages: r.messages ?? [],
          indexed: r.index?.indexed ?? 0,
          indexing: !!r.index?.indexing,
        });
        setFailed(false);
      } catch {
        if (!alive) return;
        setFailed(true); // 端点不可用 → 退回本地过滤（见 titleHits）
      } finally {
        if (alive) setLoading(false);
      }
    }, 200);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [needle]);

  // 影子/团队空间会话整类不进搜索（scheduled-tasks-design §3.6/§10 决议 5），
  // 其子树（定时任务拉起的 subagent 等）随父级一并排除（sessionVisibility）。
  const hidden = useMemo(() => hiddenSessionIds(g.sessions), [g.sessions]);

  // 空查询的「最近会话」列表按 P1-1 的归档语义收口：archived 且 7 天无活动
  // 的不再占位；但搜索本身仍能命中它们（归档 = 藏起来，不是丢掉）。
  const recent = useMemo(() => {
    const now = Math.floor(Date.now() / 1000);
    return [...g.sessions]
      .filter((s) => !hidden.has(s.id))
      .sort((a, b) => (b.updated ?? 0) - (a.updated ?? 0))
      .filter((s) => !(s.archived === true && now - (s.updated ?? 0) > 7 * 86400));
  }, [g.sessions, hidden]);

  // 非空查询的标题命中：服务端 /api/search 的 sessions；失败时退回本地
  // 标题 ∪ Agent 名过滤（C+ 方案⑤，与旧行为一致）。
  const titleHits = useMemo<SessionMeta[]>(() => {
    if (!needle) return [];
    if (server) return server.sessions.filter((s) => !hidden.has(s.id));
    if (!failed) return []; // 首次请求在飞
    const agentName = (s: SessionMeta) =>
      (g.agents.find((a) => a.id === s.agent_id)?.name || "").toLowerCase();
    const ql = needle.toLowerCase();
    return [...g.sessions]
      .filter((s) => !hidden.has(s.id))
      .sort((a, b) => (b.updated ?? 0) - (a.updated ?? 0))
      .filter(
        (s) =>
          (s.title || "").toLowerCase().includes(ql) || agentName(s).includes(ql),
      );
  }, [needle, server, failed, hidden, g.sessions, g.agents]);

  const messageHits = useMemo<SearchMessageHit[]>(
    () => (server ? server.messages.filter((m) => !hidden.has(m.session_id)) : []),
    [server, hidden],
  );
  const visibleMessages = showAllMessages ? messageHits : messageHits.slice(0, MESSAGE_ROWS);

  const items = useMemo<Hit[]>(() => {
    if (!needle) return recent.map((session) => ({ kind: "session", session }));
    return [
      ...titleHits.map((session): Hit => ({ kind: "session", session })),
      ...visibleMessages.map((hit): Hit => ({ kind: "message", hit })),
    ];
  }, [needle, recent, titleHits, visibleMessages]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);
  useEffect(() => {
    setActive(0);
    setShowAllMessages(false);
  }, [q, server]);
  // 结果集变短（关键词变化/收起更多）时把高亮行夹回范围内。
  useEffect(() => {
    setActive((a) => Math.min(a, Math.max(0, items.length - 1)));
  }, [items.length]);

  const pickSession = (sid: string) => {
    onOpen(sid);
    onClose();
  };
  // 消息命中：先记住锚点再打开会话。若目标就是当前会话，直接广播滚动事件
  // （会话切换 effect 不会重跑，TranscriptList 收到事件后自己定位）。
  const pickMessage = (hit: SearchMessageHit) => {
    if (hit.session_id === g.activeSessionId) {
      window.dispatchEvent(new CustomEvent("ginno:scroll-to-message", { detail: hit.message_id }));
    } else {
      rememberMessageTarget(hit.message_id);
    }
    onOpen(hit.session_id);
    onClose();
  };
  const pickItem = (item: Hit | undefined) => {
    if (!item) return;
    if (item.kind === "session") pickSession(item.session.id);
    else pickMessage(item.hit);
  };

  // 只在请求落定（成功或已退回本地）后显示「无结果」，避免首帧闪空态。
  // 索引仍在建立时不算「无结果」——消息片段可能稍后出现，标题命中照常展示。
  const noHits =
    !!needle &&
    !loading &&
    (!!server || failed) &&
    !server?.indexing &&
    titleHits.length === 0 &&
    messageHits.length === 0;

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
                setActive((a) => Math.min(a + 1, Math.max(0, items.length - 1)));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setActive((a) => Math.max(a - 1, 0));
              } else if (e.key === "Enter" && items[active]) {
                e.preventDefault();
                pickItem(items[active]);
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
          {loading && (
            <div className="px-3 py-6 text-center text-xs text-faint">{tr("search.loading")}</div>
          )}
          {noHits && (
            <div className="px-3 py-6 text-center text-xs text-faint">{tr("search.empty")}</div>
          )}

          {/* 空查询：本地「最近会话」列表（原行为） */}
          {!needle &&
            recent.map((s, i) => (
              <SessionHitRow
                key={s.id}
                session={s}
                active={i === active}
                onMouseEnter={() => setActive(i)}
                onOpen={() => pickSession(s.id)}
              />
            ))}

          {/* 非空查询：两组结果 */}
          {needle && !loading && !noHits && (
            <>
              <div className="px-3 pb-0.5 pt-1.5 text-[10px] font-medium text-faint">
                {tr("search.sections.sessions")}
              </div>
              {titleHits.length === 0 ? (
                <div className="px-3 py-1 text-[11px] text-faint">{tr("search.noSessionHits")}</div>
              ) : (
                titleHits.map((s, i) => (
                  <SessionHitRow
                    key={s.id}
                    session={s}
                    active={i === active}
                    onMouseEnter={() => setActive(i)}
                    onOpen={() => pickSession(s.id)}
                  />
                ))
              )}

              <div className="px-3 pb-0.5 pt-2 text-[10px] font-medium text-faint">
                {tr("search.sections.messages")}
              </div>
              {visibleMessages.map((hit, i) => {
                const flatIndex = titleHits.length + i;
                return (
                  <MessageHitRow
                    key={`${hit.session_id}:${hit.message_id}:${i}`}
                    hit={hit}
                    needle={needle}
                    active={flatIndex === active}
                    onMouseEnter={() => setActive(flatIndex)}
                    onOpen={() => pickMessage(hit)}
                  />
                );
              })}
              {messageHits.length > MESSAGE_ROWS && (
                <button
                  onClick={() => setShowAllMessages((v) => !v)}
                  className="flex w-full items-center justify-between px-3 py-1.5 text-left text-[11px] text-faint transition-colors hover:bg-card2/40 hover:text-muted"
                >
                  <span>
                    {showAllMessages
                      ? tr("search.collapseMessages")
                      : tr("search.moreMessages", {
                          count: messageHits.length - MESSAGE_ROWS,
                          indexed: server?.indexed ?? 0,
                        })}
                  </span>
                  {showAllMessages && (
                    <span>{tr("search.indexedSessions", { indexed: server?.indexed ?? 0 })}</span>
                  )}
                </button>
              )}
              {/* 没有「更多」可看时仍给出索引规模，便于判断消息检索是否就绪 */}
              {messageHits.length > 0 && messageHits.length <= MESSAGE_ROWS && server && (
                <div className="px-3 py-1 text-[10px] text-faint">
                  {tr("search.indexedSessions", { indexed: server.indexed })}
                </div>
              )}
              {/* 索引建立中且尚无消息命中：标题命中照常可用，这里只做说明 */}
              {server?.indexing && messageHits.length === 0 && (
                <div className="px-3 pb-1 pt-0.5 text-[10px] text-faint">
                  {tr("search.indexing")}
                </div>
              )}
              {!loading && !server?.indexing && messageHits.length === 0 && (
                <div className="px-3 py-1 text-[11px] text-faint">{tr("search.noMessageHits")}</div>
              )}
            </>
          )}
        </div>
        <div className="flex items-center justify-between gap-2 border-t border-line px-3 py-1.5 text-[10px] text-faint">
          <span className="truncate">{tr("search.footer")}</span>
          {needle && (
            <span className="shrink-0">
              {loading
                ? tr("search.loading")
                : server
                  ? tr("search.results", {
                      sessions: titleHits.length,
                      messages: messageHits.length,
                    })
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
