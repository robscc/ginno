"use client";

/** Settings → 用量统计（usage-stats-design.md）。
 * 三个页签职责单一：概览回答「多少/何时」，会话回答「谁花的」，请求日志
 * 回答「每一次的细节」。时间控制不跨页签——概览用自己的统计窗口，会话/
 * 请求日志各有自己的过滤（评审决议）。 */

import { useState } from "react";
import { OverviewPanel } from "./usage/OverviewPanel";
import { SessionsPanel } from "./usage/SessionsPanel";
import { RequestsPanel } from "./usage/RequestsPanel";

type Tab = "overview" | "sessions" | "requests";

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "概览" },
  { id: "sessions", label: "会话" },
  { id: "requests", label: "请求日志" },
];

export function UsageSettings() {
  const [tab, setTab] = useState<Tab>("overview");
  // Cross-tab jump: session row → request log filtered by that session.
  const [reqSession, setReqSession] = useState<string | undefined>(undefined);

  function openRequests(sessionId: string) {
    setReqSession(sessionId);
    setTab("requests");
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">用量统计</h2>
      <p className="mt-1 text-xs text-faint">全局 Token 用量 · 缓存命中 · 请求审计（纯本地记录，默认保留 90 天）</p>

      <div className="mt-4 flex gap-1 border-b border-line" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => setTab(t.id)}
            className={`-mb-px rounded-t-lg border px-4 py-2 text-[13px] transition-colors ${
              tab === t.id
                ? "border-line border-b-card bg-card text-txt"
                : "border-transparent text-muted hover:text-txt"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="mt-4">
        {tab === "overview" && <OverviewPanel />}
        {tab === "sessions" && <SessionsPanel onOpenRequests={openRequests} />}
        {tab === "requests" && (
          <RequestsPanel sessionFilter={reqSession} onClearSession={() => setReqSession(undefined)} />
        )}
      </div>
    </div>
  );
}
