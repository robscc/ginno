"use client";

/** Settings → 用量统计（usage-stats-design.md）。
 * 三个页签职责单一：概览回答「多少/何时」，会话回答「谁花的」，请求日志
 * 回答「每一次的细节」。时间控制不跨页签——概览用自己的统计窗口，会话/
 * 请求日志各有自己的过滤（评审决议）。 */

import { useState } from "react";
import { useTranslations } from "next-intl";
import { OverviewPanel } from "./usage/OverviewPanel";
import { SessionsPanel } from "./usage/SessionsPanel";
import { RequestsPanel } from "./usage/RequestsPanel";

type Tab = "overview" | "sessions" | "requests";

// label 存 catalog key（settings.usage.tabs.*），渲染时经 t() 翻译；
// label 为字面量联合，保证 next-intl 的 key 类型检查可用
const TABS: { id: Tab; label: "overview" | "sessions" | "requests" }[] = [
  { id: "overview", label: "overview" },
  { id: "sessions", label: "sessions" },
  { id: "requests", label: "requests" },
];

export function UsageSettings() {
  const t = useTranslations("settings.usage");
  const [tab, setTab] = useState<Tab>("overview");
  // Cross-tab jump: session row → request log filtered by that session.
  const [reqSession, setReqSession] = useState<string | undefined>(undefined);

  function openRequests(sessionId: string) {
    setReqSession(sessionId);
    setTab("requests");
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">{t("title")}</h2>
      <p className="mt-1 text-xs text-faint">{t("subtitle")}</p>

      <div className="mt-4 flex gap-1 border-b border-line" role="tablist">
        {TABS.map((tab2) => (
          <button
            key={tab2.id}
            role="tab"
            aria-selected={tab === tab2.id}
            onClick={() => setTab(tab2.id)}
            className={`-mb-px rounded-t-lg border px-4 py-2 text-[13px] transition-colors ${
              tab === tab2.id
                ? "border-line border-b-card bg-card text-txt"
                : "border-transparent text-muted hover:text-txt"
            }`}
          >
            {t(`tabs.${tab2.label}`)}
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
