"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import { useGinno } from "@/lib/store";
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
  // kb 域 catalog（messages/{en,zh-CN}/kb.json）。翻译函数命名 tKb：本文件多处
  // `.map((t) => …)` 回调形参会遮蔽 `t`（已知坑），域前缀名彻底避开。
  const tKb = useTranslations("kb");
  const g = useGinno(); // Build wiki：新建并切换到「📚 Wiki 编译」会话
  const [stats, setStats] = useState<WikiStats | null>(null);
  const [pages, setPages] = useState<WikiPage[]>([]);
  const [results, setResults] = useState<WikiSearchResult[]>([]);
  const [query, setQuery] = useState("");
  const [searched, setSearched] = useState(false);
  const [view, setView] = useState<View>("all");
  // 「仅记忆来源」过滤：由全局记忆沉淀的页面（frontmatter type: memory）。
  const [onlyMemory, setOnlyMemory] = useState(false);
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

  // 相对时间（原模块级 timeAgo 迁入组件：文案走 kb.time catalog）。
  function timeAgo(ts?: number): string {
    if (!ts) return tKb("time.never");
    const s = Math.floor(Date.now() / 1000 - ts);
    if (s < 60) return tKb("time.seconds", { n: s });
    if (s < 3600) return tKb("time.minutes", { n: Math.floor(s / 60) });
    if (s < 86400) return tKb("time.hours", { n: Math.floor(s / 3600) });
    return tKb("time.days", { n: Math.floor(s / 86400) });
  }

  const loadAll = useCallback(async () => {
    try {
      const [st, pg] = await Promise.all([api.kbWikiStats(), api.kbWikiList()]);
      setStats(st);
      if (pg.ok) setPages(pg.pages);
      setConnError("");
    } catch {
      setConnError(tKb("error.notConnected"));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // Deep link from chat citations (SourcesBlock wiki rows): /kb?page=<path>.
  // window.location rather than useSearchParams — /kb is statically exported
  // and useSearchParams would force a Suspense boundary just for one param.
  // Resolves once the list lands; a path that no longer exists (page deleted
  // after the citation) is silently ignored.
  useEffect(() => {
    const q = new URLSearchParams(window.location.search).get("page");
    if (!q || !pages.length) return;
    if (pages.some((p) => p.path === q)) setOpenTarget({ path: q });
  }, [pages]);

  useEffect(() => {
    if (view === "discover" && configured) {
      setDiscover(null);
      setDiscoverError("");
      setRelated(null);
      api
        .kbWikiDiscover()
        .then((d) => {
          if (d.ok) setDiscover(d);
          else setDiscoverError(tKb("discover.loadFailed"));
        })
        .catch(() => setDiscoverError(tKb("discover.notConnected")));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
        setNote(r.error || tKb("search.failed"));
      }
    } catch {
      setNote(tKb("search.failedNoRuntime"));
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
        setNote(tKb("reindex.done", { count: r.indexed }));
      } else {
        setNote(tKb("reindex.failed"));
      }
    } catch {
      setNote(tKb("reindex.failedNoRuntime"));
    } finally {
      setBusy(false);
    }
  }

  async function onBuild() {
    setBusy(true);
    setNote("");
    try {
      // agent-wiki-workflow-design.md:Build = 打开可见会话运行「📚 Wiki 编译」
      // workflow(agent 扇出综合写页);run 绑定到该会话,run.* 事件在聊天里可见。
      const s = await g.newSession(undefined, {
        title: tKb("build.sessionTitle"),
        workflow_id: "wiki-compile",
      });
      if (!s?.id) {
        setNote(tKb("build.failed"));
        return;
      }
      const r = await api.triggerWorkflowRun("wiki-compile", undefined, s.id);
      if (!r.ok) {
        setNote(tKb("build.failed"));
        return;
      }
      setNote(tKb("build.started"));
    } catch {
      setNote(tKb("build.failedNoRuntime"));
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
      setNote(tKb("discover.relatedFailedNoRuntime"));
    }
  }

  async function onDetectImport() {
    setImportProbe("");
    if (!importPath.trim()) {
      setImportProbe(tKb("import.needPath"));
      return;
    }
    try {
      const r = await api.kbWikiProbe(importPath.trim());
      setImportProbe(
        r.ok
          ? r.detected?.namespace
            ? tKb("import.detected", {
                ns: r.detected.namespace,
                wiki: r.wiki_pages ?? 0,
                raw: r.raw_pages ?? 0,
                hasIndex: r.has_index ? "yes" : "no",
              })
            : tKb("import.noWikiDir", { total: r.total_md ?? 0 })
          : r.error || tKb("import.detectFailed"),
      );
    } catch {
      setImportProbe(tKb("import.detectFailedNoRuntime"));
    }
  }

  async function onImport() {
    if (!importPath.trim()) {
      setImportProbe(tKb("import.needPath"));
      return;
    }
    setImportBusy(true);
    setImportProbe("");
    try {
      const probe = await api.kbWikiProbe(importPath.trim());
      if (!probe.ok) {
        setImportProbe(probe.error || tKb("import.invalidPath"));
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
        setImportProbe(tKb("import.saveFailed"));
        return;
      }
      const ix = await api.kbWikiReindex();
      setImportProbe(ix.ok ? tKb("import.imported", { count: ix.indexed }) : tKb("import.indexFailed"));
      await loadAll();
    } catch {
      setImportProbe(tKb("import.importFailedNoRuntime"));
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
    { id: "search", label: tKb("tabs.searchResults", { count: results.length }) },
    { id: "all", label: tKb("tabs.allPages", { count: pages.length }) },
    { id: "discover", label: tKb("tabs.discover") },
    { id: "graph", label: tKb("tabs.graph", { count: pages.length }), icon: Network },
  ];

  return (
    <div className="flex min-w-0 flex-1 flex-col px-8 py-7">
      {/* header */}
      <div className="flex items-center gap-2">
        <BookOpen className="h-5 w-5 text-violet" />
        <h2 className="text-lg font-semibold text-txt">{tKb("title")}</h2>
        {configured && (
          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={onBuild}
              disabled={busy}
              className="flex items-center gap-1.5 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
            >
              <Hammer className={`h-3.5 w-3.5 ${busy ? "animate-pulse" : ""}`} />
              {tKb("actions.build")}
            </button>
            <button
              onClick={onReindex}
              disabled={busy}
              className="flex items-center gap-1.5 rounded-lg border border-line bg-card px-3 py-1.5 text-xs text-muted hover:text-txt disabled:opacity-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} />
              {tKb("actions.reindex")}
            </button>
          </div>
        )}
      </div>
      <p className="mt-1 text-sm text-muted">{tKb("subtitle")}</p>
      {note && <div className="mt-2 text-xs text-violet">{note}</div>}
      {connError && (
        <div className="mt-2 rounded-lg border border-red/40 bg-red/10 px-3 py-2 text-xs text-red">{connError}</div>
      )}

      {/* not configured → import panel */}
      {!configured && (
        <div className="mt-6 max-w-2xl rounded-xl border border-line bg-card p-4">
          <div className="mb-2 flex items-center gap-2 text-sm font-medium text-txt">
            <BookOpen className="h-4 w-4 text-violet" /> {tKb("import.title")}
          </div>
          <p className="mb-3 text-xs text-muted">
            {tKb.rich("import.hint", {
              code: (chunks) => <code className="text-txt">{chunks}</code>,
            })}
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
              <Search className="h-3.5 w-3.5" /> {tKb("import.detect")}
            </button>
            <button
              onClick={onImport}
              disabled={importBusy}
              className="flex items-center gap-1.5 rounded-lg bg-violet px-3 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
            >
              <Hammer className="h-3.5 w-3.5" /> {tKb("import.importAndIndex")}
            </button>
          </div>
          {importProbe && <div className="mt-2 text-xs text-violet">{importProbe}</div>}
          <div className="mt-3 text-xs text-faint">
            {tKb.rich("import.settingsHint", {
              link: (chunks) => (
                <Link href="/settings/knowledge" className="text-violet hover:underline">
                  {chunks}
                </Link>
              ),
            })}
            {stats?.error ? <span className="ml-1">{stats.error}</span> : null}
          </div>
        </div>
      )}

      {configured && (
        <>
          {/* stats bar */}
          <div className="mt-4 flex flex-wrap items-center gap-2 text-xs text-faint">
            <span className="pill border border-line2 text-muted">{tKb("stats.pages", { count: stats?.total_pages ?? 0 })}</span>
            <span className="pill border border-line2 text-muted">{tKb("stats.links", { count: stats?.total_links ?? 0 })}</span>
            <span className="pill border border-line2 text-muted">{tKb("stats.tags", { count: stats?.total_tags ?? 0 })}</span>
            <span className="pill border border-line2 text-muted">{tKb("stats.indexed", { time: timeAgo(stats?.last_indexed) })}</span>
            <span className="pill border border-line2 text-faint">{stats?.vault_path}</span>
          </div>

          {/* search */}
          <div className="mt-4 flex max-w-2xl gap-2">
            <div className="relative flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
              <input
                className="field w-full pl-9"
                placeholder={tKb("search.placeholder")}
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
              {tKb("search.button")}
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
                    <div className="text-sm text-faint">{tKb("search.noResults")}</div>
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
                        <div className="mt-2 text-xs text-faint">{tKb("search.source", { path: r.path })}</div>
                        {r.matched_terms.length > 0 && (
                          <div className="mt-1 text-[11px] text-faint">
                            {tKb("search.matches", { terms: r.matched_terms.slice(0, 8).join(" · ") })}
                          </div>
                        )}
                        {r.summary && <p className="mt-2 text-sm leading-relaxed text-muted">{r.summary}</p>}
                      </button>
                    ))
                  )
                ) : (
                  <div className="text-sm text-faint">{tKb("search.startHint")}</div>
                ))}

              {view === "all" &&
                (pages.length === 0 ? (
                  <div className="text-sm text-faint">{tKb("all.empty")}</div>
                ) : (
                  <>
                    {pages.some((p) => p.type === "memory") && (
                      <button
                        onClick={() => setOnlyMemory((v) => !v)}
                        className={`pill border text-[11px] ${
                          onlyMemory
                            ? "border-violet/60 bg-violet/20 text-violet"
                            : "border-line2 text-faint hover:text-txt"
                        }`}
                      >
                        {tKb("all.memoryOnly")}
                      </button>
                    )}
                  <div className="overflow-hidden rounded-xl border border-line">
                    {pages.filter((p) => !onlyMemory || p.type === "memory").map((p, i) => (
                      <button
                        key={i}
                        onClick={() => setOpenTarget({ path: p.path })}
                        className={`flex w-full items-center gap-2 px-3 py-2 text-left transition-all hover:translate-x-0.5 hover:bg-card2/50 ${
                          openTarget?.path === p.path ? "bg-card2/60 shadow-[inset_2px_0_0_0_#8b5cf6]" : ""
                        }`}
                      >
                        <FileText className="h-4 w-4 shrink-0 text-muted" />
                        <span className="truncate text-sm text-txt">{p.title}</span>
                        {p.type === "memory" && (
                          <span
                            className="pill border border-violet/40 bg-violet/10 text-violet"
                            title={tKb("all.memoryPillTitle")}
                          >
                            {tKb("all.memoryPill")}
                          </span>
                        )}
                        <TagPills tags={p.tags} />
                        <span className="ml-auto truncate text-[11px] text-faint">{p.path}</span>
                      </button>
                    ))}
                  </div>
                  </>
                ))}

              {view === "discover" &&
                (discover ? (
                  <>
                    <div className="flex items-center gap-2 text-xs text-faint">
                      <Sparkles className="h-3.5 w-3.5 text-violet" />
                      {tKb("discover.summary", {
                        pages: discover.stats?.pages ?? 0,
                        edges: discover.stats?.edges ?? 0,
                      })}
                    </div>
                    <Section title={tKb("discover.relatedTitle")}>
                      <div className="flex gap-2">
                        <input
                          className="field flex-1"
                          placeholder={tKb("discover.relatedPlaceholder")}
                          value={relatedQuery}
                          onChange={(e) => setRelatedQuery(e.target.value)}
                          onKeyDown={(e) => e.key === "Enter" && onRelated()}
                        />
                        <button
                          onClick={onRelated}
                          className="rounded-lg border border-line2 px-3 text-xs text-muted hover:text-txt"
                        >
                          {tKb("discover.relatedButton")}
                        </button>
                      </div>
                      {related &&
                        (related.length === 0 ? (
                          <div className="mt-2 text-xs text-faint">{tKb("discover.noRelated")}</div>
                        ) : (
                          <div className="mt-2">
                            {related.map((r, i) => (
                              <PairRow key={i} a={relatedQuery} b={r.title} score={r.score} type={r.type} />
                            ))}
                          </div>
                        ))}
                    </Section>
                    <Section title={tKb("discover.strongTitle", { count: discover.strong.length })}>
                      {discover.strong.length === 0 ? (
                        <div className="text-xs text-faint">{tKb("discover.none")}</div>
                      ) : (
                        discover.strong.map((p, i) => <PairRow key={i} a={p.a} b={p.b} score={p.score} type={p.type} />)
                      )}
                    </Section>
                    <Section title={tKb("discover.clustersTitle", { count: discover.clusters.length })}>
                      {discover.clusters.length === 0 ? (
                        <div className="text-xs text-faint">{tKb("discover.noClusters")}</div>
                      ) : (
                        discover.clusters.map((c, i) => (
                          <div key={i} className="py-1">
                            <div className="text-sm text-txt">
                              {c.label} <span className="text-faint">{tKb("discover.density", { density: c.density })}</span>
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
                    <Section title={tKb("discover.mergeTitle", { count: discover.merge_candidates.length })}>
                      {discover.merge_candidates.length === 0 ? (
                        <div className="text-xs text-faint">{tKb("discover.none")}</div>
                      ) : (
                        discover.merge_candidates.map((p, i) => <PairRow key={i} a={p.a} b={p.b} score={p.score} />)
                      )}
                    </Section>
                    <Section title={tKb("discover.orphanTitle", { count: discover.isolated.length })}>
                      {discover.isolated.length === 0 ? (
                        <div className="text-xs text-faint">{tKb("discover.none")}</div>
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
                  <div className="text-sm text-faint">{tKb("discover.loading")}</div>
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
