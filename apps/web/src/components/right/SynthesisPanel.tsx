"use client";

import { useEffect, useState, type ReactNode } from "react";
import Link from "next/link";
import { Check, Sparkles, X } from "lucide-react";
import { useGinno } from "@/lib/store";
import type { SynthesisCaseSummary } from "@/lib/runtime";
import { SynthesisCaseDrawer } from "./SynthesisCaseDrawer";

/**
 * Right-panel 总结 tab: 「总结成流程」的 synthesis cases — realtime status for
 * the in-flight case (WS synthesis.event → reloadSynthesisCases; 1.5s poll as
 * fallback), plus full history with disk-locating synthesis_ids.
 */
export function SynthesisPanel() {
  const g = useGinno();
  const cases = g.synthesisCases;
  const active = cases.some((c) => !c.status && c.running);
  const [openCase, setOpenCase] = useState<string | null>(null);
  const reload = g.reloadSynthesisCases;

  // Fresh list on every tab visit (the list also reloads on synthesis.event WS).
  useEffect(() => {
    void reload();
  }, [reload]);

  // Fallback poll while a case is in flight (covers WS reconnect gaps; the
  // drawer has its own detail poll).
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => void reload(), 1500);
    return () => clearInterval(t);
  }, [active, reload]);

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center px-4 pb-2 pt-4">
        <Sparkles className="mr-2 h-4 w-4 text-muted" />
        <span className="text-sm font-semibold text-txt">总结记录</span>
        <span className="ml-2 rounded-full bg-card2 px-2 py-0.5 text-[11px] text-muted">{cases.length}</span>
        {active && (
          <span className="ml-auto flex items-center gap-1.5 text-[11px] text-blue">
            <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-blue" /> 总结中
          </span>
        )}
      </div>
      <div className="flex-1 space-y-1.5 overflow-y-auto px-3 pb-3">
        {cases.length === 0 && (
          <div className="px-1 py-6 text-center text-xs text-faint">
            暂无总结记录。在聊天里点「⌁ 总结成流程」后，每次总结会在这里实时显示进度与结果。
          </div>
        )}
        {cases.map((c) => (
          <CaseRow key={c.synthesis_id} c={c} onOpen={() => setOpenCase(c.synthesis_id)} />
        ))}
      </div>
      <div className="border-t border-line px-4 py-2 text-[11px] text-faint">
        <Link href="/settings/synthesis-quality" className="hover:text-txt">
          成功率与准确率漏斗 → 设置 · 总结质量
        </Link>
      </div>

      {openCase && (
        <SynthesisCaseDrawer
          synthesisId={openCase}
          onClose={() => setOpenCase(null)}
          onReplayed={() => void reload()}
        />
      )}
    </div>
  );
}

function CaseRow({ c, onOpen }: { c: SynthesisCaseSummary; onOpen: () => void }) {
  const when = c.ts
    ? new Date(c.ts * 1000).toLocaleString(undefined, {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "";
  let icon: ReactNode;
  let label: string;
  if (!c.status) {
    if (c.running) {
      icon = <span className="inline-block h-2.5 w-2.5 shrink-0 animate-pulse rounded-full bg-blue" />;
      label = "总结中…";
    } else {
      icon = <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-full bg-yellow/70" />;
      label = "未完成";
    }
  } else if (c.status !== "ok") {
    icon = <X className="h-3.5 w-3.5 shrink-0 text-red" />;
    label = c.fail_stage || "生成失败";
  } else {
    const runFailed = c.outcome?.first_run && c.outcome.first_run.status === "failed";
    const adopted = c.outcome?.created;
    icon = runFailed ? (
      <X className="h-3.5 w-3.5 shrink-0 text-red" />
    ) : (
      <Check className={`h-3.5 w-3.5 shrink-0 ${adopted ? "text-green" : "text-faint"}`} />
    );
    label = runFailed
      ? `首跑失败 @ ${c.outcome?.first_run?.failed_node || "?"}`
      : adopted
        ? "已采用"
        : "已生成";
  }
  return (
    <button
      onClick={onOpen}
      className="flex w-full flex-col gap-1 rounded-md border border-line bg-card px-3 py-2 text-left transition-colors hover:border-line2 hover:bg-card2/40"
    >
      <div className="flex w-full items-center gap-2">
        {icon}
        <span className="min-w-0 flex-1 truncate text-xs text-txt">{label}</span>
        {c.session_stats?.messages !== undefined && (
          <span className="shrink-0 text-[10px] text-faint">{c.session_stats.messages} 消息</span>
        )}
        <span className="shrink-0 text-[10px] text-faint">{when}</span>
      </div>
      {/* synthesis_id is the disk-locating entry: ~/.ginno/synthesis/<id> */}
      <span
        className="w-full truncate pl-[22px] font-mono text-[10px] text-faint"
        title={`~/.ginno/synthesis/${c.synthesis_id}`}
      >
        {c.synthesis_id}
      </span>
    </button>
  );
}
