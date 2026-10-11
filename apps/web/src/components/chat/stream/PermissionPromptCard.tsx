"use client";

// PermissionPromptCard — 权限请求横幅（自 ChatStream.tsx 机械拆出，渲染逐字一致）。

import { useTranslations } from "next-intl";
import type { PermissionPrompt } from "../streamCore";

export function PermissionPromptCard({
  permission,
  onRespond,
}: {
  permission: PermissionPrompt;
  onRespond: (decision: "allow" | "deny") => void;
}) {
  const tc = useTranslations("chat");
  return (
    <div className="mx-auto w-full max-w-3xl px-6">
      <div className="mb-2 rounded-xl border border-yellow/40 bg-yellow/10 p-3">
        <div className="mb-1 text-sm font-medium text-yellow">{tc("permission.title")}</div>
        <div className="mb-2 text-xs text-muted">
          {tc("permission.toolLabel")} <code className="font-mono text-txt">{permission.tool}</code>
        </div>
        <pre className="mb-3 max-h-28 overflow-auto rounded-lg bg-base/60 p-2 text-[11px] text-muted">
          {JSON.stringify(permission.args, null, 2)}
        </pre>
        <div className="flex gap-2">
          <button
            onClick={() => onRespond("allow")}
            className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90"
          >
            {tc("permission.allow")}
          </button>
          <button
            onClick={() => onRespond("deny")}
            className="rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:text-txt"
          >
            {tc("permission.deny")}
          </button>
        </div>
      </div>
    </div>
  );
}
