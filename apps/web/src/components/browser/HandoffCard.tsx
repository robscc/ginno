"use client";

import { Globe } from "lucide-react";

export function HandoffCard({
  space,
  url,
  reason,
  onGo,
  onReturn,
}: {
  space?: string;
  url?: string;
  reason?: string;
  onGo: () => void;
  onReturn: () => void;
}) {
  return (
    <div className="mx-auto w-full max-w-3xl px-6">
      <div className="mb-2 rounded-xl border border-yellow/50 bg-yellow/10 p-3">
        <div className="mb-1 flex items-center gap-1.5 text-sm font-medium text-yellow">
          <Globe className="h-3.5 w-3.5" />
          需要你在右侧画面里操作
          {space ? <span className="font-normal text-faint">· {space}</span> : null}
        </div>
        {reason ? <div className="mb-1 text-xs text-txt">{reason}</div> : null}
        {url ? <div className="mb-3 font-mono text-[11px] text-muted break-all">{url}</div> : <div className="mb-3" />}
        <div className="flex gap-2">
          <button
            onClick={onGo}
            className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90"
          >
            去浏览器
          </button>
          <button
            onClick={onReturn}
            className="rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:text-txt"
          >
            交还
          </button>
        </div>
      </div>
    </div>
  );
}
