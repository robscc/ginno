/**
 * Client for the Python sidecar (FastAPI on 127.0.0.1:8787).
 * In dev, the port is fixed. In release, Tauri spawns the sidecar
 * and exposes the port via the `sidecar_port` command.
 */

const PORT =
  typeof window !== "undefined" &&
  // @ts-ignore — Tauri injects `__TAURI__`
  window.__TAURI__?.core?.invoke
    ? 8787 // release: Tauri-managed; we'd call invoke("sidecar_port") here
    : Number(process.env.NEXT_PUBLIC_RUNTIME_PORT ?? 8787);

const BASE = `http://127.0.0.1:${PORT}`;

export interface Session {
  session_id: string;
  project_slug: string;
  workspace: string;
  model_provider: string;
  model_name: string;
}

export async function health(): Promise<{ ok: boolean; version: string }> {
  const r = await fetch(`${BASE}/health`);
  return r.json();
}

export async function createSession(req: {
  project_slug: string;
  workspace: string;
  model_provider?: string;
  model_name?: string;
}): Promise<Session> {
  const r = await fetch(`${BASE}/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  return r.json();
}

export async function listSessions(): Promise<Session[]> {
  const r = await fetch(`${BASE}/sessions`);
  return r.json();
}

export async function listSkills(project_slug?: string) {
  const url = new URL(`${BASE}/skills`);
  if (project_slug) url.searchParams.set("project_slug", project_slug);
  const r = await fetch(url);
  return r.json();
}

export function openSessionSocket(session_id: string): WebSocket {
  return new WebSocket(`ws://127.0.0.1:${PORT}/ws/sessions/${session_id}`);
}
