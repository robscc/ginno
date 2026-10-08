"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import type { McpServerStatus } from "@/lib/runtime";

// 状态行小圆点：绿=已连接，红=连接失败；error 来自后端的单行根因压缩
function StatusRow({ s }: { s: McpServerStatus }) {
  const t = useTranslations("settings.mcp");
  return (
    <div className="flex items-center gap-2 py-1">
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${s.connected ? "bg-green" : "bg-red"}`} />
      <span className="truncate text-sm text-txt">{s.name}</span>
      {s.connected ? (
        <span className="ml-auto shrink-0 text-xs text-faint">{t("toolsCount", { count: s.tools })}</span>
      ) : (
        <span className="ml-auto min-w-0 truncate text-xs text-red" title={s.error || undefined}>
          {s.error || t("notConnected")}
        </span>
      )}
    </div>
  );
}

export function McpSettings() {
  const t = useTranslations("settings.mcp");
  const [cfg, setCfg] = useState<string>("");
  const [info, setInfo] = useState<{ servers: string[]; tools: string[]; status?: McpServerStatus[] }>({
    servers: [],
    tools: [],
  });
  const [msg, setMsg] = useState("");
  const [reconnecting, setReconnecting] = useState(false);

  const load = async () => {
    try {
      const c = await api.getMcpConfig();
      setCfg(JSON.stringify(c, null, 2));
      const i = await api.getMcp();
      setInfo(i);
    } catch {
      /* ignore */
    }
  };
  useEffect(() => {
    load();
    // 轻轮询：连接是启动后台建立的，且 GET /api/mcp 内置惰性重试（带冷却），
    // 轮询让状态点在重试成功后自动翻绿，无需手动刷新。
    const iv = setInterval(load, 5000);
    return () => clearInterval(iv);
  }, []);

  async function save() {
    try {
      const data = JSON.parse(cfg);
      await api.putMcp(data);
      const r = await api.reloadMcp();
      setMsg(t("saved", { servers: r.servers.join(", ") }));
      load();
    } catch (e) {
      setMsg(t("invalidJson", { error: (e as Error).message }));
    }
  }

  // 只重试未连上的，不重建 registry（保存走 Save & Reload 的全量 reload）
  async function reconnect() {
    setReconnecting(true);
    try {
      const r = await api.reconnectMcp();
      if (r.status) setInfo((prev) => ({ ...prev, status: r.status }));
    } catch {
      /* ignore */
    } finally {
      setReconnecting(false);
    }
  }

  const status = info.status ?? [];
  const anyDisconnected = status.some((s) => !s.connected);

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">{t("title")}</h2>
      <p className="mt-1 text-sm text-muted">
        {t("description", { servers: info.servers.length, tools: info.tools.length })}
      </p>
      {status.length > 0 && (
        <div className="mt-3 rounded-lg border border-line px-3 py-1.5">
          {status.map((s) => (
            <StatusRow key={s.name} s={s} />
          ))}
        </div>
      )}
      <textarea
        className="field mt-4 font-mono text-xs"
        rows={14}
        value={cfg}
        onChange={(e) => setCfg(e.target.value)}
      />
      <div className="mt-2 flex items-center gap-3">
        <button onClick={save} className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white">
          {t("saveReload")}
        </button>
        {anyDisconnected && (
          <button
            onClick={reconnect}
            disabled={reconnecting}
            className="rounded-lg border border-line px-3 py-1.5 text-xs font-medium text-muted hover:text-txt disabled:opacity-50"
          >
            {reconnecting ? t("reconnecting") : t("reconnect")}
          </button>
        )}
        {msg && <span className="text-xs text-muted">{msg}</span>}
      </div>
    </div>
  );
}
