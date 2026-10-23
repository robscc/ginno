"use client";

/* Connectors page（mods-connector-migration-design.md §3/§7 S3）：
 * 一个页面同时承载内置连接器（Integrations）与 Claude Mods；每个连接器
 * 点开后是同一个详情抽屉，tab/action 由各自 detailView 声明。
 * 浏览器接管/推送页/工具进度横幅保持原行为。 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { t as llmT } from "@/i18n/provider";
import * as api from "@/lib/runtime";
import type {
  ConnectorEvent,
  ConnectorInfo,
  ConnectorRuntimeStatus,
  PushedPage,
} from "@/lib/runtime";
import { ConnectorDetail } from "@/components/connectors/ConnectorDetail";
import { connectorIcon, connectorKind, metaLine, sourceOf, statusMeta } from "@/components/connectors/meta";
import { ModStoreDrawer } from "@/components/settings/ModStoreDrawer";
import {
  Cable,
  ExternalLink,
  Loader2,
  RefreshCw,
  Send,
  Store,
  TriangleAlert,
} from "lucide-react";
import { useRouter } from "next/navigation";

const POLL_MS = 5000; // 轮询兜底；状态变化另有 WS 即时推送

function hasSettingsTab(c: ConnectorInfo): boolean {
  // detailView 是权威（后端在「除 enabled 外无配置项」时不会声明 settings）。
  const declared = c.detailView?.tabs;
  if (declared) return declared.some((x) => x.kind === "settings");
  const props = c.configSchema?.properties;
  if (props) return Object.keys(props).some((k) => k !== "enabled");
  return (c.configSummary?.count ?? 0) > 0;
}

function hasInstallTab(c: ConnectorInfo): boolean {
  if (c.detailView?.tabs?.some((x) => x.kind === "install")) return true;
  return !!c.installSteps?.length;
}

export default function ConnectorsPage() {
  const tConn = useTranslations("conn");
  const [connectors, setConnectors] = useState<ConnectorInfo[]>([]);
  const [runtime, setRuntime] = useState<ConnectorRuntimeStatus | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [storeOpen, setStoreOpen] = useState(false);
  const [selected, setSelected] = useState<{
    connector: ConnectorInfo;
    initialTab?: string;
  } | null>(null);
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
      setRuntime(r.runtime ?? null);
    } catch {
      /* sidecar not up yet — keep last state */
    } finally {
      setLoaded(true);
    }
    try {
      const h = await api.getHandoffStatus();
      setHandoff(h.active || []);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    void refresh();
    timer.current = setInterval(refresh, POLL_MS);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [refresh]);

  // 事件通道（connector §5 推送）：状态变化/进度/推送页即时到达，轮询兜底。
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
          void refresh();
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
          void refresh();
        } else if (msg.type === "page_pushed") {
          setPushedPage(msg as unknown as PushedPage);
        }
      } catch {
        /* 非 JSON 帧 */
      }
    };
    return () => {
      try {
        ws?.close();
      } catch {
        /* noop */
      }
    };
  }, [refresh]);

  const builtin = useMemo(
    () => connectors.filter((c) => connectorKind(c) === "builtin"),
    [connectors],
  );
  const mods = useMemo(
    () => connectors.filter((c) => connectorKind(c) === "mod"),
    [connectors],
  );

  // 运行时就绪度：握手期（connecting/reconnecting）是正常过程，只在真正
  // 不可用（Node 缺失 / 通道 unavailable / 全局关闭）时才报黄条，避免把
  // 启动瞬间的连接中状态渲染成「故障，修复前不会加载」。
  const rtStatus = runtime?.status || "";
  const runtimeUnavailable = !!runtime && (!runtime.nodePath || rtStatus === "unavailable");
  const runtimeOff = !!runtime && rtStatus === "disabled" && runtime.modsEnabled === false;
  const runtimeBusy =
    !!runtime &&
    !runtimeUnavailable &&
    !runtimeOff &&
    (rtStatus === "connecting" ||
      rtStatus === "reconnecting" ||
      (!rtStatus && runtime.brokerOk === false));

  const openDetail = (c: ConnectorInfo, initialTab?: string) => {
    setSelected({ connector: c, initialTab });
  };

  const renderCard = (c: ConnectorInfo) => {
    const meta = statusMeta(c);
    const Icon = connectorIcon(c);
    const kind = connectorKind(c);
    const src = sourceOf(c);
    const summary = c.configSummary;
    const version = c.version ? `v${c.version}` : "";
    const sourceLabel =
      src === "global"
        ? tConn("source.global")
        : src === "project"
          ? tConn("source.project")
          : src;
    return (
      <div
        key={c.id}
        role="button"
        tabIndex={0}
        onClick={() => openDetail(c)}
        onKeyDown={(e) => {
          // 内层按钮的键盘事件不触发整卡打开
          if (e.target !== e.currentTarget) return;
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            openDetail(c);
          }
        }}
        className={`cursor-pointer rounded-xl border border-line bg-card p-4 transition-colors hover:border-line2 ${
          !c.enabled ? "opacity-70" : ""
        }`}
      >
        <div className="flex items-start gap-3">
          <Icon
            className={`mt-0.5 h-5 w-5 shrink-0 ${kind === "mod" ? "text-violet" : "text-faint"}`}
          />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="truncate text-sm font-medium text-txt">{c.name}</span>
              <span className={`inline-flex items-center gap-1 text-[11px] ${meta.cls}`}>
                <meta.Icon
                  className={`h-3 w-3 ${
                    c.status === "installing" || c.status === "restarting" ? "animate-spin" : ""
                  }`}
                />
                {tConn(`status.${meta.statusKey}`)}
              </span>
              <span className="rounded-full border border-line2 px-1.5 py-px text-[10px] text-faint">
                {tConn(`kind.${kind}`)}
              </span>
              {sourceLabel && (
                <span className="rounded-full border border-line2 bg-card2 px-1.5 py-px text-[10px] text-faint">
                  {sourceLabel}
                </span>
              )}
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-faint">
              {version && <span>{version}</span>}
              {metaLine(c) && <span className="truncate">{metaLine(c)}</span>}
              <span>
                {hasSettingsTab(c)
                  ? summary
                    ? tConn("card.settingsSummary", {
                        count: summary.count,
                        changed: summary.modified,
                      })
                    : tConn("card.settingsAvailable")
                  : tConn("card.noSettings")}
              </span>
            </div>
            <p className="mt-1.5 text-xs leading-relaxed text-faint">{c.description}</p>
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          {!c.enabled ? (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                void api
                  .patchConnectorConfig(c.id, { enabled: true })
                  .then(() => refresh())
                  .catch(() => {});
              }}
              className="rounded-lg bg-violet-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-violet-500"
            >
              {tConn("card.enable")}
            </button>
          ) : (
            <>
              {c.id === "browser-profile" && (
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    void api
                      .connectorAction(
                        c.id,
                        c.status === "connected" ? "profile_stop" : "profile_start",
                      )
                      .then(() => refresh())
                      .catch(() => {});
                  }}
                  className="rounded-lg border border-line px-3 py-1.5 text-xs text-txt hover:bg-hover"
                >
                  {c.status === "connected"
                    ? tConn("actions.stopInstance")
                    : tConn("actions.startInstance")}
                </button>
              )}
              {hasInstallTab(c) &&
                (c.status === "not_installed" ||
                  c.status === "disconnected" ||
                  c.status === "error") && (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      openDetail(c, "install");
                    }}
                    className="rounded-lg border border-line px-3 py-1.5 text-xs text-txt hover:bg-hover"
                  >
                    {tConn("actions.installGuide")}
                  </button>
                )}
              {hasSettingsTab(c) && (
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    openDetail(c, "settings");
                  }}
                  className="rounded-lg border border-line px-3 py-1.5 text-xs text-txt hover:bg-hover"
                >
                  {tConn("card.settings")}
                </button>
              )}
            </>
          )}
        </div>
      </div>
    );
  };

  return (
    // 壳的页面槽（AppShell 非工作区路由）是 flex 容器、不含滚动：页面必须
    // 自带竖向滚动，否则内容超过一屏就被裁掉（2026-10-23：看不到 Claude Mods）。
    <div className="min-h-0 min-w-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-4xl px-6 py-8">
      <header className="mb-5 flex flex-wrap items-center gap-2">
        <Cable className="h-5 w-5 text-faint" />
        <h1 className="text-lg font-semibold text-txt">{tConn("title")}</h1>
        <span className="text-xs text-faint">{tConn("subtitle")}</span>
        <button
          type="button"
          onClick={() => void refresh()}
          title={tConn("refresh")}
          className="ml-auto rounded-md p-1.5 text-faint hover:bg-card hover:text-txt"
        >
          <RefreshCw className="h-4 w-4" />
        </button>
      </header>

      {/* 运行时状态条：连接中走中性提示；只有真不可用才告警 */}
      {runtimeUnavailable && (
        <div className="mb-4 flex items-start gap-2 rounded-xl border border-yellow/30 bg-yellow/10 px-4 py-2.5 text-xs leading-relaxed text-yellow-800 dark:text-yellow-200">
          <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            {tConn("runtime.warning", {
              nodePath: runtime?.nodePath || tConn("runtime.nodeMissing"),
            })}
          </span>
        </div>
      )}
      {runtimeOff && (
        <div className="mb-4 flex items-start gap-2 rounded-xl border border-yellow/30 bg-yellow/10 px-4 py-2.5 text-xs leading-relaxed text-yellow-800 dark:text-yellow-200">
          <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{tConn("runtime.off")}</span>
        </div>
      )}
      {runtimeBusy && (
        <div className="mb-4 flex items-center gap-2 rounded-xl border border-line bg-card px-4 py-2.5 text-xs text-faint">
          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-violet-500" />
          <span>
            {tConn("runtime.connecting")}
            {runtime?.detail ? ` · ${runtime.detail}` : ""}
          </span>
        </div>
      )}

      {/* 浏览器工具进度（browser §5.1）：慢操作永远有反馈 */}
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

      {/* 浏览器推送页（popup「发送此页面」→ 连接器页 → 预填进聊天） */}
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
            type="button"
            onClick={() => {
              // 预填进聊天（既有 ginno:prefill-input 机制）并跳回工作区
              window.dispatchEvent(
                new CustomEvent("ginno:prefill-input", {
                  detail: {
                    // 预填文案是发给模型的组合模板（会进对话），走 provider 的
                    // t(en, zh) 选边，而非纯 UI 的 useTranslations。
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

      {/* 接管横幅（设计 M3）：browser_handoff 工具阻塞等待时在此释放 */}
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
            type="button"
            onClick={async () => {
              for (const h of handoff) {
                await api.connectorAction("chrome-extension", "browser_handoff_release", {
                  tabId: h.tabId,
                });
              }
              void refresh();
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
        <div className="space-y-8">
          <section>
            <div className="mb-1 flex items-baseline gap-2">
              <h2 className="text-[12px] font-semibold uppercase tracking-wide text-muted">
                {tConn("section.integrations")}
              </h2>
              <span className="text-[11px] text-faint">
                {tConn("section.count", { count: builtin.length })}
              </span>
            </div>
            <p className="mb-3 text-[11.5px] text-faint">{tConn("section.integrationsSub")}</p>
            {builtin.length ? (
              <div className="space-y-2.5">{builtin.map(renderCard)}</div>
            ) : (
              <p className="text-xs text-faint">{tConn("section.builtinEmpty")}</p>
            )}
          </section>

          <section>
            <div className="mb-1 flex items-baseline gap-2">
              <h2 className="text-[12px] font-semibold uppercase tracking-wide text-muted">
                {tConn("section.mods")}
              </h2>
              <span className="text-[11px] text-faint">
                {tConn("section.count", { count: mods.length })}
              </span>
            </div>
            <p className="mb-3 text-[11.5px] text-faint">{tConn("section.modsSub")}</p>
            {mods.length ? (
              <div className="space-y-2.5">{mods.map(renderCard)}</div>
            ) : (
              <p className="text-xs text-faint">{tConn("section.modsEmpty")}</p>
            )}
          </section>

          {/* 商店入口放在页面底部（2026-10-23 用户定稿）：浏览是低频终点动作，
             不占顶部主视线；空列表时也不再多出一个重复按钮。 */}
          <div className="mt-7 flex flex-wrap items-center justify-center gap-2.5">
            <button
              type="button"
              onClick={() => setStoreOpen(true)}
              className="flex items-center gap-1.5 rounded-lg border border-violet/40 bg-violet/10 px-3 py-1.5 text-xs text-violet transition-colors hover:bg-violet/20"
            >
              <Store className="h-3.5 w-3.5" /> {tConn("browseMods")}
            </button>
            <span className="text-[11px] text-faint">{tConn("runtime.browseHint")}</span>
          </div>
        </div>
      )}

      {selected && (
        <ConnectorDetail
          key={selected.connector.id}
          connector={selected.connector}
          initialTab={selected.initialTab}
          onClose={() => setSelected(null)}
          onRefresh={() => void refresh()}
          onBrowseMods={() => setStoreOpen(true)}
          storeOpen={storeOpen}
        />
      )}

      {storeOpen && (
        <ModStoreDrawer
          onClose={() => setStoreOpen(false)}
          onInstalled={() => void refresh()}
        />
      )}
      </div>
    </div>
  );
}