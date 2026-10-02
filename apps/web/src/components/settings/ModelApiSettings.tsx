"use client";

import { useEffect, useRef, useState } from "react";
import { Plus } from "lucide-react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { ModelConfig, ModelConfigRefs } from "@/lib/types";
import { ModelConfigCard } from "./ModelConfigCard";
import { ModelConfigForm } from "./ModelConfigForm";
import { blankConfig, describeRefs, normalizeConfig } from "./modelConfigShared";

const COLLAPSE_AT = 6; // Q9: no hard cap; fold beyond six cards

// Settings → 模型 API: config list + add/edit (multi-provider-model-config.md
// §2). Talks only to /api/model_configs; deletion is a PUT with the entry
// removed, and verify both probes AND persists (Q4).
export function ModelApiSettings() {
  const g = useGinno();
  const [configs, setConfigs] = useState<ModelConfig[]>([]);
  const [defaultConfig, setDefaultConfig] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [editing, setEditing] = useState<"new" | string | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string; refs?: ModelConfigRefs } | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [busy, setBusy] = useState(false);

  // System-proxy switch (settings.json top-level `use_system_proxy`, default
  // on). Kept in this tab from the previous providers UI: the proxy is a
  // property of the network environment, applied globally by the runtime.
  const [useSysProxy, setUseSysProxy] = useState<boolean | null>(null);
  const [proxyMsg, setProxyMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    void load();
    void api
      .getSettings()
      .then((s) => {
        const v = (s as Record<string, unknown>).use_system_proxy;
        setUseSysProxy(typeof v === "boolean" ? v : true);
      })
      .catch(() => setUseSysProxy(true));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const load = async () => {
    try {
      const r = await api.listModelConfigs();
      setConfigs((r.configs ?? []).map(normalizeConfig));
      setDefaultConfig(r.default_config ?? "");
      setLoadError(null);
    } catch {
      setLoadError("Cannot connect to the runtime; the config list is unavailable");
    } finally {
      setLoading(false);
    }
  };

  // Green flashes self-dismiss; errors stay until the next action.
  useEffect(() => {
    if (notice?.kind !== "ok") return;
    const t = setTimeout(() => setNotice(null), 3000);
    return () => clearTimeout(t);
  }, [notice]);

  // PUT replaces the whole model_configs block, so always send every config.
  // Re-list after success: the server is the source of truth (it may have
  // re-assigned the default after a delete). Other pages (Agents dropdown,
  // session creation) still read the legacy providers key — refresh it too.
  const applyPut = async (next: ModelConfig[], dc: string, okText?: string): Promise<boolean> => {
    setBusy(true);
    try {
      const r = await api.putModelConfigs(next, dc);
      if (r?.ok !== false) {
        await load();
        g.reloadProviders();
        g.reloadSessions();
        if (okText) setNotice({ kind: "ok", text: okText });
        return true;
      }
      setNotice({ kind: "error", text: r.error || "Failed to save", refs: r.refs });
    } catch {
      setNotice({ kind: "error", text: "Failed to save: cannot connect to the runtime" });
    } finally {
      setBusy(false);
    }
    return false;
  };

  const onToggle = (cfg: ModelConfig) => {
    const next = configs.map((c) => (c.id === cfg.id ? { ...c, enabled: !c.enabled } : c));
    void applyPut(next, defaultConfig, `${cfg.enabled ? "Disabled" : "Enabled"} "${cfg.name || cfg.id}"`);
  };

  const onSetDefault = (id: string) => {
    void applyPut(configs, id, "Set as default");
  };

  const onDelete = (id: string) => {
    const cfg = configs.find((c) => c.id === id);
    let dc = defaultConfig;
    let okText = `Deleted "${cfg?.name || id}"`;
    // Removing the default: re-point the default at the first remaining
    // enabled config in the same PUT (mirrors the backend fallback chain);
    // if none remains, clear it and let the backend decide.
    if (cfg && dc === id) {
      dc = nextEnabledAfterRemoval(configs, id);
      if (dc) okText += `, default switched to "${configs.find((c) => c.id === dc)?.name ?? dc}"`;
    }
    void applyPut(
      configs.filter((c) => c.id !== id),
      dc,
      okText,
    );
    if (editing === id) setEditing(null);
  };

  const onSaved = (saved: ModelConfig, latencyMs: number) => {
    // Q4: the verify endpoint already persisted the record — merge the
    // returned config into the local list, no extra PUT.
    setConfigs((cs) => {
      const i = cs.findIndex((c) => c.id === saved.id);
      if (i === -1) return [...cs.map(normalizeConfig), normalizeConfig(saved)];
      const next = [...cs];
      next[i] = normalizeConfig(saved);
      return next;
    });
    setEditing(null);
    setNotice({ kind: "ok", text: `"${saved.name || saved.id}" verified and saved · ${latencyMs} ms` });
    g.reloadProviders();
    g.reloadSessions();
  };

  // Sort: default first, then by name (design §2.1).
  const sorted = [...configs].sort((a, b) => {
    if (a.id === defaultConfig) return -1;
    if (b.id === defaultConfig) return 1;
    return (a.name || a.id).localeCompare(b.name || b.id);
  });

  const visible = showAll || editing === "new" ? sorted : sorted.slice(0, COLLAPSE_AT);

  return (
    <div className="mx-auto max-w-3xl px-8 py-7">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-txt">Model API</h2>
          <p className="mt-1 text-sm text-muted">
            Connect any number of model endpoints: Anthropic protocol, OpenAI-compatible endpoints
            (DeepSeek / Qwen / Ollama etc.), and the OpenAI Responses API — powering Agent
            reasoning.
          </p>
        </div>
        <button
          onClick={() => setEditing(editing === "new" ? null : "new")}
          disabled={busy}
          className="flex shrink-0 items-center gap-1.5 rounded-lg bg-violet px-3 py-2 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          <Plus className="h-3.5 w-3.5" />
          Add config
        </button>
      </div>

      {notice && (
        // Single yellow bar for refusals (Q8): error text + reference summary.
        // Success notices use the same slot in green and self-dismiss.
        <div
          className={`mt-4 rounded-md border px-3 py-2 text-xs ${
            notice.kind === "ok" ? "border-green/40 bg-green/10 text-green" : "border-yellow/40 bg-yellow/10 text-yellow"
          }`}
        >
          {notice.text}
          {notice.kind === "error" && describeRefs(notice.refs) && (
            <div className="mt-1">Still referenced by: {describeRefs(notice.refs)} (rebind the agents / sessions first, then delete)</div>
          )}
        </div>
      )}

      {loading ? (
        <div className="mt-6 text-sm text-faint">Loading…</div>
      ) : loadError ? (
        <div className="mt-6 rounded-md border border-yellow/40 bg-yellow/10 px-3 py-2 text-xs text-yellow">
          {loadError}
        </div>
      ) : (
        <div className="mt-5 space-y-4">
          {editing === "new" && (
            <ModelConfigForm initial={blankConfig()} isNew onCancel={() => setEditing(null)} onSaved={onSaved} />
          )}

          {sorted.length === 0 && editing !== "new" && (
            <div className="rounded-2xl border border-dashed border-line2 p-8 text-center">
              <p className="text-sm text-muted">No model configs yet</p>
              <p className="mx-auto mt-2 max-w-md text-xs leading-5 text-faint">
                Click &quot;Add config&quot; in the top right, fill in the Base URL and API key,
                then verify. Anthropic official, DeepSeek, Qwen, Kimi, local Ollama and other
                OpenAI-compatible endpoints all work.
              </p>
            </div>
          )}

          {visible.map((cfg) =>
            editing === cfg.id ? (
              <ModelConfigForm
                key={cfg.id}
                initial={cfg}
                isNew={false}
                onCancel={() => setEditing(null)}
                onSaved={onSaved}
              />
            ) : (
              <ModelConfigCard
                key={cfg.id}
                cfg={cfg}
                isDefault={cfg.id === defaultConfig}
                busy={busy}
                onToggle={() => onToggle(cfg)}
                onSetDefault={() => onSetDefault(cfg.id)}
                onEdit={() => setEditing(cfg.id)}
                onDelete={() => onDelete(cfg.id)}
              />
            ),
          )}

          {sorted.length > COLLAPSE_AT && editing !== "new" && (
            <button
              onClick={() => setShowAll((v) => !v)}
              className="w-full rounded-xl border border-line py-2 text-xs text-muted transition-colors hover:text-txt"
            >
              {showAll ? "Collapse" : `Show all ${sorted.length}`}
            </button>
          )}
        </div>
      )}

      <div className="mt-6 rounded-xl border border-line p-4">
        <div className="flex items-center justify-between">
          <label className="flex items-center gap-2 text-sm text-txt">
            <input
              type="checkbox"
              checked={useSysProxy === true}
              disabled={useSysProxy === null}
              onChange={(e) => void onToggleSysProxy(e.target.checked)}
            />
            Use system proxy
          </label>
          {proxyMsg && (
            <span className={`text-xs ${proxyMsg.ok ? "text-faint" : "text-red"}`}>{proxyMsg.text}</span>
          )}
        </div>
        <p className="mt-1 text-xs text-faint">
          When on, model requests follow the macOS system proxy settings; when off, all model
          requests connect directly. Local addresses (127.0.0.1 / localhost) always connect
          directly. If verification or chat returns 502, your proxy software is usually blocking
          the local port — try turning this off.
        </p>
      </div>
    </div>
  );

  function nextEnabledAfterRemoval(list: ModelConfig[], removedId: string): string {
    const first = list.find((c) => c.id !== removedId && c.enabled);
    return first?.id ?? "";
  }

  async function onToggleSysProxy(next: boolean) {
    const prev = useSysProxy;
    setUseSysProxy(next);
    setProxyMsg(null);
    try {
      const s = (await api.getSettings()) as Record<string, unknown>;
      s.use_system_proxy = next;
      await api.putSettings(s);
      setProxyMsg({ ok: true, text: "Saved" });
    } catch {
      setUseSysProxy(prev);
      setProxyMsg({ ok: false, text: "Failed to save: cannot connect to the runtime" });
    }
  }
}
