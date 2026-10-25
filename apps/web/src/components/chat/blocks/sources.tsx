"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { BookMarked, ChevronDown, Globe, Link2 } from "lucide-react";
import type { WikiPage } from "@/lib/types";
import { useChatT, useEventI18nText } from "./i18n";
import type { Block, SourceItem } from "./types";

// Non-global: used by .test()/.match() (a /g regex there would be stateful).
const CITATION_BLOCK_RE =
  /<\s*ginno_(?:wiki_)?citations\s*>([\s\S]*?)<\s*\/\s*ginno_(?:wiki_)?citations\s*>/i;
// Global variants for replace(): strip ALL blocks, including a truncated one
// whose closing tag never arrived (model output cut off inside the block).
const CITATION_BLOCK_RE_G =
  /<\s*ginno_(?:wiki_)?citations\s*>[\s\S]*?<\s*\/\s*ginno_(?:wiki_)?citations\s*>/gi;
const CITATION_UNCLOSED_RE = /<\s*ginno_(?:wiki_)?citations\b[^>]*>[\s\S]*$/i;
const CITATION_OPEN_RE = /<\s*ginno_(?:wiki_)?citations/i;
// web_search tool output lines: "[s1] Title — host\n    https://url"
const WEB_RESULT_RE = /\[(s\d+)\][^\n]*\n\s+(https?:\/\/\S+)/g;

/** Tolerantly parse a trailing ``<ginno_citations>`` block (mirror of the
 * runtime parser — history text is already stripped, this covers live text). */
