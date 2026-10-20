"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { ArrowLeft, Download, Loader2, RefreshCw, Search, Store } from "lucide-react";
import * as api from "@/lib/runtime";
import type { MarketItemDetail, MarketPlugin, MarketSource } from "@/lib/runtime";

// Mods 商店抽屉（mod-marketplace-design.md §4/§6.3，2026-10-21 入口定稿：
// 设置 → 连接 → Mods 页签内「浏览 Mods」）。三态视图：列表 / 详情 / （安装
// 经确认模态）。安装前展示 README + hooks 模块预览；danger 级 classic 命令
// 预览在装后首次 grants 确认流程里（M2 简化：安装确认页明示来源与版本）。

export function ModStoreDrawer({
  onClose,
  onInstalled,
}: {
  onClose: () => void;
  /** 安装/更新成功后回调（Mods 页签重拉已装列表并高亮新条目）。 */
  onInstalled: (name: string) => void;
}) {
  const t = useTranslations("settings.mods.store");
  const [loading, setLoading] = useState(true);
  const [sources, setSources] = useState<MarketSource[]>([]);
  const [installed, setInstalled] = useState<Record<string, api.MarketOrigin | null>>({});
  const [activeSrc, setActiveSrc] = useState<string>("");
  const [q, setQ] = useState("");
  const [detail, setDetail] = useState<{ src: string; plugin: MarketPlugin; data: MarketItemDetail } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: "error" | "ok"; text: string } | null>(null);

  const refresh = useCallback(async (force = false) => {
    setLoading(true);
    try {
      const r = await api.listMarketplace(force);
      setSources(r.sources ?? []);
      setInstalled(r.installedOrigins ?? {});
      if (!force) {
        const first = (r.sources ?? []).find((s) => s.enabled && !s.skipped);
        if (first) setActiveSrc((cur) => cur || first.name);
      }
    } catch {
      setMsg({ kind: "error", text: t("loadFailed") });
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const active = sources.find((s) => s.name === activeSrc);
  const plugins = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const rows = active?.plugins ?? [];
    if (!needle) return rows;
    return rows.filter(
      (p) =>
        p.name.toLowerCase().includes(needle) ||
        p.description.toLowerCase().includes(needle) ||
        p.tags.some((x) => x.toLowerCase().includes(needle)),
    );
  }, [active, q]);

  const openDetail = async (src: string, plugin: MarketPlugin) => {
    setBusy(`detail:${plugin.name}`);
    setMsg(null);
    try {
      const data = await api.getMarketplaceItem(src, plugin.source);
      if (data.ok) setDetail({ src, plugin, data });
      else setMsg({ kind: "error", text: data.error || t("loadFailed") });
    } catch {
      setMsg({ kind: "error", text: t("loadFailed") });
    } finally {
      setBusy(null);
    }
  };

  const install = async (src: string, plugin: MarketPlugin) => {
    const origin = installed[plugin.name];
    setBusy(`install:${plugin.name}`);
    setMsg(null);
    try {
      const r = origin
        ? await api.updateFromMarket(plugin.name)
        : await api.installFromMarket(src, plugin.source);
      if (r.ok) {
        setMsg({ kind: "ok", text: t(origin ? "updated" : "installed", { name: plugin.name }) });
        setDetail(null);
        onInstalled(plugin.name);
        void refresh();
      } else {
        setMsg({ kind: "error", text: r.error || t("installFailed") });
      }
    } catch {
      setMsg({ kind: "error", text: t("installFailed") });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/40" onClick={onClose}>
      <div
        className="flex h-full w-[520px] max-w-[92vw] flex-col border-l border-line bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* header */}
        <div className="flex items-center gap-2 border-b border-line px-4 py-3">
          {detail ? (
            <button
              onClick={() => setDetail(null)}
              className="rounded-md p-1 text-muted hover:bg-card2 hover:text-txt"
              title={t("back")}
            >
              <ArrowLeft className="h-4 w-4" />
            </button>
          ) : (
            <Store className="h-4 w-4 text-violet" />
          )}
          <span className="text-sm font-medium text-txt">
            {detail ? detail.plugin.name : t("title")}
          </span>
          <button
            onClick={() => void refresh(true)}
            className="ml-auto rounded-md p-1.5 text-faint hover:bg-card2 hover:text-txt"
            title={t("refresh")}
          >
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          </button>
          <button
            onClick={onClose}
            className="rounded-md px-2 py-1 text-xs text-muted hover:bg-card2 hover:text-txt"
          >
            {t("close")}
          </button>
        </div>

        {msg && (
          <div
            className={`border-b px-4 py-2 text-xs ${
              msg.kind === "error"
                ? "border-red/30 bg-red/10 text-red"
                : "border-green/30 bg-green/10 text-green"
            }`}
          >
            {msg.text}
          </div>
        )}

        {/* body: detail or list */}
        {detail ? (
          <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 text-sm">
            <div className="text-xs text-faint">
              {detail.data.source?.repo}
              {detail.data.commit ? ` @ ${detail.data.commit}` : ""}
            </div>
            <p className="mt-2 leading-relaxed text-muted">
              {detail.plugin.description || detail.data.manifest?.description || t("noDescription")}
            </p>
            <div className="mt-2 flex flex-wrap gap-1.5 text-[10px] text-faint">
              <span className="rounded-full border border-line px-1.5 py-px font-mono">
                v{detail.data.manifest?.version || detail.plugin.version || "?"}
              </span>
              {detail.data.hookModules && detail.data.hookModules.length > 0 && (
                <span className="rounded-full border border-line px-1.5 py-px">
                  {t("jsMod", { n: detail.data.hookModules.length })}
                </span>
              )}
              {(!detail.data.hookModules || detail.data.hookModules.length === 0) && (
                <span className="rounded-full border border-line px-1.5 py-px">
                  {t("classicMod")}
                </span>
              )}
            </div>
            <div className="mt-3 whitespace-pre-wrap break-words rounded-md border border-line bg-base/60 px-3 py-2 text-xs leading-relaxed text-muted">
              {detail.data.readme || t("noReadme")}
            </div>
            <button
              onClick={() => void install(detail.src, detail.plugin)}
              disabled={busy === `install:${detail.plugin.name}`}
              className="mt-4 flex items-center gap-1.5 rounded-md border border-violet/40 bg-violet/10 px-3 py-1.5 text-xs text-violet transition-colors hover:bg-violet/20 disabled:opacity-50"
            >
              {busy === `install:${detail.plugin.name}` ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Download className="h-3.5 w-3.5" />
              )}
              {installed[detail.plugin.name] ? t("update") : t("install")}
            </button>
          </div>
        ) : (
          <div className="flex min-h-0 flex-1 flex-col">
            {/* source tabs + search */}
            <div className="flex items-center gap-1.5 border-b border-line px-3 py-2">
              {sources
                .filter((s) => !s.skipped)
                .map((s) => (
                  <button
                    key={s.name}
                    onClick={() => setActiveSrc(s.name)}
                    className={`rounded-full px-2.5 py-0.5 text-[11px] transition-colors ${
                      s.name === activeSrc
                        ? "bg-violet/15 text-violet"
                        : "text-muted hover:bg-card2"
                    }`}
                    title={s.repo}
                  >
                    {s.name}
                  </button>
                ))}
              <div className="ml-auto flex items-center gap-1.5 rounded-md border border-line px-2 py-1">
                <Search className="h-3 w-3 text-faint" />
                <input
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder={t("search")}
                  className="w-36 bg-transparent text-xs text-txt outline-none placeholder:text-faint"
                />
              </div>
            </div>
            {active?.error && (
              <div className="border-b border-yellow/30 bg-yellow/10 px-4 py-1.5 text-[11px] text-yellow">
                {t("staleCache", { error: active.error })}
              </div>
            )}
            {/* list */}
            <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
              {loading ? (
                <div className="flex justify-center py-10 text-faint">
                  <Loader2 className="h-4 w-4 animate-spin" />
                </div>
              ) : plugins.length === 0 ? (
                <div className="py-10 text-center text-xs text-faint">{t("empty")}</div>
              ) : (
                plugins.map((p) => {
                  const origin = installed[p.name];
                  const newer =
                    origin && p.version && origin.version && p.version !== origin.version;
                  return (
                    <button
                      key={p.name}
                      onClick={() => void openDetail(activeSrc, p)}
                      disabled={busy === `detail:${p.name}`}
                      className="mb-1.5 flex w-full items-center gap-2.5 rounded-lg border border-line bg-base/40 px-3 py-2.5 text-left transition-colors hover:border-violet/40"
                    >
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5">
                          <span className="truncate text-sm font-medium text-txt">{p.name}</span>
                          {p.version && (
                            <span className="shrink-0 font-mono text-[10px] text-faint">
                              v{p.version}
                            </span>
                          )}
                        </div>
                        <div className="truncate text-xs text-muted">
                          {p.description || t("noDescription")}
                        </div>
                      </div>
                      {origin ? (
                        <span
                          className={`shrink-0 rounded-full border px-1.5 py-px text-[10px] ${
                            newer
                              ? "border-violet/40 bg-violet/10 text-violet"
                              : "border-green/40 bg-green/10 text-green"
                          }`}
                        >
                          {newer ? t("hasUpdate", { v: p.version }) : t("installedTag")}
                        </span>
                      ) : (
                        <Download className="h-3.5 w-3.5 shrink-0 text-faint" />
                      )}
                    </button>
                  );
                })
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
