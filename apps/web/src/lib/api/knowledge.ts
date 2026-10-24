/**
 * Knowledge base / LLMWiki, memory refinery and citations / web sources.
 *
 * Split out of lib/runtime.ts, which re-exports every name unchanged.
 */
import { BASE, H, json } from "./core";

export async function kbServers() {
  return json<Array<{ name: string; tools: string[] }>>(`${BASE}/kb/servers`);
}
export async function kbSearch(q: string) {
  return json<{ q: string; results: string[] }>(`${BASE}/kb/search?q=${encodeURIComponent(q)}`);
}
export async function kbList(path = "") {
  return json<{ path: string; results: string[] }>(`${BASE}/kb/list?path=${encodeURIComponent(path)}`);
}
// ---- knowledge base / LLMWiki (in-memory vault index) ----
export async function kbWikiSearch(q: string) {
  return json<{ ok: boolean; error?: string; results: import("../types").WikiSearchResult[] }>(
    `${BASE}/kb/wiki/search?q=${encodeURIComponent(q)}`,
  );
}
export async function kbWikiSearchByTag(tag: string) {
  return json<{ ok: boolean; error?: string; results: import("../types").WikiSearchResult[] }>(
    `${BASE}/kb/wiki/search?tag=${encodeURIComponent(tag)}`,
  );
}
export async function kbWikiList() {
  return json<{ ok: boolean; error?: string; pages: import("../types").WikiPage[] }>(`${BASE}/kb/wiki/list`);
}
export async function kbWikiStats() {
  return json<import("../types").WikiStats>(`${BASE}/kb/wiki/stats`);
}
export async function kbWikiReindex() {
  return json<{ ok: boolean; indexed: number; tags: string[] }>(`${BASE}/kb/wiki/index`, {
    method: "POST",
  });
}
export async function kbWikiDiscover() {
  return json<import("../types").WikiDiscover>(`${BASE}/kb/wiki/discover`);
}
export async function kbWikiRelated(title: string, top_k = 10) {
  return json<{ ok: boolean; related: import("../types").WikiRelatedItem[]; clusters: unknown[] }>(
    `${BASE}/kb/wiki/related?title=${encodeURIComponent(title)}&top_k=${top_k}`,
  );
}
export async function kbWikiOrphans() {
  return json<{ ok: boolean; pages: import("../types").WikiPage[] }>(`${BASE}/kb/wiki/orphans`);
}
export async function kbWikiPage(path = "", title = "") {
  const q = new URLSearchParams();
  if (path) q.set("path", path);
  if (title) q.set("title", title);
  return json<import("../types").WikiPageDoc>(`${BASE}/kb/wiki/page?${q.toString()}`);
}
export async function kbWikiPutPage(path: string, raw: string) {
  return json<{ ok: boolean; error?: string; path?: string }>(`${BASE}/kb/wiki/page`, {
    method: "PUT",
    headers: H,
    body: JSON.stringify({ path, raw }),
  });
}
export async function kbWikiCreatePage(path: string, raw: string) {
  return json<{ ok: boolean; error?: string; path?: string }>(`${BASE}/kb/wiki/page`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ path, raw }),
  });
}
export async function kbWikiProbe(path: string) {
  return json<{
    ok: boolean;
    error?: string;
    vault_path?: string;
    detected?: {
      namespace: string;
      wiki_dir: string;
      raw_dir: string;
      research_dir: string;
      memory_dir: string;
      todo_dir: string;
    };
    wiki_pages?: number;
    raw_pages?: number;
    has_index?: boolean;
    total_md?: number;
  }>(`${BASE}/kb/wiki/probe?path=${encodeURIComponent(path)}`);
}
export async function kbWikiPutConfig(data: object) {
  return json<{ ok: boolean }>(`${BASE}/kb/wiki/config`, {
    method: "PUT",
    headers: H,
    body: JSON.stringify(data),
  });
}

// ---- memory refinery (draft → review → apply; KB-aware promotion) ----
export async function getMemory() {
  return json<{ ok: boolean; content: string; pool_count: number; draft_pending: boolean; kb_usable: boolean }>(
    `${BASE}/memory`,
  );
}
export async function summarizeMemory(provider?: string) {
  return json<
    {
      ok: boolean;
      draft_exists?: boolean;
      error?: string;
      message?: string;
      skipped?: string;
    } & import("../types").MemoryDraft
  >(`${BASE}/memory/summarize`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(provider ? { provider } : {}),
  });
}
export async function getMemoryDraft() {
  return json<{ ok: boolean } & import("../types").MemoryDraft>(`${BASE}/memory/draft`);
}
export async function applyMemoryDraft(content?: string, force?: boolean) {
  return json<{ ok: boolean; error?: string; summarized_chars?: number; pool_remaining?: number }>(
    `${BASE}/memory/draft/apply`,
    {
      method: "POST",
      headers: H,
      body: JSON.stringify({ ...(content !== undefined ? { content } : {}), ...(force ? { force: true } : {}) }),
    },
  );
}
export async function discardMemoryDraft() {
  return json<{ ok: boolean }>(`${BASE}/memory/draft/discard`, { method: "POST" });
}
export async function kbPromotePreview(text: string, title?: string) {
  return json<import("../types").PromotePreview>(`${BASE}/kb/wiki/promote/preview`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ text, ...(title ? { title } : {}) }),
  });
}
export async function kbPromoteApply(path: string, raw: string, removeFromMemory?: string[]) {
  return json<{ ok: boolean; error?: string; path?: string; removed_from_memory?: number }>(
    `${BASE}/kb/wiki/promote/apply`,
    {
      method: "POST",
      headers: H,
      body: JSON.stringify({
        path,
        raw,
        ...(removeFromMemory?.length ? { remove_from_memory: removeFromMemory } : {}),
      }),
    },
  );
}

// ---- citations & web sources (docs/citations-design.md) ----
export type SourceItem = { kind: "wiki" | "web"; ref: string; note?: string };

export async function openExternal(url: string) {
  return json<{ ok: boolean; error?: string }>(`${BASE}/open-external`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ url }),
  });
}

/** Click handler for rendered external links: WKWebView silently ignores
 *  target=_blank / window.open, so clicks route through the sidecar's
 *  /api/open-external (which launches the OS default browser, with the
 *  public-host guard). window.open stays as the fallback for a down
 *  sidecar (covers plain-browser dev, where it actually works). */
export function openLinkExternal(url: string) {
  openExternal(url)
    .then((r) => {
      if (!r.ok) window.open(url, "_blank", "noopener");
    })
    .catch(() => {
      window.open(url, "_blank", "noopener");
    });
}
export async function getCitationUsage(sort = "cited", limit = 20) {
  return json<{ ok: boolean; rows: Array<Record<string, unknown>> }>(
    `${BASE}/kb/wiki/usage?sort=${encodeURIComponent(sort)}&limit=${limit}`,
  );
}
export async function getWebUsage() {
  return json<{
    ok: boolean;
    engines: Array<{ engine: string; searches: number; hits_cited: number; cite_rate: number }>;
    top_domains: Array<{ domain: string; cited: number; fetched: number }>;
    total_searches: number;
    total_cited: number;
  }>(`${BASE}/kb/wiki/web-usage`);
}
export async function testWebSearch(engine: string) {
  return json<{ ok: boolean; results?: number; error?: string }>(`${BASE}/web/test-search`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ engine }),
  });
}
