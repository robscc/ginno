"use client";

// ConnStatusChip — composer 工具行的 WS 连接状态点（自 ChatStream.tsx 机械拆出，
// 渲染逐字一致）：非 live 时可点重连。

import { useTranslations } from "next-intl";

export function ConnStatusChip({
  wsStatus,
  onReconnect,
}: {
  wsStatus: "connecting" | "live" | "reconnecting" | "offline";
  onReconnect: () => void;
}) {
  const tc = useTranslations("chat");
  const live = wsStatus === "live";
  const dot =
    wsStatus === "live"
      ? "#22c55e"
      : wsStatus === "offline"
        ? "#ef4444"
        : "#eab308";
  const label =
    wsStatus === "live"
      ? tc("conn.connected")
      : wsStatus === "reconnecting"
        ? tc("conn.reconnecting")
        : wsStatus === "offline"
          ? tc("conn.offline")
          : tc("conn.connecting");
  const tip = live
    ? tc("conn.tipLive")
    : wsStatus === "reconnecting"
      ? tc("conn.tipReconnecting")
      : wsStatus === "offline"
        ? tc("conn.tipOffline")
        : tc("conn.tipConnecting");
  return (
    <button
      type="button"
      onClick={() => {
        if (!live) onReconnect();
      }}
      title={tip}
      aria-label={live ? tc("conn.ariaStatus", { status: label }) : tc("conn.ariaStatusClickable", { status: label })}
      className={`ml-1 flex shrink-0 items-center gap-1 whitespace-nowrap rounded-md px-1.5 py-0.5 text-[10px] transition-colors ${
        live ? "cursor-default" : "cursor-pointer hover:bg-card2"
      }`}
      style={{ color: dot }}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${
          live || wsStatus === "offline" ? "" : "animate-pulse"
        }`}
        style={{ background: dot }}
      />
      {label}
    </button>
  );
}
