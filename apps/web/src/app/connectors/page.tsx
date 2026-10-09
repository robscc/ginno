"use client";

/* Connectors page (connector-module-design.md §2.2): one card per connector —
 * status badge, version line, actions (install wizard / config / enable).
 * Reachable from the sidebar entry ABOVE Knowledge Base. */

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { t as llmT } from "@/i18n/provider";
import * as api from "@/lib/runtime";
import type { ConnectorInfo } from "@/lib/runtime";
import { InstallWizard } from "@/components/connectors/InstallWizard";
import { ConfigFold } from "@/components/connectors/ConfigFold";
import {
  Cable,
  CheckCircle2,
  CircleDashed,
  CircleOff,
  ExternalLink,
  Globe,
  Loader2,
  Monitor,
  RefreshCw,
  Send,
  TriangleAlert,
  XCircle,
} from "lucide-react";
import { useRouter } from "next/navigation";
import type { ConnectorEvent, PushedPage } from "@/lib/runtime";

const POLL_MS = 5000; // M1 polling fallback (设计 §5);事件通道接好后可替换

// 状态徽标：文案迁 conn 域 catalog（status.<statusKey>），这里只留样式/图标；
// statusKey 收成字面量联合，模板串 `status.${statusKey}` 才能通过 next-intl 的
// key 类型检查。未知状态回退到 not_installed 的展示。
type ConnStatusKey =
  | "connected"
  | "disconnected"
  | "not_installed"
  | "installing"
  | "error"
  | "disabled";

const STATUS_META: Record<
  string,
  { statusKey: ConnStatusKey; cls: string; Icon: typeof CheckCircle2 }
> = {
  connected: { statusKey: "connected", cls: "text-green-600 dark:text-green-400", Icon: CheckCircle2 },
  disconnected: { statusKey: "disconnected", cls: "text-yellow-600 dark:text-yellow-400", Icon: TriangleAlert },
  not_installed: { statusKey: "not_installed", cls: "text-faint", Icon: CircleDashed },
  installing: { statusKey: "installing", cls: "text-blue-600 dark:text-blue-400", Icon: Loader2 },
  error: { statusKey: "error", cls: "text-red-600 dark:text-red-400", Icon: XCircle },
  disabled: { statusKey: "disabled", cls: "text-faint", Icon: CircleOff },
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
  // 停用时不拼 statusDetail——"已停用"徽标下再显示 "Connected · Chrome" 会误导
  // (底层 relay 可能仍连着,但用户视角此连接器已不可用)。
  if (c.enabled && c.status === "connected" && c.statusDetail) bits.push(c.statusDetail);
  return bits.join(" · ");
}

