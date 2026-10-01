"use client";

/* Connectors page (connector-module-design.md §2.2): one card per connector —
 * status badge, version line, actions (install wizard / config / enable).
 * Reachable from the sidebar entry ABOVE Knowledge Base. */

import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "@/lib/runtime";
import type { ConnectorInfo } from "@/lib/runtime";
import { InstallWizard } from "@/components/connectors/InstallWizard";
import { ConfigFold } from "@/components/connectors/ConfigFold";
import {
  Cable,
  CheckCircle2,
  CircleDashed,
  CircleOff,
  Globe,
  Loader2,
  Monitor,
  RefreshCw,
  TriangleAlert,
  XCircle,
} from "lucide-react";

const POLL_MS = 5000; // M1 polling fallback (设计 §5);事件通道接好后可替换

const STATUS_META: Record<
  string,
  { label: string; cls: string; Icon: typeof CheckCircle2 }
> = {
  connected: { label: "已连接", cls: "text-green-600 dark:text-green-400", Icon: CheckCircle2 },
  disconnected: { label: "已断开", cls: "text-yellow-600 dark:text-yellow-400", Icon: TriangleAlert },
  not_installed: { label: "未安装", cls: "text-faint", Icon: CircleDashed },
  installing: { label: "安装中", cls: "text-blue-600 dark:text-blue-400", Icon: Loader2 },
  error: { label: "错误", cls: "text-red-600 dark:text-red-400", Icon: XCircle },
  disabled: { label: "已禁用", cls: "text-faint", Icon: CircleOff },
};

function iconFor(c: ConnectorInfo) {
  if (c.icon === "globe") return Globe;
  if (c.icon === "monitor") return Monitor;
  return Cable;
}

function metaLine(c: ConnectorInfo): string {
  const bits: string[] = [];
  if (c.version) bits.push(`v${c.version}`);
  const extra = c.extra || {};
  if (typeof extra.browserType === "string") bits.push(String(extra.browserType));
  if (c.status === "connected" && c.statusDetail) bits.push(c.statusDetail);
  return bits.join(" · ");
}

