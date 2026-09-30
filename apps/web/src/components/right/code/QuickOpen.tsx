"use client";

/**
 * ⌘P quick-open: search files by NAME and jump to one — docs/code-panel-design.md
 * §3.4.
 *
 * Like SearchView, the runtime call is INJECTED (`search`); this component never
 * fetches. The server does the matching (fuzzy/substring), so the list here is
 * exactly what the runtime returned — no client-side re-filtering that could
 * disagree with it.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { FileText, Search } from "lucide-react";
import { cn } from "@/lib/utils";
import { codeErrorMessage } from "./treeUtils";
import type { CodeSearchFn } from "./SearchView";

/** Debounce for the name search — cheap enough to run while typing, but not
 *  once per keystroke. */
const DEBOUNCE_MS = 120;

/** Split a root-relative path into its directory prefix and file name. */
function splitPath(path: string): { dir: string; name: string } {
  const i = path.lastIndexOf("/");
  return i < 0 ? { dir: "", name: path } : { dir: path.slice(0, i), name: path.slice(i + 1) };
}

export interface QuickOpenProps {
  /** Data source, injected by the panel (never fetched here). */
  search: CodeSearchFn;
  /** Open the picked file (root-relative path). */
  onOpen: (path: string) => void;
  onClose: () => void;
  className?: string;
}

export function QuickOpen({ search, onOpen, onClose, className }: QuickOpenProps) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<string[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState<{ code: string; message?: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const searchRef = useRef(search);
  searchRef.current = search;
  const seqRef = useRef(0);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Debounced name search. An empty query clears everything (no "recent files"
  // list exists yet — inventing one here would be a silent scope creep).
  useEffect(() => {
    const query = q.trim();
    const seq = ++seqRef.current;
    if (!query) {
      setHits([]);
      setTruncated(false);
      setError(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    const t = window.setTimeout(() => {
      void (async () => {
        try {
          const r = await searchRef.current("name", query);
          if (seq !== seqRef.current) return;
          setHits(r.hits.map((h) => h.path));
          setTruncated(r.truncated);
          setError(r.error ?? null);
        } catch (err) {
          if (seq !== seqRef.current) return;
          setHits([]);
          setError({ code: "unknown", message: err instanceof Error ? err.message : "搜索失败" });
        } finally {
          if (seq === seqRef.current) setLoading(false);
        }
      })();
    }, DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [q]);

  useEffect(() => {
    setActive(0);
  }, [hits]);

  const pick = useMemo(
    () => (path: string) => {
      onOpen(path);
      onClose();
    },
    [onOpen, onClose],
  );

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(a + 1, Math.max(0, hits.length - 1)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (hits[active]) pick(hits[active]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  };

  return (
    <div
      className={cn("fixed inset-0 z-50 flex items-start justify-center bg-black/50 pt-[12vh]", className)}
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-label="快速打开文件"
        className="w-[560px] max-w-[90vw] overflow-hidden rounded-xl border border-line bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-line px-3">
          <Search className="h-4 w-4 shrink-0 text-faint" />
          <input
            ref={inputRef}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="按文件名搜索…"
            className="w-full bg-transparent py-3 text-sm text-txt outline-none placeholder:text-faint"
          />
          {loading ? <span className="shrink-0 text-[10px] text-faint">搜索中…</span> : null}
        </div>

        <div className="max-h-[46vh] overflow-y-auto py-1">
          {error ? (
            <div className="px-3 py-6 text-center text-xs text-red">
              {codeErrorMessage(error.code)}
              {error.message ? `：${error.message}` : ""}
            </div>
          ) : q.trim() && !hits.length && !loading ? (
            <div className="px-3 py-6 text-center text-xs text-faint">没有匹配的文件</div>
          ) : !q.trim() ? (
            <div className="px-3 py-6 text-center text-xs text-faint">输入文件名开始搜索</div>
          ) : (
            hits.map((path, i) => {
              const { dir, name } = splitPath(path);
              return (
                <button
                  key={path}
                  type="button"
                  onMouseEnter={() => setActive(i)}
                  onClick={() => pick(path)}
                  className={cn(
                    "flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm transition-colors",
                    i === active ? "bg-card2 text-txt" : "text-muted",
                  )}
                >
                  <FileText className="h-3.5 w-3.5 shrink-0 text-faint" />
                  <span className="shrink-0 font-medium">{name}</span>
                  {dir ? (
                    <span className="min-w-0 flex-1 truncate text-[11px] text-faint" title={path}>
                      {dir}
                    </span>
                  ) : (
                    <span className="min-w-0 flex-1" />
                  )}
                </button>
              );
            })
          )}
        </div>

        <div className="border-t border-line px-3 py-1.5 text-[10px] text-faint">
          {truncated ? "结果可能不完整（搜索已截断） · " : ""}
          ↑↓ 选择 · Enter 打开 · Esc 关闭
        </div>
      </div>
    </div>
  );
}