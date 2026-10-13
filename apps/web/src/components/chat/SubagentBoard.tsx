"use client";

// SubagentBoard — 顶部常驻委派板（PlanBoard 同款模式，2026-10-13 用户定稿）。
//
// 原先转录流里的「已委派 N 个子代理」组卡（SubagentGroupCard）是 live-only
// （不进历史持久化，重载即消失），且在清单模式下与 PlanBoard 重复展示同一
// 批子代理。处置：委派状态整体上收为本顶部板——
// * 数据与侧栏子会话行同源（sessions + subagent.spawned/status 事件 upsert），
//   重载不丢（相对组卡的净收益）；
// * 清单条目的子代理（child_session_id 被任一 plan item 引用）从本板排除——
//   PlanBoard 已按条目展示并带跳转，不再双报；
// * 转录流里的 subagent_spawn 卡片随之停渲染（结果卡/简报卡保留，职责不同）。
// 三档分节 + 鸭子状态点沿用组卡收纳方案（运行中/失败默认展开，成功默认收起）。

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import {
  SUBAGENT_BUCKET_ORDER,
  subagentSessionBucket,
  subagentStatusMeta,
  useStatusLabel,
} from "@/components/chat/blocks/types";
import { DuckStatusIcon } from "@/components/chat/blocks/duckStatus";

function subagentElapsed(sinceSec: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - sinceSec));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`;
}

export function SubagentBoard({ sessionId }: { sessionId: string }) {
  const g = useGinno();
  const router = useRouter();
  const t = useTranslations("chat");
  const statusLabel = useStatusLabel();
  // 收起以板为单位（全终态默认收起，有活动子代理默认展开）。
  const [collapsed, setCollapsed] = useState<boolean | null>(null);
  const [bucketOpen, setBucketOpen] = useState<Record<string, boolean>>({});

  // 清单条目引用的子会话：PlanBoard 的展示面，本板排除。
  const planChildren = useMemo(() => {
    const ids = new Set<string>();
    for (const p of g.plansBySession[sessionId] ?? []) {
      for (const it of p.items) {
        if (it.child_session_id) ids.add(it.child_session_id);
      }
    }
    return ids;
  }, [g.plansBySession, sessionId]);

  const children = useMemo(
    () =>
      g.sessions
        .filter(
          (s) =>
            (s.type === "subagent" || s.type === "delegation") &&
            s.parent_session_id === sessionId &&
            !planChildren.has(s.id),
        )
        .sort((a, b) => (b.created ?? 0) - (a.created ?? 0)),
    [g.sessions, sessionId, planChildren],
  );
  if (children.length === 0) return null;

  const bucketOf = (s: (typeof children)[number]) => subagentSessionBucket(s);
  const buckets = SUBAGENT_BUCKET_ORDER.map((b) => ({
    bucket: b,
    entries: children.filter((c) => bucketOf(c) === b),
  })).filter((b) => b.entries.length > 0);
  const anyActive = buckets.some((b) => b.bucket !== "done" && b.entries.length > 0);
  const fold = collapsed ?? !anyActive;
  const bucketOpenOf = (b: string) => bucketOpen[b] ?? b !== "done";

  const openChild = (sid: string) => {
    g.setActiveSession(sid);
    if (window.location.pathname !== "/") router.push("/");
    window.dispatchEvent(new CustomEvent("ginno:focus-latest", { detail: sid }));
  };

  return (
    <div className="mx-auto mb-2 w-full max-w-3xl rounded-lg border border-line bg-card/60 px-3 py-2 text-xs">
      <div className="flex items-center gap-1.5">
        <button
          onClick={() => setCollapsed(!fold)}
          className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
          title={fold ? t("planBoard.expand") : t("planBoard.collapse")}
        >
          {fold ? (
            <ChevronRight className="h-3.5 w-3.5 shrink-0 text-faint" />
          ) : (
            <ChevronDown className="h-3.5 w-3.5 shrink-0 text-faint" />
          )}
          <span className="shrink-0">🧭</span>
          <span className="min-w-0 flex-1 truncate text-left font-medium text-txt">
            {t("subagent.delegated", { count: children.length })}
          </span>
        </button>
        <span className="shrink-0 text-faint">
          {buckets.map((b) => `${statusLabel(b.bucket)} ${b.entries.length}`).join(" · ")}
        </span>
      </div>

      {!fold && (
        <div className="mt-1.5 flex flex-col">
          {buckets.map(({ bucket, entries }) => {
            const open = bucketOpenOf(bucket);
            return (
              <div key={bucket} className="border-t border-line/40 first:border-t-0">
                <button
                  onClick={() => setBucketOpen((m) => ({ ...m, [bucket]: !open }))}
                  className="flex w-full items-center gap-1.5 py-1 text-[10px] text-faint transition-colors hover:text-muted"
                  title={statusLabel(bucket)}
                >
                  <ChevronRight
                    className={`h-3 w-3 shrink-0 transition-transform ${open ? "rotate-90" : ""}`}
                  />
                  <DuckStatusIcon status={bucket} />
                  <span>{statusLabel(bucket)}</span>
                  <span>· {entries.length}</span>
                </button>
                {open && (
                  <div className="flex flex-col">
                    {entries.map((c) => {
                      const sub = c.subagent;
                      const status =
                        c.type === "delegation"
                          ? c.stop_reason === "error" || c.stop_reason === "timeout"
                            ? "failed"
                            : c.stop_reason
                              ? "done"
                              : "running"
                          : sub?.status ?? "running";
                      const meta = subagentStatusMeta(status);
                      const active = status === "running" || status === "waiting";
                      const type = String(
                        (sub as { agent_type?: string } | undefined)?.agent_type ?? "",
                      ).trim();
                      const title =
                        sub?.goal || c.title || t("subagent.fallbackTitle");
                      return (
                        <button
                          key={c.id}
                          onClick={() => openChild(c.id)}
                          className={`flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left transition-colors hover:bg-card2/40 ${
                            active ? "" : "opacity-60"
                          }`}
                          title={title}
                        >
                          <span
                            className="shrink-0 text-[11px] leading-none"
                            title={statusLabel(status)}
                          >
                            {meta.glyph}
                          </span>
                          {type && (
                            <span className="shrink-0 rounded-full border border-violet/40 bg-violet/10 px-1.5 text-[10px] leading-4 text-violet">
                              {type}
                            </span>
                          )}
                          {c.type === "delegation" && (
                            <span className="shrink-0 rounded bg-amber/10 px-1 py-px text-[10px] text-amber">
                              {c.backend || "delegation"}
                            </span>
                          )}
                          <span className="min-w-0 flex-1 truncate text-left text-muted">{title}</span>
                          {active && c.created && (
                            <span className="shrink-0 text-[10px] text-faint">
                              {t("subagent.runningFor", {
                                time: subagentElapsed(c.created),
                              })}
                            </span>
                          )}
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