export default function ConnectorsPage() {
  const [connectors, setConnectors] = useState<ConnectorInfo[]>([]);
  const [wizardFor, setWizardFor] = useState<string | null>(null);
  const [configFor, setConfigFor] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const [handoff, setHandoff] = useState<{ tabId: string }[]>([]);

  const refresh = useCallback(async () => {
    try {
      const r = await api.listConnectors();
      setConnectors(r.connectors || []);
    } catch {
      /* sidecar not up yet — keep last state */
    } finally {
      setLoaded(true);
    }
    try {
      const h = await api.getHandoffStatus();
      setHandoff(h.active || []);
    } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    refresh();
    timer.current = setInterval(refresh, POLL_MS);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [refresh]);

  // 向导打开时轮询加密,让 Step 5 的"等待连接"尽快打勾
  useEffect(() => {
    if (wizardFor && timer.current) {
      clearInterval(timer.current);
      timer.current = setInterval(refresh, 1000);
    } else if (!wizardFor) {
      if (timer.current) clearInterval(timer.current);
      timer.current = setInterval(refresh, POLL_MS);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wizardFor]);

  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
      <header className="mb-6 flex items-center gap-2">
        <Cable className="h-5 w-5 text-faint" />
        <h1 className="text-lg font-semibold text-txt">连接器</h1>
        <button
          onClick={refresh}
          title="刷新"
          className="ml-auto rounded-md p-1.5 text-faint hover:bg-card hover:text-txt"
        >
          <RefreshCw className="h-4 w-4" />
        </button>
      </header>

      {/* 接管横幅(设计 M3):browser_handoff 工具阻塞等待时在此释放 */}
      {handoff.length > 0 && (
        <div className="mb-4 flex items-center gap-3 rounded-xl border border-violet-500/40 bg-violet-500/10 p-4">
          <div className="min-w-0 flex-1 text-sm text-txt">
            <div className="font-medium">Agent 正在等待你接管浏览器</div>
            <div className="mt-0.5 text-xs text-faint">
              标签 {handoff.map((h) => `#${h.tabId}`).join(", ")} 交给你了——登录、
              验证码或付款确认完成后,点「已接管,继续」交回控制权。
            </div>
          </div>
          <button
            onClick={async () => {
              for (const h of handoff) {
                await api.connectorAction(
                  "chrome-extension",
                  "browser_handoff_release",
                  { tabId: h.tabId },
                );
              }
              refresh();
            }}
            className="shrink-0 rounded-lg bg-violet-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-violet-500"
          >
            已接管,继续
          </button>
        </div>
      )}

      {!loaded ? (
        <div className="flex items-center gap-2 py-10 text-sm text-faint">
          <Loader2 className="h-4 w-4 animate-spin" /> 正在读取连接器状态…
        </div>
      ) : connectors.length === 0 ? (
        <div className="py-10 text-sm text-faint">还没有已注册的连接器。</div>
      ) : (
        <div className="space-y-3">
          {connectors.map((c) => {
            const meta = STATUS_META[c.status] || STATUS_META.not_installed;
            const Icon = iconFor(c);
            const isActive = c.status !== "disabled";
            return (
              <div
                key={c.id}
                className="rounded-xl border border-line bg-card p-4"
              >
                <div className="flex items-start gap-3">
                  <Icon className="mt-0.5 h-5 w-5 shrink-0 text-faint" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-sm font-medium text-txt">{c.name}</span>
                      <span className={`inline-flex items-center gap-1 text-xs ${meta.cls}`}>
                        <meta.Icon
                          className={`h-3.5 w-3.5 ${c.status === "installing" ? "animate-spin" : ""}`}
                        />
                        {meta.label}
                      </span>
                    </div>
                    {metaLine(c) && (
                      <div className="mt-0.5 truncate text-xs text-faint">{metaLine(c)}</div>
                    )}
                    <p className="mt-1.5 text-xs leading-relaxed text-faint">{c.description}</p>
                  </div>
                </div>

                <div className="mt-3 flex flex-wrap items-center gap-2">
                  {c.id === "chrome-extension" && !c.enabled ? null : (
                    <>
                      {(c.status === "not_installed" || c.status === "disconnected" || c.status === "error") &&
                        (c.installSteps?.length ? (
                          <button
                            onClick={() => setWizardFor(c.id)}
                            className="rounded-lg bg-violet-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-violet-500"
                          >
                            安装指引
                          </button>
                        ) : null)}
                      {c.id === "browser-profile" && (
                        <button
                          onClick={async () => {
                            await api.connectorAction(c.id, c.status === "connected" ? "profile_stop" : "profile_start");
                            refresh();
                          }}
                          className="rounded-lg border border-line px-3 py-1.5 text-xs text-txt hover:bg-hover"
                        >
                          {c.status === "connected" ? "停止实例" : "启动实例"}
                        </button>
                      )}
                      <button
                        onClick={() => setConfigFor(configFor === c.id ? null : c.id)}
                        className="rounded-lg border border-line px-3 py-1.5 text-xs text-txt hover:bg-hover"
                      >
                        配置
                      </button>
                      <button
                        onClick={async () => {
                          await api.patchConnectorConfig(c.id, { enabled: !c.enabled });
                          refresh();
                        }}
                        className="rounded-lg border border-line px-3 py-1.5 text-xs text-faint hover:bg-hover"
                      >
                        {isActive ? "禁用" : "启用"}
                      </button>
                    </>
                  )}
                </div>

                {configFor === c.id && (
                  <ConfigFold connector={c} onSaved={refresh} />
                )}
              </div>
            );
          })}
        </div>
      )}

      {wizardFor && (
        <InstallWizard
          connectorId={wizardFor}
          onClose={() => setWizardFor(null)}
        />
      )}
    </div>
  );
}
