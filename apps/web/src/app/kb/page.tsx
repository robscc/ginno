"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import * as api from "@/lib/runtime";
import type {
  WikiDiscover,
  WikiPage,
  WikiRelatedItem,
  WikiSearchResult,
  WikiStats,
} from "@/lib/types";
import { BookOpen, FileText, Hammer, RefreshCw, Search, Sparkles, Tag, Network } from "lucide-react";
import { GraphView } from "@/components/kb/GraphView";
import { PageViewer, type ViewTarget } from "@/components/kb/PageViewer";

type View = "search" | "all" | "discover" | "graph";

function timeAgo(ts?: number): string {
  if (!ts) return "never";
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function TagPills({ tags }: { tags: string[] }) {
  if (!tags.length) return null;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {tags.map((t) => (
        <span key={t} className="pill border border-line2 text-faint">
          <Tag className="mr-0.5 inline h-2.5 w-2.5" />
          {t}
        </span>
      ))}
    </span>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-line bg-card p-4">
      <div className="mb-2 text-sm font-medium text-txt">{title}</div>
      {children}
    </div>
  );
}

function PairRow({ a, b, score, type }: { a: string; b: string; score: number; type?: string }) {
  return (
    <div className="flex items-center gap-2 py-1 text-sm">
      <span className="text-txt">{a}</span>
      <span className="text-faint">↔</span>
      <span className="text-txt">{b}</span>
      <span className="ml-auto text-xs font-semibold text-violet">{Math.round(score * 100)}%</span>
      {type && <span className="pill border border-line2 text-faint">{type}</span>}
    </div>
  );
}

