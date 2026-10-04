"use client";

/**
 * In-project content search results (⇧⌘F) — docs/code-panel-design.md §3.4.
 *
 * The runtime call is INJECTED (`search`): this component never fetches, so the
 * panel stays the single owner of project/session/root resolution and of the
 * wire shape (brief §3.4 / file ownership). It only does UI + interaction.
 *
 * Truncation is ALWAYS surfaced (brief §1-2 / design §4.6): a silent cut reads
 * as "that is everything", which for a search is the worst possible lie.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { ChevronDown, Search, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { codeErrorMessage } from "./treeUtils";

// ---- shared search contract -------------------------------------------------
//
// Defined here (not in `codeTypes.ts`, which the wiring step owns) so both
// QuickOpen and SearchView speak one shape. The panel implements the injected
// `CodeSearchFn` against `GET /api/code/search`.

export type CodeSearchMode = "name" | "content";

export interface CodeSearchHit {
  /** Root-relative path (the wire's `root`-relative address space). */
  path: string;
  /** 1-based line number; only present for `mode=content`. */
  line?: number;
  /** The matching line's text; only present for `mode=content`. */
  text?: string;
}

/** One search response. Mirrors `{ ok, hits, scanned, truncated, elapsed_ms }`
 *  on success; a refusal (`ok:false`) is folded into `error` instead of
 *  throwing so the view can render it without a try/catch at every call site. */
export interface CodeSearchResult {
  hits: CodeSearchHit[];
  /** How many files the runtime walked before it returned. */
  scanned: number;
  /** The budget (time or hits) cut the run — the result may be incomplete. */
  truncated: boolean;
  elapsedMs: number;
  error?: { code: string; message?: string };
}

/** Injected by the panel. `limit` follows the wire's default 50 / cap 200. */
export type CodeSearchFn = (
  mode: CodeSearchMode,
  q: string,
  limit?: number,
) => Promise<CodeSearchResult>;

/** Debounce before a content search fires (content grep is expensive; typing
 *  "hello" must not launch five walks). Enter bypasses it. */
const DEBOUNCE_MS = 500;

/** Highlight every case-insensitive occurrence of the query inside a hit line.
 *  Index-based rather than regex — the query is user text, not a pattern. */
function Highlighted({ text, q }: { text: string; q: string }): ReactNode {
  const needle = q.trim().toLowerCase();
  if (!needle) return <>{text}</>;
  const hay = text.toLowerCase();
  const out: ReactNode[] = [];
  let i = 0;
  let key = 0;
  for (;;) {
    const j = hay.indexOf(needle, i);
    if (j < 0) {
      out.push(text.slice(i));
      break;
    }
    if (j > i) out.push(text.slice(i, j));
    out.push(
      <mark key={key++} className="rounded-sm bg-yellow/30 text-txt">
        {text.slice(j, j + needle.length)}
      </mark>,
    );
    i = j + needle.length;
  }
  return <>{out}</>;
}

export interface SearchViewProps {
  /** Data source, injected by the panel (never fetched here). */
  search: CodeSearchFn;
  /** Open a hit, positioned at `line` when present (the panel's jump path). */
  onOpen: (path: string, line?: number) => void;
  onClose: () => void;
  /** Root label shown in the header (e.g. the active root's name). */
  rootName?: string;
  initialQuery?: string;
  className?: string;
}

