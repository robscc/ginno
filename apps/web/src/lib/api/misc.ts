/**
 * Endpoints that do not belong to a single product domain: usage
 * telemetry, debug log and the mod marketplace.
 *
 * Split out of lib/runtime.ts, which re-exports every name unchanged.
 */
import { BASE, H, json } from "./core";
import type { UsageGrid, UsageOverview, UsageRequests, UsageSessions } from "../types";

// ---- usage telemetry (usage-stats-design.md §5) ----
export async function getUsageOverview(days = 30) {
  return json<UsageOverview>(`${BASE}/usage/overview?days=${days}`);
}

export async function getUsageGrid(opts?: { from?: string; to?: string }) {
  const q = new URLSearchParams();
  if (opts?.from) q.set("from", opts.from);
  if (opts?.to) q.set("to", opts.to);
  const s = q.toString();
  return json<UsageGrid>(`${BASE}/usage/grid${s ? `?${s}` : ""}`);
}

export async function getUsageSessions(opts?: { from?: string; to?: string; sort?: string; limit?: number }) {
  const q = new URLSearchParams();
  if (opts?.from) q.set("from", opts.from);
  if (opts?.to) q.set("to", opts.to);
  if (opts?.sort) q.set("sort", opts.sort);
  if (opts?.limit) q.set("limit", String(opts.limit));
  const s = q.toString();
  return json<UsageSessions>(`${BASE}/usage/sessions${s ? `?${s}` : ""}`);
}

export async function getUsageRequests(opts?: {
  date?: string;
  provider?: string;
  model?: string;
  source?: string;
  session_id?: string;
  page?: number;
  page_size?: number;
}) {
  const q = new URLSearchParams();
  if (opts?.date) q.set("date", opts.date);
  if (opts?.provider) q.set("provider", opts.provider);
  if (opts?.model) q.set("model", opts.model);
  if (opts?.source) q.set("source", opts.source);
  if (opts?.session_id) q.set("session_id", opts.session_id);
  if (opts?.page) q.set("page", String(opts.page));
  if (opts?.page_size) q.set("page_size", String(opts.page_size));
  const s = q.toString();
  return json<UsageRequests>(`${BASE}/usage/requests${s ? `?${s}` : ""}`);
}

// Temporary telemetry for diagnosing WKWebView drag & drop (see ChatStream.addFiles).
export async function debugLog(payload: unknown) {
  try {
    await fetch(`${BASE}/debug-log`, {
      method: "POST",
      headers: H,
      body: JSON.stringify(payload),
    });
  } catch {
    /* best-effort */
  }
}
// ---- marketplace（mod-marketplace-design.md §6.2）---------------------------

export interface MarketPlugin {
  name: string;
  source: string;
  description: string;
  version: string;
  tags: string[];
}

export interface MarketSource {
  name: string;
  repo: string;
  enabled: boolean;
  skipped?: boolean;
  commit?: string;
  fetched_at?: number;
  error?: string;
  plugins: MarketPlugin[];
}

export interface MarketOrigin {
  marketplace: string;
  repo: string;
  path: string;
  commit: string;
  version: string;
  installed_at: number;
}

export interface MarketplaceStatus {
  ok?: boolean;
  error?: string;
  sources: MarketSource[];
  /** 已装 mod name → 市场来源(origin);本地安装的 mod 无键 */
  installedOrigins: Record<string, MarketOrigin | null>;
}

export interface MarketItemDetail {
  ok: boolean;
  error?: string;
  path?: string;
  manifest?: { name?: string; version?: string; description?: string };
  readme?: string;
  hookModules?: string[];
  entry?: MarketPlugin;
  source?: { name?: string; repo?: string };
  commit?: string;
}

export async function listMarketplace(refresh = false) {
  return json<MarketplaceStatus>(
    `${BASE}/mods/marketplace${refresh ? "?refresh=1" : ""}`,
  );
}

export async function getMarketplaceItem(src: string, plugin: string) {
  return json<MarketItemDetail>(
    `${BASE}/mods/marketplace/item?src=${encodeURIComponent(src)}&path=${encodeURIComponent(plugin)}`,
  );
}

export async function installFromMarket(src: string, plugin: string, overwrite = false) {
  return json<{ ok: boolean; error?: string; mod?: { name: string; origin?: MarketOrigin } }>(
    `${BASE}/mods/marketplace/install`,
    { method: "POST", headers: H, body: JSON.stringify({ src, path: plugin, overwrite }) },
  );
}

export async function updateFromMarket(name: string) {
  return json<{ ok: boolean; error?: string; mod?: { name: string; origin?: MarketOrigin } }>(
    `${BASE}/mods/marketplace/update`,
    { method: "POST", headers: H, body: JSON.stringify({ name }) },
  );
}

export async function putMarketplaceSources(
  sources: Array<Pick<MarketSource, "name" | "repo" | "enabled">>,
) {
  return json<{ ok: boolean; sources: MarketSource[] }>(
    `${BASE}/mods/marketplace/sources`,
    { method: "PUT", headers: H, body: JSON.stringify({ sources }) },
  );
}