export default function KnowledgeBasePage() {
  const [stats, setStats] = useState<WikiStats | null>(null);
  const [pages, setPages] = useState<WikiPage[]>([]);
  const [results, setResults] = useState<WikiSearchResult[]>([]);
  const [query, setQuery] = useState("");
  const [searched, setSearched] = useState(false);
  const [view, setView] = useState<View>("all");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string>("");
  const [connError, setConnError] = useState<string>("");
  const [discover, setDiscover] = useState<WikiDiscover | null>(null);
  const [discoverError, setDiscoverError] = useState<string>("");
  const [relatedQuery, setRelatedQuery] = useState("");
  const [related, setRelated] = useState<WikiRelatedItem[] | null>(null);
  const [importPath, setImportPath] = useState("");
  const [importProbe, setImportProbe] = useState<string>("");
  const [importBusy, setImportBusy] = useState(false);
  const [openTarget, setOpenTarget] = useState<ViewTarget | null>(null);

  const configured = !!stats?.ok;

  const loadAll = useCallback(async () => {
    try {
      const [st, pg] = await Promise.all([api.kbWikiStats(), api.kbWikiList()]);
      setStats(st);
      if (pg.ok) setPages(pg.pages);
      setConnError("");
    } catch {
      setConnError("运行时未连接：请确认 sidecar 已启动（dev: pnpm dev:runtime）。");
    }
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  useEffect(() => {
    if (view === "discover" && configured) {
      setDiscover(null);
      setDiscoverError("");
      setRelated(null);
      api
        .kbWikiDiscover()
        .then((d) => {
          if (d.ok) setDiscover(d);
          else setDiscoverError("加载发现结果失败");
        })
        .catch(() => setDiscoverError("运行时未连接，无法加载发现结果。"));
    }
  }, [view, configured]);

  async function onSearch() {
    const q = query.trim();
    if (!q) return;
    setBusy(true);
    setNote("");
    try {
      const tag = q.startsWith("tag:") ? q.slice(4).trim() : "";
      const r = tag ? await api.kbWikiSearchByTag(tag) : await api.kbWikiSearch(q);
      if (r.ok) {
        setResults(r.results);
        setSearched(true);
        setView("search");
      } else {
        setNote(r.error || "检索失败");
      }
    } catch {
      setNote("检索失败：无法连接运行时");
    } finally {
      setBusy(false);
    }
  }

  async function onReindex() {
    setBusy(true);
    setNote("");
    try {
      const r = await api.kbWikiReindex();
      if (r.ok) {
        await loadAll();
        setNote(`索引已重建（${r.indexed} 页）。`);
      } else {
        setNote("重建索引失败");
      }
    } catch {
      setNote("重建索引失败：无法连接运行时");
    } finally {
      setBusy(false);
    }
  }

  async function onBuild() {
    setBusy(true);
    setNote("");
    try {
      const r = await api.kbWikiBuild();
      if (r.ok) {
        setNote(
          `编译完成：扫描 ${r.scanned ?? 0} 篇，新建 ${(r.created || []).length}，更新 ${(r.updated || []).length}，自动关联 ${(r.new_links || []).length}，用时 ${r.duration_ms ?? 0}ms`,
        );
        await loadAll();
      } else {
        setNote(r.error || "编译失败");
      }
    } catch {
      setNote("编译失败：无法连接运行时");
    } finally {
      setBusy(false);
    }
  }

  async function onRelated() {
    if (!relatedQuery.trim()) return;
    try {
      const r = await api.kbWikiRelated(relatedQuery.trim());
      setRelated(r.ok ? r.related : []);
    } catch {
      setRelated([]);
      setNote("相关查询失败：无法连接运行时");
    }
  }

  async function onDetectImport() {
    setImportProbe("");
    if (!importPath.trim()) {
      setImportProbe("请填写 vault 路径");
      return;
    }
    try {
      const r = await api.kbWikiProbe(importPath.trim());
      setImportProbe(
        r.ok
          ? r.detected?.namespace
            ? `检测到命名空间「${r.detected.namespace}」：Wiki ${r.wiki_pages} 页 / Raw ${r.raw_pages} 篇${r.has_index ? "（含 INDEX）" : ""}`
            : `未检测到 */Wiki 目录，将把整个 vault 作为知识库索引（共 ${r.total_md} 篇）`
          : r.error || "检测失败",
      );
    } catch {
      setImportProbe("检测失败：无法连接运行时");
    }
  }

  async function onImport() {
    if (!importPath.trim()) {
      setImportProbe("请填写 vault 路径");
      return;
    }
    setImportBusy(true);
    setImportProbe("");
    try {
      const probe = await api.kbWikiProbe(importPath.trim());
      if (!probe.ok) {
        setImportProbe(probe.error || "检测失败：路径无效");
        return;
      }
      const d = probe.detected;
      const saved = await api.kbWikiPutConfig({
        enabled: true,
        vault_path: importPath.trim(),
        wiki_dir: d?.wiki_dir || "",
        raw_dir: d?.raw_dir || "",
        auto_inject: true,
        inject_top_k: 5,
        inject_min_score: 0.3,
        rescan_interval_s: 60,
      });
      if (!saved.ok) {
        setImportProbe("保存配置失败");
        return;
      }
      const ix = await api.kbWikiReindex();
      setImportProbe(ix.ok ? `已导入并索引 ${ix.indexed} 页` : "已保存配置，但索引失败");
      await loadAll();
    } catch {
      setImportProbe("导入失败：无法连接运行时");
    } finally {
      setImportBusy(false);
    }
  }

  // Open a wikilink target: resolve to an existing page (by path or title) or
  // fall back to a create-stub when the note doesn't exist yet.
  function openByRef(t: string) {
    const lp = t.toLowerCase();
    const hit = pages.find((p) => p.path.toLowerCase() === lp) || pages.find((p) => p.title.toLowerCase() === lp);
    setOpenTarget(hit ? { path: hit.path } : { title: t });
  }

  const tabs: { id: View; label: string; icon?: typeof Network }[] = [
    { id: "search", label: `搜索结果${searched ? ` (${results.length})` : ""}` },
    { id: "all", label: `全部页面 (${pages.length})` },
    { id: "discover", label: "发现" },
    { id: "graph", label: `图谱 (${pages.length})`, icon: Network },
  ];

  return (
    <div className="flex min-w-0 flex-1 flex-col px-8 py-7">
      {/* header */}
      <div className="flex items-center gap-2">
        <BookOpen className="h-5 w-5 text-violet" />
        <h2 className="text-lg font-semibold text-txt">Knowledge Base</h2>
        {configured && (
          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={onBuild}
              disabled={busy}
              className="flex items-center gap-1.5 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
            >
              <Hammer className={`h-3.5 w-3.5 ${busy ? "animate-pulse" : ""}`} />
              Build wiki
            </button>
            <button
              onClick={onReindex}
              disabled={busy}
              className="flex items-center gap-1.5 rounded-lg border border-line bg-card px-3 py-1.5 text-xs text-muted hover:text-txt disabled:opacity-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} />
              Rebuild index
            </button>
          </div>
        )}
      </div>
      <p className="mt-1 text-sm text-muted">
        Obsidian 知识库：把 Raw 编译成 Wiki、按相关性检索并在对话中自动注入（LLMWiki）。支持页面预览、wikilink 跳转/创建与图谱。
      </p>
      {note && <div className="mt-2 text-xs text-violet">{note}</div>}
      {connError && (
        <div className="mt-2 rounded-lg border border-red/40 bg-red/10 px-3 py-2 text-xs text-red">{connError}</div>
      )}

      {/* not configured → import panel */}
      {!configured && (
        <div className="mt-6 max-w-2xl rounded-xl border border-line bg-card p-4">
          <div className="mb-2 flex items-center gap-2 text-sm font-medium text-txt">
            <BookOpen className="h-4 w-4 text-violet" /> 导入已有的 LLM Wiki 知识库
          </div>
          <p className="mb-3 text-xs text-muted">
            指向你的 Obsidian vault。若已编译好 Wiki（如 <code className="text-txt">Molly/Wiki</code>），会被直接索引、无需重新编译。
          </p>
          <div className="flex gap-2">
            <input
              className="field flex-1"
              placeholder="/Users/…/Documents/Obsidian Vault"
              value={importPath}
              onChange={(e) => setImportPath(e.target.value)}
            />
            <button
              onClick={onDetectImport}
              disabled={importBusy}
              className="flex items-center gap-1.5 rounded-lg border border-line2 px-3 text-xs text-muted hover:text-txt disabled:opacity-50"
            >
              <Search className="h-3.5 w-3.5" /> 检测
            </button>
            <button
              onClick={onImport}
              disabled={importBusy}
              className="flex items-center gap-1.5 rounded-lg bg-violet px-3 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
            >
              <Hammer className="h-3.5 w-3.5" /> 导入并索引
            </button>
          </div>
          {importProbe && <div className="mt-2 text-xs text-violet">{importProbe}</div>}
          <div className="mt-3 text-xs text-faint">
            需要细调（top-K / 自动注入 / 目录）？去{" "}
            <Link href="/settings/knowledge" className="text-violet hover:underline">
              设置 → 知识库
            </Link>
            。
            {stats?.error ? <span className="ml-1">{stats.error}</span> : null}
          </div>
        </div>
      )}

      {configured && (
        <>
          {/* stats bar */}
          <div className="mt-4 flex flex-wrap items-center gap-2 text-xs text-faint">
            <span className="pill border border-line2 text-muted">{stats?.total_pages ?? 0} pages</span>
            <span className="pill border border-line2 text-muted">{stats?.total_links ?? 0} links</span>
            <span className="pill border border-line2 text-muted">{stats?.total_tags ?? 0} tags</span>
            <span className="pill border border-line2 text-muted">indexed {timeAgo(stats?.last_indexed)}</span>
            <span className="pill border border-line2 text-faint">{stats?.vault_path}</span>
          </div>

          {/* search */}
          <div className="mt-4 flex max-w-2xl gap-2">
            <div className="relative flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
              <input
                className="field w-full pl-9"
                placeholder="搜索知识…（支持中英文）"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && onSearch()}
              />
            </div>
            <button
              onClick={onSearch}
              disabled={busy || !query.trim()}
              className="rounded-lg bg-violet px-4 text-sm font-medium text-white hover:opacity-90 disabled:opacity-40"
            >
              Search
            </button>
          </div>

          {/* tag cloud */}
          {!!stats?.unique_tags?.length && (
            <div className="mt-3 flex max-w-2xl flex-wrap gap-1.5">
              {stats.unique_tags.slice(0, 20).map((t) => (
                <button
                  key={t}
                  onClick={async () => {
                    const r = await api.kbWikiSearchByTag(t);
                    if (r.ok) {
                      setResults(r.results);
                      setSearched(true);
                      setView("search");
                      setQuery(`tag:${t}`);
                    }
                  }}
                  className="pill border border-line text-faint hover:border-line2 hover:text-muted"
                >
                  #{t}
                </button>
              ))}
            </div>
          )}

          {/* tabs */}
          <div className="mt-5 flex gap-1 border-b border-line">
            {tabs.map((t) => {
              const Ic = t.icon;
              return (
                <button
                  key={t.id}
                  onClick={() => setView(t.id)}
                  className={`flex items-center gap-1 px-3 py-2 text-sm font-medium transition-colors ${
                    view === t.id ? "border-b-2 border-violet text-txt" : "text-muted hover:text-txt"
                  }`}
                >
                  {Ic && <Ic className="h-3.5 w-3.5" />}
                  {t.label}
                </button>
              );
            })}
          </div>

          {/* two-pane: list/graph on the left, page inspector on the right */}
          <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_460px]">
            <div className="min-w-0 space-y-3">
              {view === "graph" && (
                <GraphView pages={pages} selected={openTarget?.path ?? null} onSelect={(p) => setOpenTarget({ path: p })} />
              )}

              {view === "search" &&
                (searched ? (
                  results.length === 0 ? (
                    <div className="text-sm text-faint">没有匹配的条目。</div>
                  ) : (
                    results.map((r, i) => (
                      <button
                        key={i}
                        onClick={() => setOpenTarget({ path: r.path })}
                        className="block w-full rounded-xl border border-line bg-card p-4 text-left transition-colors hover:border-line2"
                      >
                        <div className="flex items-center gap-2">
                          <FileText className="h-4 w-4 shrink-0 text-violet" />
                          <span className="font-medium text-txt">{r.title}</span>
                          <span className="ml-auto text-xs font-semibold text-violet">{Math.round(r.score * 100)}%</span>
                        </div>
                        <div className="mt-1.5 flex items-center gap-2">
                          <TagPills tags={r.tags} />
                        </div>
                        <div className="mt-2 text-xs text-faint">来源: {r.path}</div>
                        {r.matched_terms.length > 0 && (
                          <div className="mt-1 text-[11px] text-faint">命中: {r.matched_terms.slice(0, 8).join(" · ")}</div>
                        )}
                        {r.summary && <p className="mt-2 text-sm leading-relaxed text-muted">{r.summary}</p>}
                      </button>
                    ))
                  )
                ) : (
                  <div className="text-sm text-faint">输入关键词开始检索。</div>
                ))}

              {view === "all" &&
                (pages.length === 0 ? (
                  <div className="text-sm text-faint">还没有索引到任何页面（先点 Build wiki 编译 Raw）。</div>
                ) : (
                  <div className="overflow-hidden rounded-xl border border-line">
                    {pages.map((p, i) => (
                      <button
                        key={i}
                        onClick={() => setOpenTarget({ path: p.path })}
                        className={`flex w-full items-center gap-2 px-3 py-2 text-left transition-all hover:translate-x-0.5 hover:bg-card2/50 ${
                          openTarget?.path === p.path ? "bg-card2/60 shadow-[inset_2px_0_0_0_#8b5cf6]" : ""
                        }`}
                      >
                        <FileText className="h-4 w-4 shrink-0 text-muted" />
                        <span className="truncate text-sm text-txt">{p.title}</span>
                        <TagPills tags={p.tags} />
                        <span className="ml-auto truncate text-[11px] text-faint">{p.path}</span>
                      </button>
                    ))}
                  </div>
                ))}

              {view === "discover" &&
                (discover ? (
                  <>
                    <div className="flex items-center gap-2 text-xs text-faint">
                      <Sparkles className="h-3.5 w-3.5 text-violet" />
                      {discover.stats?.pages ?? 0} 页 · {discover.stats?.edges ?? 0} 条关联边
                    </div>
                    <Section title="查看某页的相关">
                      <div className="flex gap-2">
                        <input
                          className="field flex-1"
                          placeholder="页面标题，如 权限节点"
                          value={relatedQuery}
                          onChange={(e) => setRelatedQuery(e.target.value)}
                          onKeyDown={(e) => e.key === "Enter" && onRelated()}
                        />
                        <button
                          onClick={onRelated}
                          className="rounded-lg border border-line2 px-3 text-xs text-muted hover:text-txt"
                        >
                          相关
                        </button>
                      </div>
                      {related &&
                        (related.length === 0 ? (
                          <div className="mt-2 text-xs text-faint">没有相关页（或标题不匹配）。</div>
                        ) : (
                          <div className="mt-2">
                            {related.map((r, i) => (
                              <PairRow key={i} a={relatedQuery} b={r.title} score={r.score} type={r.type} />
                            ))}
                          </div>
                        ))}
                    </Section>
                    <Section title={`强关联 (≥80%) · ${discover.strong.length}`}>
                      {discover.strong.length === 0 ? (
                        <div className="text-xs text-faint">无。</div>
                      ) : (
                        discover.strong.map((p, i) => <PairRow key={i} a={p.a} b={p.b} score={p.score} type={p.type} />)
                      )}
                    </Section>
                    <Section title={`聚类 · ${discover.clusters.length}`}>
                      {discover.clusters.length === 0 ? (
                        <div className="text-xs text-faint">无显著聚类。</div>
                      ) : (
                        discover.clusters.map((c, i) => (
                          <div key={i} className="py-1">
                            <div className="text-sm text-txt">
                              {c.label} <span className="text-faint">· 密度 {c.density}</span>
                            </div>
                            <div className="mt-0.5 flex flex-wrap gap-1">
                              {c.members.map((m) => (
                                <span key={m} className="pill border border-line2 text-faint">
                                  {m}
                                </span>
                              ))}
                            </div>
                          </div>
                        ))
                      )}
                    </Section>
                    <Section title={`可合并候选 · ${discover.merge_candidates.length}`}>
                      {discover.merge_candidates.length === 0 ? (
                        <div className="text-xs text-faint">无。</div>
                      ) : (
                        discover.merge_candidates.map((p, i) => <PairRow key={i} a={p.a} b={p.b} score={p.score} />)
                      )}
                    </Section>
                    <Section title={`孤立页（无入链）· ${discover.isolated.length}`}>
                      {discover.isolated.length === 0 ? (
                        <div className="text-xs text-faint">无。</div>
                      ) : (
                        <div className="flex flex-wrap gap-1">
                          {discover.isolated.map((t) => (
                            <button
                              key={t}
                              onClick={() => openByRef(t)}
                              className="pill border border-line2 text-faint hover:border-line2 hover:text-muted"
                            >
                              {t}
                            </button>
                          ))}
                        </div>
                      )}
                    </Section>
                  </>
                ) : discoverError ? (
                  <div className="text-sm text-red">{discoverError}</div>
                ) : (
                  <div className="text-sm text-faint">加载发现结果中…</div>
                ))}
            </div>

            {/* right inspector: preview / edit / create */}
            <div className="lg:sticky lg:top-2 h-[78vh]">
              <PageViewer
                target={openTarget}
                onNavigate={openByRef}
                onSaved={() => loadAll()}
                onClose={() => setOpenTarget(null)}
              />
            </div>
          </div>
        </>
      )}
    </div>
  );
}