export function parseSources(text: string): SourceItem[] {
  const m = text.match(CITATION_BLOCK_RE);
  if (!m) return [];
  const items: SourceItem[] = [];
  const seen = new Set<string>();
  for (const raw of m[1].split("\n")) {
    let line = raw.trim();
    if (!line) continue;
    let note = "";
    // Brackets optional: contract is note=[…] but models often emit note=… —
    // accept both so the note splits off and web refs stay clean, openable URLs.
    const nm = line.match(/\|\s*note\s*=\s*\[?(.*?)\]?\s*$/i);
    if (nm) {
      note = nm[1].trim();
      line = line.slice(0, nm.index).trimEnd();
    }
    let kind: string, ref: string;
    const bar = line.indexOf("|");
    if (bar < 0) {
      kind = "wiki";
      ref = line;
    } else {
      kind = line.slice(0, bar).trim().toLowerCase();
      ref = line.slice(bar + 1).trim();
      // Contract is note=[…], but models also emit a bare description segment
      // (`web|https://…|why cited` — GLM does this on every turn). Without
      // peeling it off the ref stops being a URL and the row never opens.
      if (!note && ref.includes("|")) {
        const rbar = ref.indexOf("|");
        note = ref
          .slice(rbar + 1)
          .trim()
          .replace(/^\[|\]$/g, "")
          .trim();
        ref = ref.slice(0, rbar).trim();
      }
    }
    if ((kind !== "wiki" && kind !== "web") || !ref) continue;
    const key = `${kind}:${ref.toLowerCase()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    items.push({ kind: kind as SourceItem["kind"], ref, note });
    if (items.length >= 20) break;
  }
  return items;
}

export function stripSources(text: string): string {
  return text
    .replace(CITATION_BLOCK_RE_G, "")
    .replace(CITATION_UNCLOSED_RE, "")
    .replace(/\s+$/, "");
}

/** Build an sN → URL map from the web_search tool outputs in a block list.
 * The citation contract lets the model cite by id (`web|s3`); the id only
 * means anything next to the tool result that minted it, so resolve it here
 * (both live transcripts and history carry the tool blocks). */
export function webRefMap(blocks: Block[]): Record<string, string> {
  const map: Record<string, string> = {};
  for (const b of blocks) {
    if (b.kind !== "tool" || b.name !== "web_search" || !b.content) continue;
    WEB_RESULT_RE.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = WEB_RESULT_RE.exec(b.content)) !== null) {
      map[m[1].toLowerCase()] = m[2];
    }
  }
  return map;
}

/** Replace `web|sN` refs with their resolved URL (leaves others untouched). */
export function resolveSourceRefs(items: SourceItem[], map: Record<string, string>): SourceItem[] {
  return items.map((s) => {
    if (s.kind === "web" && /^s\d+$/i.test(s.ref)) {
      const url = map[s.ref.toLowerCase()];
      if (url) return { ...s, ref: url };
    }
    return s;
  });
}

/** While streaming, hide an in-flight (not yet closed) citation block so the
 * raw machine text never flashes; the closed block folds into SourcesBlock. */
export function maskPartialSources(text: string): string {
  if (CITATION_BLOCK_RE.test(text)) return text;
  const m = text.match(CITATION_OPEN_RE);
  return m ? text.slice(0, m.index).replace(/\s+$/, "") : text;
}

/** Centered, de-emphasized system row for context chips. */
export function ContextBlocks({ blocks }: { blocks: Extract<Block, { kind: "context" }>[] }) {
  const evText = useEventI18nText();
  if (!blocks.length) return null;
  return (
    <div className="flex flex-col items-center gap-1">
      {blocks.map((b, i) => (
        <div
          key={i}
          className="selectable max-w-[85%] whitespace-pre-wrap rounded-lg border border-line/60 bg-card/40 px-3 py-1.5 text-center text-xs leading-relaxed text-muted"
        >
          {/* 事件契约：i18n_key 命中即翻译，否则原样直显 text（历史会话兼容） */}
          {evText(b, b.text)}
        </div>
      ))}
    </div>
  );
}

/** Web-hostname label for a citation ref (falls back to the ref itself). */
function hostOf(ref: string): string {
  try {
    return new URL(ref).hostname.replace(/^www\./, "");
  } catch {
    return ref;
  }
}

/** Normalize a wiki ref for matching — frontend mirror of the runtime's
 * `_norm_wiki_ref`: `./` as a SEGMENT prefix, leading slashes, optional `.md`,
 * case-insensitive (models cite paths or titles with drift). */
export function normWikiRef(ref: string): string {
  let r = ref.trim();
  while (r.startsWith("./")) r = r.slice(2);
  r = r.replace(/^\/+/, "");
  if (r.toLowerCase().endsWith(".md")) r = r.slice(0, -3);
  return r.toLowerCase();
}

/** Resolve a cited wiki ref against the KB page list (path first, then title —
 * same precedence the runtime's validate_citations uses). Null = not in KB. */
export function matchWikiPage(pages: WikiPage[], ref: string): WikiPage | null {
  const key = normWikiRef(ref);
  if (key) {
    const byPath = pages.find((p) => normWikiRef(p.path) === key);
    if (byPath) return byPath;
  }
  const titleKey = ref.trim().toLowerCase();
  return pages.find((p) => p.title.trim().toLowerCase() === titleKey) ?? null;
}

/** Answer provenance: cited wiki pages + web sources (citations-design.md §5.2).
 * Collapsed to one line; expands to a list. Web rows open in the system
 * browser (via sidecar — WKWebView won't hand off external links itself);
 * wiki rows deep-link to /kb?page=<path> (resolved against the KB list —
 * frontend-only, mirroring validate_citations' path-then-title matching).
 * Unresolvable refs (index_only citations, deleted pages, model drift)
 * degrade to the old plain row + 「未收录」 badge instead of navigating. */
export function SourcesBlock({ items }: { items: SourceItem[] }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [missed, setMissed] = useState<ReadonlySet<string>>(new Set());
  const router = useRouter();
  const { tc } = useChatT();
  if (!items.length) return null;

  const openWeb = async (url: string) => {
    setBusy(url);
    try {
      const { openExternal } = await import("@/lib/runtime");
      const r = await openExternal(url);
      if (!r.ok) window.open(url, "_blank", "noopener");
    } catch {
      window.open(url, "_blank", "noopener");
    } finally {
      setBusy(null);
    }
  };

  const openWiki = async (ref: string) => {
    setBusy(ref);
    try {
      const { kbWikiList } = await import("@/lib/runtime");
      const r = await kbWikiList();
      const target = r.ok ? matchWikiPage(r.pages ?? [], ref) : null;
      if (target) {
        router.push(`/kb?page=${encodeURIComponent(target.path)}`);
      } else {
        // Only "list loaded, no match" is a real miss — a fetch error keeps
        // the row clickable (the page may exist; retry later).
        setMissed((prev) => {
          const next = new Set(prev);
          next.add(ref);
          return next;
        });
      }
    } catch {
      /* sidecar unreachable — leave the row as-is */
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mt-2 rounded-lg border border-line/60 bg-card/40 text-xs">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-muted hover:text-txt"
      >
        <Link2 className="h-3.5 w-3.5 shrink-0" />
        <span>{tc("sources.title", { count: items.length })}</span>
        <ChevronDown className={`ml-auto h-3.5 w-3.5 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div className="flex flex-col gap-0.5 border-t border-line/50 px-2 py-1.5">
          {items.map((s, i) => {
            const isWeb = s.kind === "web" && /^https?:\/\//i.test(s.ref);
            const wikiMissed = s.kind === "wiki" && missed.has(s.ref);
            const label = s.kind === "web" ? hostOf(s.ref) : s.ref.split("/").pop() || s.ref;
            const Icon = s.kind === "web" ? Globe : BookMarked;
            return (
              <div
                key={i}
                className="flex items-start gap-2 rounded-md px-1.5 py-1 hover:bg-panel/60"
                title={s.note || s.ref}
              >
                <Icon className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${s.kind === "web" ? "text-blue" : "text-violet"}`} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    {isWeb ? (
                      <button
                        type="button"
                        disabled={busy === s.ref}
                        onClick={() => openWeb(s.ref)}
                        className="max-w-full truncate text-left text-txt underline decoration-line underline-offset-2 hover:text-blue disabled:opacity-50"
                      >
                        {label}
                      </button>
                    ) : s.kind === "wiki" && !wikiMissed ? (
                      <button
                        type="button"
                        disabled={busy === s.ref}
                        onClick={() => openWiki(s.ref)}
                        className="max-w-full truncate text-left text-txt underline decoration-line underline-offset-2 hover:text-violet disabled:opacity-50"
                      >
                        {label}
                      </button>
                    ) : (
                      <span className="min-w-0 max-w-full truncate text-txt">{label}</span>
                    )}
                    {wikiMissed && (
                      <span className="shrink-0 rounded border border-line px-1 text-[10px] leading-4 text-faint">
                        {tc("sources.notIndexed")}
                      </span>
                    )}
                  </div>
                  {s.note && <div className="truncate text-faint">{s.note}</div>}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
