"use client";

/* 连接器详情抽屉（mods-connector-migration-design.md §3.5）。
 *
 * 统一的是基座，不是外观：tab 列表来自 detailView.tabs，前端只按共享
 * kind（overview / settings / install / activity / permissions）渲染。
 * 内置连接器和 Claude Mod 走同一套数据契约与操作。 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import type {
  ConnectorDetailTab,
  ConnectorInfo,
  ModCompat,
} from "@/lib/runtime";
import { ConfigEditor } from "./ConfigEditor";
import { InstallWizard } from "./InstallWizard";
import { connectorIcon, connectorKind, sourceOf, statusMeta } from "./meta";
import {
  Loader2,
  RefreshCw,
  ShieldAlert,
  Store,
  Trash2,
  X,
} from "lucide-react";

type TFn = (key: string, values?: Record<string, string | number>) => string;

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** 后端没声明 detailView 时的兜底（旧后端/半实现状态）。 */
function fallbackTabs(
  c: ConnectorInfo,
  hasSettings: boolean,
  t: TFn,
): ConnectorDetailTab[] {
  const tabs: ConnectorDetailTab[] = [
    { id: "overview", kind: "overview", title: t("detail.tabs.overview") },
  ];
  if (hasSettings) {
    tabs.push({ id: "settings", kind: "settings", title: t("detail.tabs.settings") });
  }
  if (connectorKind(c) === "mod") {
    tabs.push({ id: "permissions", kind: "permissions", title: t("detail.tabs.permissions") });
  } else if (c.installSteps?.length) {
    tabs.push({ id: "install", kind: "install", title: t("detail.tabs.install") });
  }
  tabs.push({ id: "activity", kind: "activity", title: t("detail.tabs.activity") });
  return tabs;
}

/** 共享 kind 用本地 catalog 文案；自定义页签保留后端下发的 title。 */
function tabTitle(tab: ConnectorDetailTab, t: TFn): string {
  switch (tab.kind) {
    case "overview":
      return t("detail.tabs.overview");
    case "settings":
      return t("detail.tabs.settings");
    case "install":
      return t("detail.tabs.install");
    case "activity":
      return t("detail.tabs.activity");
    case "permissions":
      return t("detail.tabs.permissions");
    default:
      return tab.title || tab.kind;
  }
}

