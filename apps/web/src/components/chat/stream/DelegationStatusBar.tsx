"use client";

// DelegationStatusBar — 委托回放会话 composer 槽位的状态栏（自 ChatStream.tsx
// 机械拆出，渲染逐字一致）：正确反馈 backend 运行态（用户需求 2026-10-05）。

import { Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import type { SessionMeta } from "@/lib/types";

export function DelegationStatusBar({ session }: { session: SessionMeta }) {
  const tc = useTranslations("chat");
  const st = session.stop_reason ?? "running";
  const elapsed = Math.max(0, Math.round(Date.now() / 1000 - (session.created ?? Date.now() / 1000)));
  const mm = `${Math.floor(elapsed / 60)}m${String(elapsed % 60).padStart(2, "0")}s`;
  const color = st === "running" ? "#a78bfa" : st === "success" ? "#4ade80" : "#fb7185";
  return (
    <div className="flex items-center justify-center gap-2 rounded-2xl border border-line bg-card px-3 py-3 text-xs">
      {st === "running" ? (
        <Loader2 className="h-3.5 w-3.5 animate-spin" style={{ color }} />
      ) : (
        <span style={{ color }}>●</span>
      )}
      <span className="font-mono text-txt">{session.backend ?? "delegation"}</span>
      <span className="text-faint">· {session.mode ?? ""}</span>
      <span style={{ color }}>
        {st === "running" ? `${tc("composer.delegationRunning")} · ${mm}` : st}
      </span>
      <span className="text-faint">· read-only</span>
      {st === "running" && (
        <button
          onClick={() =>
            import("@/lib/runtime").then((m) => m.stopDelegation(session.id))
          }
          className="ml-1 rounded-md border border-red/40 px-1.5 py-0.5 text-[10px] text-red hover:bg-red/10"
        >
          ✕ stop
        </button>
      )}
    </div>
  );
}