export function SearchView({
  search,
  onOpen,
  onClose,
  rootName,
  initialQuery = "",
  className,
}: SearchViewProps) {
  // code 域 catalog（本组件文案收在 code.search；错误码经 treeUtils 的
  // codeErrorMessage 走 code.errors）
  const t = useTranslations("code");
  const [q, setQ] = useState(initialQuery);
  const [result, setResult] = useState<CodeSearchResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  // Latest `search` without re-arming the debounce effect on every parent
  // render (an inline prop would otherwise change identity each time).
  const searchRef = useRef(search);
  searchRef.current = search;
  // Monotonic request id: a slow earlier request must never overwrite a newer
  // response (out-of-order results would show the wrong query's hits).
  const seqRef = useRef(0);

  const run = useCallback(async (raw: string) => {
    const query = raw.trim();
    const seq = ++seqRef.current;
    if (!query) {
      setResult(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const r = await searchRef.current("content", query);
      if (seq === seqRef.current) setResult(r);
    } catch (err) {
      if (seq !== seqRef.current) return;
      setResult({
        hits: [],
        scanned: 0,
        truncated: false,
        elapsedMs: 0,
        error: { code: "unknown", message: err instanceof Error ? err.message : t("search.searchFailed") },
      });
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Debounced auto-run; Enter runs immediately (below).
  useEffect(() => {
    if (!q.trim()) {
      seqRef.current += 1; // invalidate any in-flight run
      setResult(null);
      setLoading(false);
      return;
    }
    // 局部变量不可命名 t——会遮蔽 useTranslations 的 t（已知坑）
    const timer = window.setTimeout(() => void run(q), DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [q, run, t]);

  const groups = useMemo(() => {
    const out: { path: string; rows: { hit: CodeSearchHit; index: number }[] }[] = [];
    let index = 0;
    for (const hit of result?.hits ?? []) {
      let g = out[out.length - 1];
      if (!g || g.path !== hit.path) {
        g = { path: hit.path, rows: [] };
        out.push(g);
      }
      g.rows.push({ hit, index: index++ });
    }
    return out;
  }, [result]);

  const total = result?.hits.length ?? 0;

  useEffect(() => {
    if (!total) return;
    document.getElementById(`code-search-hit-${active}`)?.scrollIntoView({ block: "nearest" });
  }, [active, total]);

  const openHit = useCallback(
    (hit: CodeSearchHit | undefined) => {
      if (!hit) return;
      onOpen(hit.path, hit.line);
    },
    [onOpen],
  );

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(a + 1, Math.max(0, total - 1)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (result && total) openHit(result.hits[active]);
      else void run(q);
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  };

  const banner = useMemo(() => {
    if (!result) return null;
    if (result.error) {
      return (
        <div className="shrink-0 border-b border-line bg-card px-3 py-1 text-[11px] text-red">
          {codeErrorMessage(result.error.code)}
          {result.error.message ? `: ${result.error.message}` : ""}
        </div>
      );
    }
    if (result.truncated) {
      // Explicit by design: never let a budget cut masquerade as "no more hits".
      return (
        <div className="shrink-0 border-b border-line bg-card px-3 py-1 text-[11px] text-yellow">
          {t("search.stopped", { count: result.scanned.toLocaleString() })}
        </div>
      );
    }
    return null;
  }, [result, t]);

  return (
    <div className={cn("flex h-full min-h-0 flex-col", className)}>
      <div className="flex shrink-0 items-center gap-2 border-b border-line px-2 py-1.5">
        <Search className="h-3.5 w-3.5 shrink-0 text-faint" />
        <input
          ref={inputRef}
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setActive(0);
          }}
          onKeyDown={onKeyDown}
          placeholder={rootName ? t("search.placeholderNamed", { name: rootName }) : t("search.placeholder")}
          aria-label={t("search.inputAria")}
          className="min-w-0 flex-1 bg-transparent text-xs text-txt outline-none placeholder:text-faint"
        />
        <span className="shrink-0 text-[10px] text-faint">
          {loading
            ? t("search.searching")
            : result && !result.error
              ? t("search.stat", { hits: total, scanned: result.scanned.toLocaleString(), ms: result.elapsedMs })
              : ""}
        </span>
        <button
          type="button"
          onClick={onClose}
          title={t("search.close")}
          aria-label={t("search.close")}
          className="shrink-0 rounded p-0.5 text-faint transition-colors hover:text-txt"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      {banner}

      <div className="min-h-0 flex-1 overflow-auto py-1">
        {!q.trim() ? (
          <div className="px-3 py-6 text-center text-[11px] text-faint">
            {t("search.emptyHint")}
          </div>
        ) : loading && !result ? (
          <div className="px-3 py-6 text-center text-[11px] text-faint">{t("search.searching")}</div>
        ) : result && !result.error && !total ? (
          <div className="px-3 py-6 text-center text-[11px] text-faint">{t("search.noMatches")}</div>
        ) : (
          groups.map((g) => (
            <div key={g.path} className="mb-1">
              <div className="flex items-center gap-1 px-2 py-0.5 text-[11px] text-muted">
                <ChevronDown className="h-3 w-3 shrink-0 text-faint" />
                <span className="min-w-0 flex-1 truncate" title={g.path}>
                  {g.path}
                </span>
                <span className="shrink-0 text-[10px] text-faint">{t("search.groupHits", { count: g.rows.length })}</span>
              </div>
              {g.rows.map(({ hit, index }) => (
                <button
                  key={`${hit.path}:${hit.line ?? 0}:${index}`}
                  id={`code-search-hit-${index}`}
                  type="button"
                  onMouseEnter={() => setActive(index)}
                  onClick={() => openHit(hit)}
                  className={cn(
                    "flex w-full items-start gap-2 py-0.5 pl-6 pr-2 text-left text-[11px]",
                    index === active ? "bg-line2/60 text-txt" : "text-muted hover:bg-line2/40",
                  )}
                >
                  <span className="w-9 shrink-0 text-right font-mono text-[10px] text-faint">
                    {hit.line ?? ""}
                  </span>
                  <span className="min-w-0 flex-1 truncate font-mono" title={hit.text ?? ""}>
                    <Highlighted text={hit.text ?? ""} q={q} />
                  </span>
                </button>
              ))}
            </div>
          ))
        )}
      </div>

      <div className="shrink-0 border-t border-line px-3 py-1 text-[10px] text-faint">
        {t("search.keysHint")}
      </div>
    </div>
  );
}