export function ConnectorDetail({
  connector,
  initialTab,
  onClose,
  onRefresh,
  onBrowseMods,
  storeOpen = false,
}: {
  connector: ConnectorInfo;
  initialTab?: string;
  onClose: () => void;
  onRefresh: () => void;
  onBrowseMods?: () => void;
  /** 商店抽屉开着时，ESC 只关商店，不能连详情一起关。 */
  storeOpen?: boolean;
}) {
  const t = useTranslations("conn");
  const tr = t as unknown as TFn;

  const [detail, setDetail] = useState<ConnectorInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [tab, setTab] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [confirmUninstall, setConfirmUninstall] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [grantBusy, setGrantBusy] = useState(false);
  const dirtyRef = useRef(false);

  const current = detail ?? connector;
  const kind = connectorKind(current);
  const status = statusMeta(current);
  const StatusIcon = status.Icon;
  const Icon = connectorIcon(current);
  const src = sourceOf(current);
  const modName =
    typeof current.extra?.modName === "string" && current.extra.modName
      ? current.extra.modName
      : current.id.replace(/^mod-/, "");

  const load = useCallback(async () => {
    setLoading(true);
    setLoadFailed(false);
    try {
      const d = await api.getConnectorDetail(connector.id);
      setDetail(d);
    } catch {
      // 保留列表条目作为降级展示；详情字段缺失时各 tab 自行兜底。
      setLoadFailed(true);
    } finally {
      setLoading(false);
    }
  }, [connector.id]);

  useEffect(() => {
    void load();
  }, [load]);

  const settingsAvailable =
    !!current.configSchema?.properties &&
    Object.keys(current.configSchema.properties).filter((k) => k !== "enabled").length > 0;

  const tabs = useMemo(() => {
    const declared = current.detailView?.tabs;
    if (declared && declared.length > 0) return declared;
    return fallbackTabs(current, settingsAvailable, tr);
  }, [current, settingsAvailable, tr]);

  const tabsKey = tabs.map((x) => x.id).join("|");
  const initialApplied = useRef(false);
  useEffect(() => {
    if (!tabs.length) return;
    // 入口 tab（如从卡片「安装向导」进来）可能晚于详情加载才出现——只要
    // 还没应用过且它出现了就跳过去。
    if (initialTab && !initialApplied.current) {
      const found = tabs.find((x) => x.id === initialTab);
      if (found) {
        initialApplied.current = true;
        setTab(found.id);
        return;
      }
    }
    setTab((cur) => (tabs.some((x) => x.id === cur) ? cur : tabs[0].id));
    // tabs 内容由 tabsKey 代表；用 key 避免每次渲染重建数组触发。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabsKey, initialTab]);

  // 未保存保护：关闭/切 tab 都要确认（ConfigEditor 通过 onDirtyChange 上报）。
  const canLeave = useCallback(() => {
    if (!dirtyRef.current) return true;
    return window.confirm(tr("config.unsavedConfirm"));
  }, [tr]);

  const requestClose = useCallback(() => {
    if (!canLeave()) return;
    onClose();
  }, [canLeave, onClose]);

  // 稳定回调：ConfigEditor 的 dirty 上报 effect 不能因父级重渲染反复触发。
  const handleDirtyChange = useCallback((d: boolean) => {
    dirtyRef.current = d;
  }, []);

  const switchTab = (id: string) => {
    if (id === tab) return;
    if (!canLeave()) return;
    dirtyRef.current = false;
    setTab(id);
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      // 向导/商店开着时 ESC 只关上层，不动带未保存更改的详情抽屉。
      if (wizardOpen) {
        setWizardOpen(false);
        return;
      }
      if (storeOpen) return;
      requestClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [requestClose, wizardOpen, storeOpen]);

  const runAction = async (actionId: string) => {
    setBusy(actionId);
    setMsg(null);
    try {
      const r = await api.connectorAction(current.id, actionId);
      if (r && r.ok === false) {
        setMsg({ kind: "error", text: String(r.error || tr("detail.actionFailed")) });
      } else {
        await load();
        onRefresh();
      }
    } catch {
      setMsg({ kind: "error", text: tr("detail.actionFailed") });
    } finally {
      setBusy(null);
    }
  };

  const toggleEnabled = async () => {
    const next = current.enabled === false;
    setBusy("toggle");
    setMsg(null);
    try {
      const r = await api.patchConnectorConfig(current.id, { enabled: next });
      if (r.ok === false || r.error) {
        setMsg({ kind: "error", text: r.error || tr("detail.actionFailed") });
      } else {
        setDetail((d) => (d ? { ...d, enabled: next } : d));
        onRefresh();
      }
    } catch {
      setMsg({ kind: "error", text: tr("detail.actionFailed") });
    } finally {
      setBusy(null);
    }
  };

  const uninstall = async () => {
    setBusy("uninstall");
    setMsg(null);
    try {
      const r = await api.uninstallMod(modName);
      if (!r.ok) {
        setMsg({ kind: "error", text: r.error || tr("detail.uninstallFailed") });
        return;
      }
      onRefresh();
      onClose();
    } catch {
      setMsg({ kind: "error", text: tr("detail.uninstallFailed") });
    } finally {
      setBusy(null);
    }
  };

  const setClassicGrant = async (value: string) => {
    setGrantBusy(true);
    setMsg(null);
    try {
      const r = await api.updateMod(modName, { grants: { classic: value } });
      if (r.ok === false) {
        setMsg({ kind: "error", text: r.error || tr("detail.permissions.saveFailed") });
        return;
      }
      setDetail((d) => (d ? { ...d, classicGrant: value } : d));
      setMsg({ kind: "ok", text: tr("detail.permissions.saved") });
      onRefresh();
    } catch {
      setMsg({ kind: "error", text: tr("detail.permissions.saveFailed") });
    } finally {
      setGrantBusy(false);
    }
  };

  const actions = current.detailView?.actions || [];
  const rawCompat = current.extra?.compat;
  const compat = isPlainObject(rawCompat)
    ? (rawCompat as unknown as ModCompat)
    : null;

  if (typeof document === "undefined") return null;

  return createPortal(
    <div
      className="fixed inset-0 z-[60] flex justify-end bg-black/45"
      onClick={requestClose}
    >
      <aside
        className="flex h-full w-[min(640px,94vw)] flex-col border-l border-line2 bg-base shadow-2xl"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={current.name}
      >
        {/* header */}
        <div className="flex items-center gap-3 border-b border-line px-5 pb-3 pt-4">
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-violet/30 bg-violet/10 text-violet">
            <Icon className="h-4 w-4" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="truncate text-sm font-semibold text-txt">{current.name}</span>
              <span className={`inline-flex items-center gap-1 text-[11px] ${status.cls}`}>
                <StatusIcon
                  className={`h-3 w-3 ${current.status === "installing" || current.status === "restarting" ? "animate-spin" : ""}`}
                />
                {t(`status.${status.statusKey}`)}
              </span>
              <span className="rounded-full border border-line2 px-1.5 py-px text-[10px] text-faint">
                {t(`kind.${kind}`)}
              </span>
            </div>
            <div className="mt-0.5 truncate text-[11px] text-faint">
              {[
                current.version ? `v${current.version}` : "",
                current.statusDetail || "",
                src || "",
              ]
                .filter(Boolean)
                .join(" · ")}
            </div>
          </div>
          <button
            type="button"
            onClick={() => void toggleEnabled()}
            disabled={busy === "toggle"}
            className={`shrink-0 rounded-lg border px-2.5 py-1 text-[11px] transition-colors disabled:opacity-50 ${
              current.enabled === false
                ? "border-violet/40 bg-violet/10 text-violet hover:bg-violet/20"
                : "border-line text-muted hover:bg-hover hover:text-txt"
            }`}
          >
            {current.enabled === false ? t("detail.enable") : t("detail.disable")}
          </button>
          <button
            type="button"
            onClick={requestClose}
            title={t("detail.close")}
            className="shrink-0 rounded-md p-1.5 text-faint hover:bg-hover hover:text-txt"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* action strip：后端声明的动作 + 标准动作 */}
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-5 py-2">
          {actions.map((a) => (
            <button
              key={a.id}
              type="button"
              onClick={() => void runAction(a.id)}
              disabled={busy === a.id}
              className="rounded-lg border border-line px-2.5 py-1 text-[11px] text-txt hover:bg-hover disabled:opacity-50"
            >
              {busy === a.id ? <Loader2 className="mr-1 inline h-3 w-3 animate-spin" /> : null}
              {a.title || a.id}
            </button>
          ))}
          {tabs.some((x) => x.kind === "settings") && (
            <button
              type="button"
              onClick={() => switchTab(tabs.find((x) => x.kind === "settings")!.id)}
              className="rounded-lg border border-line px-2.5 py-1 text-[11px] text-txt hover:bg-hover"
            >
              {t("card.settings")}
            </button>
          )}
          {kind === "mod" && onBrowseMods && (
            <button
              type="button"
              onClick={onBrowseMods}
              className="flex items-center gap-1 rounded-lg border border-violet/40 bg-violet/10 px-2.5 py-1 text-[11px] text-violet hover:bg-violet/20"
            >
              <Store className="h-3 w-3" /> {t("detail.browseMods")}
            </button>
          )}
          <span className="flex-1" />
          {kind === "mod" &&
            (confirmUninstall ? (
              <span className="flex items-center gap-2">
                <span className="text-[10px] text-faint">{t("detail.uninstallHint")}</span>
                <button
                  type="button"
                  onClick={() => void uninstall()}
                  disabled={busy === "uninstall"}
                  className="text-[11px] text-red-600 hover:opacity-80 disabled:opacity-50 dark:text-red-400"
                >
                  {t("detail.uninstallConfirm")}
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmUninstall(false)}
                  className="text-[11px] text-muted hover:text-txt"
                >
                  {t("detail.uninstallCancel")}
                </button>
              </span>
            ) : (
              <button
                type="button"
                onClick={() => setConfirmUninstall(true)}
                disabled={busy === "uninstall"}
                className="flex items-center gap-1 text-[11px] text-muted hover:text-red-600 disabled:opacity-50 dark:hover:text-red-400"
              >
                <Trash2 className="h-3 w-3" /> {t("detail.uninstall")}
              </button>
            ))}
        </div>

        {msg && (
          <div
            className={`border-b px-5 py-1.5 text-[11px] ${
              msg.kind === "error"
                ? "border-red/30 bg-red/10 text-red-600 dark:text-red-400"
                : "border-green/30 bg-green/10 text-green-700 dark:text-green-400"
            }`}
          >
            {msg.text}
          </div>
        )}

        {/* tabs */}
        <div className="flex gap-0.5 overflow-x-auto border-b border-line px-5">
          {tabs.map((x) => (
            <button
              key={x.id}
              type="button"
              onClick={() => switchTab(x.id)}
              className={`-mb-px whitespace-nowrap border-b-2 px-2.5 py-2 text-[12px] transition-colors ${
                x.id === tab
                  ? "border-violet text-txt"
                  : "border-transparent text-faint hover:text-muted"
              }`}
            >
              {tabTitle(x, tr)}
            </button>
          ))}
        </div>

        {/* body */}
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {loading && !detail ? (
            <div className="flex items-center gap-2 py-8 text-sm text-faint">
              <Loader2 className="h-4 w-4 animate-spin" /> {t("detail.loading")}
            </div>
          ) : loadFailed && !detail ? (
            <div className="flex items-center gap-3 py-6 text-sm text-faint">
              {t("detail.loadFailed")}
              <button
                type="button"
                onClick={() => void load()}
                className="rounded-lg border border-line px-2.5 py-1 text-xs text-txt hover:bg-hover"
              >
                <RefreshCw className="mr-1 inline h-3 w-3" />
                {t("refresh")}
              </button>
            </div>
          ) : (
            <DetailPane
              activeTab={tabs.find((x) => x.id === tab)?.kind || ""}
              current={current}
              compat={compat}
              modName={modName}
              kind={kind}
              src={src}
              settingsAvailable={settingsAvailable}
              installCount={current.installSteps?.length || 0}
              busy={busy}
              grantBusy={grantBusy}
              tr={tr}
              onOpenSettings={() => switchTab("settings")}
              onOpenInstallWizard={() => setWizardOpen(true)}
              onReload={async () => {
                await load();
                onRefresh();
              }}
              onSetClassicGrant={setClassicGrant}
              onDirtyChange={handleDirtyChange}
              onSaved={() => {
                void load();
                onRefresh();
              }}
            />
          )}
        </div>
      </aside>

      {wizardOpen && (
        // 阻止冒泡：向导内点击不应触发抽屉的 scrim 关闭。
        <div onClick={(e) => e.stopPropagation()}>
          <InstallWizard connectorId={current.id} onClose={() => setWizardOpen(false)} />
        </div>
      )}
    </div>,
    document.body,
  );
}