export default function ConnectorsPage() {
  // conn 域 catalog（messages/{en,zh-CN}/conn.json）。
  const tConn = useTranslations("conn");
  const [connectors, setConnectors] = useState<ConnectorInfo[]>([]);
  const [wizardFor, setWizardFor] = useState<string | null>(null);
  const [configFor, setConfigFor] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const [handoff, setHandoff] = useState<{ tabId: string }[]>([]);
  const [progress, setProgress] = useState<{
    tool: string;
    stage: string;
    elapsedMs?: number;
  } | null>(null);
  const [pushedPage, setPushedPage] = useState<PushedPage | null>(null);
  const router = useRouter();

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

  // 事件通道(connector §5 推送):状态变化/进度/推送页即时到达,轮询只做兜底。
  useEffect(() => {
    let ws: WebSocket | null = null;
    try {
      ws = api.openSocket(api.wsConnectorsUrl());
    } catch {
      return; // 预渲染环境
    }
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data) as ConnectorEvent;
        if (msg.type === "snapshot") {
          const snap = msg as unknown as {
            connectors?: { connectors?: ConnectorInfo[] };
            latestPage?: PushedPage | null;
          };
          setConnectors(snap.connectors?.connectors || []);
          setPushedPage(snap.latestPage || null);
        } else if (msg.type === "connector_status_changed") {
          refresh();
        } else if (msg.type === "tool_progress") {
          setProgress({
            tool: String(msg.tool || ""),
            stage: String(msg.stage || ""),
            elapsedMs: typeof msg.elapsedMs === "number" ? msg.elapsedMs : undefined,
          });
          if (String(msg.stage || "").endsWith("completed")) {
            setTimeout(() => setProgress(null), 1500);
          }
        } else if (msg.type === "handoff_changed") {
          refresh();
        } else if (msg.type === "page_pushed") {
          setPushedPage(msg as unknown as PushedPage);
        }
      } catch { /* 非 JSON 帧 */ }
    };
    return () => {
      try { ws?.close(); } catch { /* noop */ }
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
        <h1 className="text-lg font-semibold text-txt">{tConn("title")}</h1>
        <button
          onClick={refresh}
          title={tConn("refresh")}
          className="ml-auto rounded-md p-1.5 text-faint hover:bg-card hover:text-txt"
        >
          <RefreshCw className="h-4 w-4" />
        </button>
      </header>

      {/* 浏览器工具进度(browser §5.1):慢操作永远有反馈 */}
      {progress && (
        <div className="mb-4 flex items-center gap-2 rounded-xl border border-line bg-card px-4 py-2.5 text-xs text-faint">
          <Loader2 className="h-3.5 w-3.5 animate-spin text-violet-500" />
          <span className="font-mono">{progress.tool}</span>
          <span>· {progress.stage}</span>
          {progress.elapsedMs !== undefined && (
            <span className="text-faint">· {(progress.elapsedMs / 1000).toFixed(1)}s</span>
          )}
        </div>
      )}

      {/* 浏览器推送页(popup「发送此页面」→ 连接器页 → 预填进聊天) */}
      {pushedPage?.url && (
        <div className="mb-4 flex items-center gap-3 rounded-xl border border-line bg-card p-4">
          <ExternalLink className="h-4 w-4 shrink-0 text-faint" />
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium text-txt">
              {pushedPage.title
                ? tConn("pushedPage.fromWithTitle", { title: pushedPage.title })
                : tConn("pushedPage.from")}
            </div>
            <div className="mt-0.5 truncate text-xs text-faint">{pushedPage.url}</div>
          </div>
          <button
            onClick={() => {
              // 预填进聊天(既有 ginno:prefill-input 机制)并跳回工作区
              window.dispatchEvent(
                new CustomEvent("ginno:prefill-input", {
                  detail: {
                    // 预填文案是发给模型的组合模板（会进对话），按 i18n 设计走
                    // provider 的 t(en, zh) 选边，而非纯 UI 的 useTranslations。
                    text: llmT(
                      `Please help me work with this browser page: ${pushedPage.title || pushedPage.url}\n${pushedPage.url}`,
                      `请帮我处理这个浏览器页面：${pushedPage.title || pushedPage.url}\n${pushedPage.url}`,
                    ),
                  },
                }),
              );
              router.push("/");
            }}
            className="shrink-0 rounded-lg bg-violet-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-violet-500"
          >
            <Send className="mr-1 inline h-3 w-3" />
            {tConn("pushedPage.send")}
          </button>
        </div>
      )}

      {/* 接管横幅(设计 M3):browser_handoff 工具阻塞等待时在此释放 */}
      {handoff.length > 0 && (
        <div className="mb-4 flex items-center gap-3 rounded-xl border border-violet-500/40 bg-violet-500/10 p-4">
          <div className="min-w-0 flex-1 text-sm text-txt">
            <div className="font-medium">{tConn("handoff.title")}</div>
            <div className="mt-0.5 text-xs text-faint">
              {tConn("handoff.detail", {
                count: handoff.length,
                ids: handoff.map((h) => `#${h.tabId}`).join(", "),
              })}
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
            {tConn("handoff.continue")}
          </button>
        </div>
      )}

      {!loaded ? (
        <div className="flex items-center gap-2 py-10 text-sm text-faint">
          <Loader2 className="h-4 w-4 animate-spin" /> {tConn("loading")}
        </div>
      ) : connectors.length === 0 ? (
        <div className="py-10 text-sm text-faint">{tConn("empty")}</div>
      ) : (
        <div className="space-y-3">
          {connectors.map((c) => {
            // 徽标优先反映 enabled 开关:后端从不把 status 置为 disabled
            // (STATUS_DISABLED 无人上报,停用只落在 settings.json 的
            // connectors.<id>.enabled)——按 c.enabled 派生展示态才能一目了然。
            const meta = !c.enabled
              ? STATUS_META.disabled
              : (STATUS_META[c.status] || STATUS_META.not_installed);
            const Icon = iconFor(c);
            return (
              <div
                key={c.id}
                className={`rounded-xl border border-line bg-card p-4 ${!c.enabled ? "opacity-70" : ""}`}
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
                        {tConn(`status.${meta.statusKey}`)}
                      </span>
                    </div>
                    {metaLine(c) && (
                      <div className="mt-0.5 truncate text-xs text-faint">{metaLine(c)}</div>
                    )}
                    <p className="mt-1.5 text-xs leading-relaxed text-faint">{c.description}</p>
                    {!c.enabled && (
                      <p className="mt-1 text-xs text-faint">{tConn("disabledHint")}</p>
                    )}
                  </div>
                </div>

                <div className="mt-3 flex flex-wrap items-center gap-2">
                  {!c.enabled ? (
                    <>
                      {/* 停用态只留恢复路径:一键启用(主色,一目了然)+ 配置。
                          安装向导 / 实例启停在停用态无意义,不展示——原实现把整个
                          操作区渲染成 null,导致停用后没有任何地方能再启用。 */}
                      <button
                        onClick={async () => {
                          await api.patchConnectorConfig(c.id, { enabled: true });
                          refresh();
                        }}
                        className="rounded-lg bg-violet-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-violet-500"
                      >
                        {tConn("actions.enable")}
                      </button>
                      <button
                        onClick={() => setConfigFor(configFor === c.id ? null : c.id)}
                        className="rounded-lg border border-line px-3 py-1.5 text-xs text-txt hover:bg-hover"
                      >
                        {tConn("actions.configure")}
                      </button>
                    </>
                  ) : (
                    <>
                      {(c.status === "not_installed" || c.status === "disconnected" || c.status === "error") &&
                        (c.installSteps?.length ? (
                          <button
                            onClick={() => setWizardFor(c.id)}
                            className="rounded-lg bg-violet-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-violet-500"
                          >
                            {tConn("actions.installGuide")}
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
                          {c.status === "connected"
                            ? tConn("actions.stopInstance")
                            : tConn("actions.startInstance")}
                        </button>
                      )}
                      <button
                        onClick={() => setConfigFor(configFor === c.id ? null : c.id)}
                        className="rounded-lg border border-line px-3 py-1.5 text-xs text-txt hover:bg-hover"
                      >
                        {tConn("actions.configure")}
                      </button>
                      <button
                        onClick={async () => {
                          await api.patchConnectorConfig(c.id, { enabled: false });
                          refresh();
                        }}
                        className="rounded-lg border border-line px-3 py-1.5 text-xs text-faint hover:bg-hover"
                      >
                        {tConn("actions.disable")}
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
