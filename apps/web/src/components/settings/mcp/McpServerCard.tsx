"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import type { McpServerStatus } from "@/lib/runtime";
import { McpToggle } from "./McpToggle";
import { serverLine, transportBadgeClass, transportOf, type McpServerEntry } from "./mcpShared";

export type McpCardAction = "tools" | "perms" | "reconnect" | "remove";

// 服务器卡片（原型 v2 .card）：状态点 / 名称 / transport 徽章 / 单行摘要或错误 /
// 工具数 / 启停开关 / ⋮ 菜单。整卡左侧点击进详情。
// 开关值由父组件下发（switchOn = pending 态覆盖轮询态，见 McpSettings），
// 卡片自身不做乐观翻转——确认驱动，pending 期间控件禁用转 spinner。
export function McpServerCard({
  s,
  entry,
  switchOn,
  switchPending,
  restarting,
  onOpen,
  onAction,
  onToggle,
}: {
  s: McpServerStatus;
  entry: McpServerEntry;
  switchOn: boolean;
  switchPending: boolean;
  restarting: boolean;
  onOpen: () => void;
  onAction: (kind: McpCardAction) => void;
  onToggle: (on: boolean) => void;
}) {
  const t = useTranslations("settings.mcp");
  const [menu, setMenu] = useState(false);
  const transport = transportOf(entry);
  const err = !s.connected ? (s.error || t("notConnected")) : null;
  const onCount = Math.max(0, s.tools - (s.disabledTools?.length ?? 0));
  return (
    <div className="relative flex items-center gap-3 rounded-lg border border-line bg-card px-3.5 py-3 transition-colors hover:border-line2">
      {/* 左侧主体：点击进详情 */}
      <button type="button" onClick={onOpen} className="flex min-w-0 flex-1 items-center gap-2.5 text-left">
        {/* 状态点：绿=已连接 红=失败 黄=重连中（脉冲） */}
        <span
          className={`h-1.5 w-1.5 shrink-0 rounded-full ${
            restarting
              ? "animate-pulse bg-yellow"
              : s.connected
                ? "bg-green"
                : "bg-red"
          }`}
        />
        <span className="min-w-0">
          <span className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold text-txt">{s.name}</span>
            <span
              className={`rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase leading-none tracking-wider ${transportBadgeClass(transport)}`}
            >
              {transport}
            </span>
          </span>
          <span className="mt-0.5 block max-w-[440px] truncate font-mono text-xs text-faint">
            {serverLine(entry) || "—"}
          </span>
          {err && (
            <span className="mt-0.5 block truncate text-xs text-red" title={err}>
              {err}
              {s.tools > 0 && !s.connected ? ` · ${t("toolsCached", { count: s.tools })}` : ""}
            </span>
          )}
        </span>
      </button>

      {/* 右侧：工具数 + 开关 + 菜单 */}
      <div className="relative flex shrink-0 items-center gap-2.5">
        <span className="min-w-16 text-right text-xs text-muted">
          {s.connected || s.tools > 0
            ? t("toolsOn", { on: onCount, total: s.tools })
            : "—"}
        </span>
        <McpToggle checked={switchOn} pending={switchPending} onChange={onToggle} label={s.name} />
        <button
          type="button"
          aria-label={s.name}
          onClick={() => setMenu((v) => !v)}
          className="flex h-7 w-7 items-center justify-center rounded-lg text-faint transition-colors hover:bg-card2 hover:text-txt"
        >
          ⋮
        </button>
        {menu && (
          <>
            {/* 透明遮罩：点击任意处收起菜单 */}
            <div className="fixed inset-0 z-30" onClick={() => setMenu(false)} />
            <div className="absolute right-0 top-8 z-40 min-w-44 rounded-lg border border-line2 bg-card2 p-1 shadow-xl">
              <MenuItem onClick={() => { setMenu(false); onOpen(); }} label={t("menuEdit")} />
              <MenuItem onClick={() => { setMenu(false); onAction("tools"); }} label={t("menuTools")} />
              <MenuItem onClick={() => { setMenu(false); onAction("perms"); }} label={t("menuPerms")} />
              <MenuItem onClick={() => { setMenu(false); onAction("reconnect"); }} label={t("menuReconnect")} />
              <hr className="mx-1 my-1 border-line" />
              <MenuItem
                danger
                onClick={() => { setMenu(false); onAction("remove"); }}
                label={t("menuRemove")}
              />
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function MenuItem({ label, onClick, danger }: { label: string; onClick: () => void; danger?: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-[13px] transition-colors hover:bg-card ${
        danger ? "text-red" : "text-txt"
      }`}
    >
      {label}
    </button>
  );
}