/* ---- 各 tab 的内容 -------------------------------------------------------- */

function DetailPane({
  activeTab,
  current,
  compat,
  modName,
  kind,
  src,
  settingsAvailable,
  installCount,
  busy,
  grantBusy,
  tr,
  onOpenSettings,
  onOpenInstallWizard,
  onReload,
  onSetClassicGrant,
  onDirtyChange,
  onSaved,
}: {
  activeTab: string;
  current: ConnectorInfo;
  compat: ModCompat | null;
  modName: string;
  kind: "builtin" | "mod";
  src: string | null;
  settingsAvailable: boolean;
  installCount: number;
  busy: string | null;
  grantBusy: boolean;
  tr: TFn;
  onOpenSettings: () => void;
  onOpenInstallWizard: () => void;
  onReload: () => Promise<void>;
  onSetClassicGrant: (v: string) => Promise<void>;
  onDirtyChange: (dirty: boolean) => void;
  onSaved: () => void;
}) {
  const t = useTranslations("conn");

  if (activeTab === "overview") {
    const extraRows = Object.entries(current.extra || {}).filter(([k, v]) => {
      if (k === "compat" || k === "modName" || k === "origin") return false;
      return typeof v === "string" || typeof v === "number" || typeof v === "boolean";
    });
    const origin = current.extra?.origin;
    const originText =
      typeof origin === "string"
        ? origin
        : isPlainObject(origin) && typeof origin.marketplace === "string"
          ? [origin.marketplace, origin.version ? `v${origin.version}` : ""]
              .filter(Boolean)
              .join(" · ")
          : null;
    return (
      <div className="space-y-5">
        <p className="text-[12.5px] leading-relaxed text-muted">{current.description}</p>

        <section>
          <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted">
            {t("detail.overview.capabilities")}
          </h3>
          {current.capabilities?.length ? (
            <div className="flex flex-wrap gap-1.5">
              {current.capabilities.map((cap) => (
                <span
                  key={cap}
                  className="rounded-md border border-line2 bg-card2 px-1.5 py-0.5 font-mono text-[10.5px] text-muted"
                >
                  {cap}
                </span>
              ))}
            </div>
          ) : (
            <p className="text-[11.5px] text-faint">{t("detail.overview.noCapabilities")}</p>
          )}
        </section>

        {(extraRows.length > 0 || src || originText) && (
          <section>
            <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted">
              {t("detail.overview.registration")}
            </h3>
            <dl className="space-y-1 text-[11.5px]">
              {src && (
                <div className="flex gap-2">
                  <dt className="w-24 shrink-0 text-faint">{t("detail.overview.source")}</dt>
                  <dd className="min-w-0 break-words text-muted">
                    {src === "global"
                      ? t("source.global")
                      : src === "project"
                        ? t("source.project")
                        : src}
                  </dd>
                </div>
              )}
              {originText && originText !== src && (
                <div className="flex gap-2">
                  <dt className="w-24 shrink-0 text-faint">{t("detail.activity.origin")}</dt>
                  <dd className="min-w-0 break-words text-muted">{originText}</dd>
                </div>
              )}
              {extraRows.map(([k, v]) => (
                <div key={k} className="flex gap-2">
                  <dt className="w-24 shrink-0 font-mono text-faint">{k}</dt>
                  <dd className="min-w-0 break-words text-muted">{String(v)}</dd>
                </div>
              ))}
            </dl>
          </section>
        )}

        <section>
          <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted">
            {t("detail.overview.settings")}
          </h3>
          <p className="text-[11.5px] text-faint">
            {current.configSummary
              ? current.configSummary.count > 0
                ? t("card.settingsSummary", {
                    count: current.configSummary.count,
                    changed: current.configSummary.modified,
                  })
                : t("card.noSettings")
              : settingsAvailable
                ? t("card.settingsAvailable")
                : t("card.noSettings")}
          </p>
          {settingsAvailable && (
            <button
              type="button"
              onClick={onOpenSettings}
              className="mt-2 rounded-lg border border-line px-2.5 py-1 text-[11px] text-txt hover:bg-hover"
            >
              {t("detail.overview.openSettings")}
            </button>
          )}
        </section>
      </div>
    );
  }

  if (activeTab === "settings") {
    if (!settingsAvailable) {
      return <div className="py-6 text-sm text-faint">{t("config.empty")}</div>;
    }
    return (
      <ConfigEditor
        key={current.id}
        connectorId={current.id}
        config={current.config}
        defaults={current.defaults}
        schema={current.configSchema}
        onSaved={onSaved}
        onDirtyChange={onDirtyChange}
      />
    );
  }

  if (activeTab === "install") {
    const steps = current.installSteps || [];
    return (
      <div className="space-y-4">
        <p className="text-[11.5px] leading-relaxed text-faint">
          {t("detail.install.hint")}
        </p>
        {steps.length > 0 ? (
          <ol className="space-y-3">
            {steps.map((s, i) => (
              <li key={s.key || i} className="flex gap-2.5 text-[12px] text-muted">
                <span className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full border border-line2 bg-card2 text-[10px] text-faint">
                  {i + 1}
                </span>
                <span className="min-w-0">
                  <span className="block text-[12.5px] font-medium text-txt">{s.title}</span>
                  {s.body && <span className="mt-0.5 block leading-relaxed">{s.body}</span>}
                </span>
              </li>
            ))}
          </ol>
        ) : (
          <p className="text-[11.5px] text-faint">{t("detail.install.empty")}</p>
        )}
        {installCount > 0 && (
          <button
            type="button"
            onClick={onOpenInstallWizard}
            className="rounded-lg bg-violet-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-violet-500"
          >
            {t("detail.install.open")}
          </button>
        )}
      </div>
    );
  }

  if (activeTab === "activity") {
    const logFile = kind === "mod" ? `mod-${modName}` : `connector-${current.id}`;
    const rows: Array<[string, string]> = [
      [t("detail.activity.id"), current.id],
      [t("detail.activity.kind"), t(`kind.${kind}`)],
      [t("detail.activity.version"), current.version ? `v${current.version}` : "—"],
      [t("detail.activity.status"), t(`status.${statusMeta(current).statusKey}`)],
      [t("detail.activity.statusDetail"), current.statusDetail || "—"],
    ];
    if (src) {
      rows.push([
        t("detail.activity.source"),
        src === "global" ? t("source.global") : src === "project" ? t("source.project") : src,
      ]);
    }
    const origin = current.extra?.origin;
    if (typeof origin === "string") {
      rows.push([t("detail.activity.origin"), origin]);
    } else if (isPlainObject(origin) && typeof origin.marketplace === "string") {
      rows.push([
        t("detail.activity.origin"),
        [origin.marketplace, origin.version ? `v${origin.version}` : ""].filter(Boolean).join(" · "),
      ]);
    }
    return (
      <div className="space-y-5">
        <section>
          <div className="mb-2 flex items-center gap-2">
            <h3 className="text-[11px] font-semibold uppercase tracking-wide text-muted">
              {t("detail.activity.title")}
            </h3>
            <button
              type="button"
              onClick={() => void onReload()}
              className="ml-auto rounded-md p-1 text-faint hover:bg-hover hover:text-txt"
              title={t("refresh")}
            >
              <RefreshCw className={`h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} />
            </button>
          </div>
          <dl className="space-y-1 text-[11.5px]">
            {rows.map(([k, v]) => (
              <div key={k} className="flex gap-2">
                <dt className="w-28 shrink-0 text-faint">{k}</dt>
                <dd className="min-w-0 break-words font-mono text-muted">{v}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-2 text-[10.5px] text-faint">
            {t("detail.activity.logHint", { file: logFile })}
          </p>
        </section>

        <section>
          <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted">
            {t("detail.activity.compat")}
          </h3>
          <CompatView compat={compat} t={t as unknown as TFn} />
        </section>
      </div>
    );
  }

  if (activeTab === "permissions") {
    return (
      <div className="space-y-4">
        <div className="flex items-start gap-2 rounded-lg border border-yellow/30 bg-yellow/10 px-3 py-2.5 text-[11.5px] leading-relaxed text-yellow-800 dark:text-yellow-200">
          <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{t("detail.permissions.warning")}</span>
        </div>
        {kind === "mod" ? (
          <div className="rounded-lg border border-line bg-panel px-3 py-2.5">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[12px] text-txt">{t("detail.permissions.grantTitle")}</span>
              <span className="flex-1" />
              <select
                value={current.classicGrant || "ask"}
                disabled={grantBusy}
                onChange={(e) => void onSetClassicGrant(e.target.value)}
                className="rounded-md border border-line2 bg-card px-1.5 py-0.5 text-xs text-txt disabled:opacity-50"
              >
                <option value="ask">{t("detail.permissions.ask")}</option>
                <option value="deny">{t("detail.permissions.deny")}</option>
                <option value="allow">{t("detail.permissions.allow")}</option>
              </select>
            </div>
            <p className="mt-1.5 text-[11px] leading-relaxed text-faint">
              {t("detail.permissions.grantHint")}
            </p>
          </div>
        ) : (
          <p className="text-[11.5px] text-faint">{t("detail.permissions.builtinNote")}</p>
        )}
      </div>
    );
  }

  return <p className="py-6 text-sm text-faint">{t("detail.unsupportedTab")}</p>;
}

/* ---- 兼容度计数 ----------------------------------------------------------- */

function CompatBar({ ok, bad }: { ok: number; bad: number }) {
  const total = ok + bad;
  if (total === 0) return <span className="h-1.5 w-16 rounded-full bg-line/60" />;
  return (
    <span className="inline-flex h-1.5 w-16 overflow-hidden rounded-full bg-line/60">
      <span className="bg-green" style={{ width: `${(ok / total) * 100}%` }} />
      {bad > 0 && <span className="bg-red" style={{ width: `${(bad / total) * 100}%` }} />}
    </span>
  );
}

function CompatView({ compat, t }: { compat: ModCompat | null; t: TFn }) {
  const events = Object.entries(compat?.events ?? {});
  const ops = Object.entries(compat?.ops ?? {});
  const unimplemented = Object.entries(compat?.unimplemented ?? {});
  if (events.length === 0 && ops.length === 0 && unimplemented.length === 0) {
    return <p className="text-[11.5px] text-faint">{t("detail.activity.compatEmpty")}</p>;
  }
  return (
    <div className="space-y-2.5 text-[11px]">
      {events.length > 0 && (
        <div>
          <div className="mb-1 font-medium text-muted">{t("detail.activity.compatEvents")}</div>
          <div className="space-y-1">
            {events.map(([event, s]) => (
              <div key={event} className="flex items-center gap-2">
                <span className="w-40 truncate font-mono text-txt" title={event}>
                  {event}
                </span>
                <CompatBar
                  ok={Math.max(0, (s.fired ?? 0) - (s.failed ?? 0))}
                  bad={s.failed ?? 0}
                />
                <span className="text-faint">
                  {t("detail.activity.compatFired", { n: s.fired ?? 0 })}
                  {(s.failed ?? 0) > 0 && (
                    <span className="text-red-500">
                      {" "}
                      · {t("detail.activity.compatFailed", { n: s.failed ?? 0 })}
                    </span>
                  )}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
      {ops.length > 0 && (
        <div>
          <div className="mb-1 font-medium text-muted">{t("detail.activity.compatOps")}</div>
          <div className="space-y-1">
            {ops.map(([op, s]) => (
              <div key={op} className="flex items-center gap-2">
                <span className="w-40 truncate font-mono text-txt" title={op}>
                  {op}
                </span>
                <CompatBar
                  ok={s.ok ?? 0}
                  bad={Math.max(0, (s.called ?? 0) - (s.ok ?? 0))}
                />
                <span className="text-faint">
                  {t("detail.activity.compatCalled", { n: s.called ?? 0, ok: s.ok ?? 0 })}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
      {unimplemented.length > 0 && (
        <div>
          <div className="mb-1 font-medium text-yellow">{t("detail.activity.compatUnimplemented")}</div>
          <div className="flex flex-wrap gap-1">
            {unimplemented.map(([op, n]) => (
              <span
                key={op}
                className="rounded bg-yellow/10 px-1.5 py-px font-mono text-[10px] text-yellow"
              >
                {op}
                {n > 1 ? ` ×${n}` : ""}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}