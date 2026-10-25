"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslations } from "next-intl";
import { ArrowLeft, Download, Loader2, Plus, RefreshCw, Search, Settings2, Store, Trash2 } from "lucide-react";
import * as api from "@/lib/runtime";
import type { MarketItemDetail, MarketPlugin, MarketSource } from "@/lib/runtime";

// Mods 商店抽屉（mod-marketplace-design.md §4/§6.3；2026-10-23 起入口在
// 连接器页「Browse Claude Mods」）。三态视图：列表 / 详情 / （安装经确认
// 模态）。安装前展示 README + hooks 模块预览；danger 级 classic 命令预览在
// 装后首次 grants 确认流程里（M2 简化：安装确认页明示来源与版本）。

export function ModStoreDrawer({
  onClose,
  onInstalled,
}: {
  onClose: () => void;
  /** 安装/更新成功后回调（连接器页重拉列表）。 */
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
  // 源管理视图（PUT /api/mods/marketplace/sources 全量覆盖）：列表/详情之外的
  // 第三态。默认源也是普通行——可停用/删除，实现「换成自己的 GitHub 仓库」。
  const [manage, setManage] = useState(false);
  const [newRepo, setNewRepo] = useState("");
  const [newName, setNewName] = useState("");
  const [srcBusy, setSrcBusy] = useState(false);

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

  // ESC 逐级退回：详情 → 管理源 → 清搜索 → 关抽屉。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (detail) setDetail(null);
      else if (manage) setManage(false);
      else if (q) setQ("");
      else onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [detail, manage, q, onClose]);

  // 成功提示 3s 自动消失（错误保留，等用户处理）。
  useEffect(() => {
    if (msg?.kind !== "ok") return;
    const id = setTimeout(() => setMsg(null), 3000);
    return () => clearTimeout(id);
  }, [msg]);

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
      // 后端 marketplace_item 按 NAME 匹配（tests: marketplace_item("…", "alpha")），
      // 传 plugin.source（'./next-steps' 这类路径）必然 not in 源——2026-10-21
      // 安装报错即此。wire 上的 query 字段仍叫 path（API 契约未动）。
      const data = await api.getMarketplaceItem(src, plugin.name);
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
        : await api.installFromMarket(src, plugin.name);
      if (r.ok) {
        // 装完即回 Mods 页签：onInstalled 高亮新条目，抽屉直接关——少一次
        // 「装好了→手动关抽屉→找条目」的绕路（2026-10-21 体验反馈）。
        setDetail(null);
        onInstalled(plugin.name);
        onClose();
        return;
      } else {
        setMsg({ kind: "error", text: r.error || t("installFailed") });
      }
    } catch {
      setMsg({ kind: "error", text: t("installFailed") });
    } finally {
      setBusy(null);
    }
  };

  // ---- 源管理 ---------------------------------------------------------------
  /** 后端 save_sources 只取 name/repo/enabled 三元组；列表态的 plugins/
   * skipped 等读路径字段发回去也会被清掉，但显式瘦身更稳。 */
  const persistSources = async (next: Array<Pick<MarketSource, "name" | "repo" | "enabled">>): Promise<boolean> => {
    setSrcBusy(true);
    setMsg(null);
    try {
      await api.putMarketplaceSources(next.map((s) => ({ name: s.name, repo: s.repo, enabled: s.enabled })));
      await refresh(true);
      return true;
    } catch {
      setMsg({ kind: "error", text: t("saveFailed") });
      return false;
    } finally {
      setSrcBusy(false);
    }
  };

  const slugOf = (repo: string): string => {
    const tail = repo.replace(/\/+$/, "").split("/").pop() || "market";
    return (tail.replace(/\.git$/, "") || "market").slice(0, 64);
  };

  const addSource = async () => {
    const repo = newRepo.trim();
    // 只收 git 可克隆地址（https / git@）；重复仓库直接拒绝。
    if (!/^(https:\/\/[^\s]+|git@[^\s:]+:[^\s]+)$/.test(repo) || sources.some((s) => s.repo === repo)) {
      setMsg({ kind: "error", text: t("addFailed") });
      return;
    }
    const name = (newName.trim() || slugOf(repo)).slice(0, 64);
    const ok = await persistSources([
      ...sources.map((s) => ({ name: s.name, repo: s.repo, enabled: s.enabled })),
      { name, repo, enabled: true },
    ]);
    if (ok) {
      setNewRepo("");
      setNewName("");
      setManage(false);
      setActiveSrc(name);
    }
  };

  const toggleSource = async (s: MarketSource) => {
    await persistSources(sources.map((x) => (x.name === s.name ? { ...x, enabled: !s.enabled } : x)));
  };

  const removeSource = async (s: MarketSource) => {
    const rest = sources.filter((x) => x.name !== s.name);
    const ok = await persistSources(rest);
    if (ok && activeSrc === s.name) setActiveSrc(rest[0]?.name ?? "");
  };

  // Portal 到 body：此前抽屉渲染在设置页内容树里，fixed 蒙版被祖先层叠
  // 上下文困住，顶端/侧栏漏出不蒙层的亮色（2026-10-21「漏白」）。挂到
  // body + z-[60]，蒙层必然盖住整个视口与应用 chrome。
  return createPortal(
    <div className="fixed inset-0 z-[60] flex justify-end bg-black/40" onClick={onClose}>
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
            {detail ? detail.plugin.name : manage ? t("manageTitle") : t("title")}
          </span>
          {!detail && (
            <button
              onClick={() => setManage((m) => !m)}
              className={`ml-auto rounded-md p-1.5 transition-colors hover:bg-card2 ${
                manage ? "text-violet" : "text-faint hover:text-txt"
              }`}
              title={t("manage")}
            >
              <Settings2 className="h-4 w-4" />
            </button>
          )}
          <button
            onClick={() => void refresh(true)}
            className={`${detail ? "ml-auto" : ""} rounded-md p-1.5 text-faint hover:bg-card2 hover:text-txt`}
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
            className={`selectable border-b px-4 py-2 text-xs ${
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
            <p className="selectable mt-2 leading-relaxed text-muted">
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
            <div className="selectable mt-3 whitespace-pre-wrap break-words rounded-md border border-line bg-base/60 px-3 py-2 text-xs leading-relaxed text-muted">
              {detail.data.readme || t("noReadme")}
            </div>
            <button
              onClick={() => void install(detail.src, detail.plugin)}
              disabled={busy === `install:${detail.plugin.name}`}
              className="mt-4 flex items-center gap-1.5 rounded-md border border-violet/40 bg-violet/10 px-3 py-1.5 text-xs text-violet transition-colors hover:bg-violet/20 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy === `install:${detail.plugin.name}` ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Download className="h-3.5 w-3.5" />
              )}
              {installed[detail.plugin.name] ? t("update") : t("install")}
            </button>
          </div>
        ) : manage ? (
          <div className="flex min-h-0 flex-1 flex-col">
            {/* 源列表：默认源也只是普通行——停用/删除即可换成自己的仓库 */}
            <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
              {sources.length === 0 && (
                <div className="py-6 text-center text-xs text-faint">
                  {t("emptySources")}
                  <div className="mt-1 text-[10px]">{t("emptyReset")}</div>
                </div>
              )}
              {sources.map((s) => (
                <div
                  key={s.name}
                  className="mb-1.5 flex items-center gap-2 rounded-lg border border-line bg-base/40 px-3 py-2"
                >
                  <div className="min-w-0 flex-1">
                    <div className={`truncate text-sm ${s.enabled ? "text-txt" : "text-faint"}`}>
                      {s.name}
                      {!s.enabled && <span className="ml-1.5 text-[10px] text-faint">({t("disabledTag")})</span>}
                    </div>
                    <div className="truncate font-mono text-[10px] text-faint" title={s.repo}>
                      {s.repo}
                    </div>
                  </div>
                  <button
                    onClick={() => void toggleSource(s)}
                    disabled={srcBusy}
                    className={`shrink-0 rounded-full border px-1.5 py-px text-[10px] transition-colors disabled:opacity-50 ${
                      s.enabled
                        ? "border-violet/40 bg-violet/10 text-violet"
                        : "border-line text-faint hover:text-txt"
                    }`}
                  >
                    {s.enabled ? t("enabled") : t("enable")}
                  </button>
                  <button
                    onClick={() => void removeSource(s)}
                    disabled={srcBusy}
                    title={t("remove")}
                    className="shrink-0 rounded-md p-1 text-faint transition-colors hover:bg-red/10 hover:text-red disabled:opacity-50"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              ))}
            </div>
            {/* 添加源：GitHub 地址（https / git@），名称缺省取仓库名 */}
            <div className="border-t border-line px-4 py-3">
              <div className="flex items-center gap-1.5">
                <input
                  value={newRepo}
                  onChange={(e) => setNewRepo(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && void addSource()}
                  placeholder={t("repoPlaceholder")}
                  className="min-w-0 flex-1 rounded-md border border-line bg-base/60 px-2 py-1.5 font-mono text-xs text-txt outline-none placeholder:text-faint focus:border-violet/40"
                />
                <input
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && void addSource()}
                  placeholder={t("namePlaceholder")}
                  className="w-28 rounded-md border border-line bg-base/60 px-2 py-1.5 text-xs text-txt outline-none placeholder:text-faint focus:border-violet/40"
                />
                <button
                  onClick={() => void addSource()}
                  disabled={srcBusy || !newRepo.trim()}
                  className="flex shrink-0 items-center gap-1 rounded-md border border-violet/40 bg-violet/10 px-2.5 py-1.5 text-xs text-violet transition-colors hover:bg-violet/20 disabled:opacity-50"
                >
                  {srcBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
                  {t("add")}
                </button>
              </div>
            </div>
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
              <div className="selectable border-b border-yellow/30 bg-yellow/10 px-4 py-1.5 text-[11px] text-yellow">
                {t("staleCache", { error: active.error })}
              </div>
            )}
            {/* list */}
            <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
              {loading ? (
                <div className="flex items-center justify-center gap-2 py-10 text-faint">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  <span className="text-xs">{t("pulling")}</span>
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
    </div>,
    document.body,
  );
}
