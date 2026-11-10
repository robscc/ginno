/**
 * Instincts（本能）API client —— docs/instincts-design.md §10.2（P0 子集）。
 *
 * Split out of lib/runtime.ts, which re-exports every name unchanged.
 */
import { BASE, H, json } from "./core";

export interface InstinctEvidence {
  observed: number;
  applied: number;
  uncorrected: number;
  corrected: number;
  last_seen: string;
}

export interface Instinct {
  id: string;
  trigger: string;
  behavior: string;
  confidence: number;
  domain: string;
  scope: "project" | "global";
  project_slug: string;
  source: string;
  status: "pending" | "active" | "evolved" | "pruned";
  evidence: InstinctEvidence;
  created: string;
  updated: string;
  evolved_to?: string | null;
}

export interface InstinctBody {
  id: string;
  trigger: string;
  behavior: string;
  confidence: number;
  domain: string;
  scope: "project" | "global";
  project_slug: string;
  source: string;
  status: string;
  evidence?: Partial<InstinctEvidence> | null;
}

export async function listInstincts(params?: {
  scope?: string;
  project_slug?: string;
  status?: string;
  domain?: string;
  min_confidence?: number;
}): Promise<Instinct[]> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v !== undefined && v !== "") q.set(k, String(v));
  }
  const qs = q.toString();
  return json(`${BASE}/instincts${qs ? `?${qs}` : ""}`);
}

export async function listObservations(limit = 50): Promise<Record<string, unknown>[]> {
  return json(`${BASE}/instincts/observations?limit=${limit}`);
}

export async function createInstinct(body: InstinctBody): Promise<Instinct> {
  return json(`${BASE}/instincts`, { method: "POST", headers: H, body: JSON.stringify(body) });
}

export async function updateInstinct(id: string, body: InstinctBody): Promise<Instinct> {
  return json(`${BASE}/instincts/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: H,
    body: JSON.stringify(body),
  });
}

export async function deleteInstinct(
  id: string,
  scope: string,
  project_slug: string,
  hard = false,
): Promise<{ ok: boolean }> {
  return json(
    `${BASE}/instincts/${encodeURIComponent(id)}?scope=${scope}&project_slug=${encodeURIComponent(project_slug)}&hard=${hard}`,
    { method: "DELETE", headers: H },
  );
}
