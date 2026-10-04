"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { ConnectorInfo } from "@/lib/runtime";
import { AGENT_HEX, agentHex } from "@/lib/theme";
import { Icon } from "@/components/icons";
import { ConfirmModal } from "@/components/ConfirmModal";
import type { AgentConfig } from "@/lib/types";

// Agent id doubles as the filename ~/.ginno/agents/<id>.json and the memory
// dir name, and the backend accepts almost anything (incl. path traversal via
// "../x"). Enforce a safe slug client-side.
const ID_RE = /^[a-z0-9][a-z0-9_-]*$/;

// Subset of components/icons.tsx that makes sense as an agent avatar.
const AGENT_ICONS = [
  "terminal",
  "search",
  "pen-line",
  "message-square",
  "book",
  "star",
  "zap",
  "boxes",
  "workflow",
  "list",
  "clock",
  "eye",
];

// Real tool names / prefixes from the runtime (tools/*.py), for suggestions.
const TOOL_SUGGESTIONS = ["*", "mcp_*", "todo_*", "read_*", "write_*", "grep_*", "bash"];

// Capability groups (connector-module-design.md §8): connectors sharing one
// tool set render as ONE toggle. The browser dual-track (extension + profile
// both serve browser_* tools) is a single「浏览器」capability; a connector
// outside the group list gets its own row when one is registered later.
const BROWSER_IDS = ["chrome-extension", "browser-profile"];

type ConnGroup = { key: string; label: string; ids: string[]; conns: ConnectorInfo[] };

// browser 组的 label 存 catalog key（settings.agents.groups.browser），其余组
// 直接显示连接器名（数据，非 UI 文案）。
function connectorGroups(conns: ConnectorInfo[]): ConnGroup[] {
  const groups: ConnGroup[] = [];
  const browser = conns.filter((c) => BROWSER_IDS.includes(c.id));
  if (browser.length) {
    groups.push({
      key: "browser",
      label: "browser",
      ids: browser.map((c) => c.id),
      conns: browser,
    });
  }
  for (const c of conns) {
    if (!BROWSER_IDS.includes(c.id)) groups.push({ key: c.id, label: c.name, ids: [c.id], conns: [c] });
  }
  return groups;
}

// Capability status dot — best-of across the group's tracks: any connected →
// green; else installing → blue; error → red; a NON-idle disconnect → yellow
// (B 轨 idle = 按需未运行,属正常态,不亮黄);rest gray. Mirrors the sidebar
// aggregate-dot philosophy in connectors/registry.py. titleKey 存 catalog key
// （字面量联合，保证 settings.agents.dot.<key> 可被类型检查）。
type DotKey = "connected" | "installing" | "error" | "disconnected" | "notConnected";
function groupDot(conns: ConnectorInfo[]): { cls: string; titleKey: DotKey } {
  if (conns.some((c) => c.status === "connected")) return { cls: "bg-green-500", titleKey: "connected" };
  if (conns.some((c) => c.status === "installing")) return { cls: "bg-blue-500", titleKey: "installing" };
  if (conns.some((c) => c.status === "error")) return { cls: "bg-red-500", titleKey: "error" };
  const liveDisconnect = conns.some(
    (c) => c.status === "disconnected" && !(c.extra || {}).idle,
  );
  if (liveDisconnect) return { cls: "bg-yellow-500", titleKey: "disconnected" };
  return { cls: "bg-neutral-400 dark:bg-neutral-500", titleKey: "notConnected" };
}

type Feedback = { text: string; ok: boolean };

export function AgentsSettings() {
  const g = useGinno();
  const t = useTranslations("settings.agents");
  const [draft, setDraft] = useState<Record<string, Record<string, string>>>({});
  const [toolsDraft, setToolsDraft] = useState<Record<string, string[]>>({});
  const [toolInput, setToolInput] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<Record<string, Feedback>>({});
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [newId, setNewId] = useState("");
  const [newName, setNewName] = useState("");
  const [createMsg, setCreateMsg] = useState<Feedback | null>(null);
  const [createBusy, setCreateBusy] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [conns, setConns] = useState<ConnectorInfo[]>([]);
  const [denyDraft, setDenyDraft] = useState<Record<string, string[]>>({});

  // Connector list feeds the per-agent capability toggles + status dots.
  // 10s poll matches the sidebar aggregate dot's cadence.
  useEffect(() => {
    let alive = true;
    const refresh = async () => {
      try {
        const r = await api.listConnectors();
        if (alive) setConns(r.connectors || []);
      } catch {
        /* sidecar not up yet — keep last state */
      }
    };
    void refresh();
    const timer = setInterval(refresh, 10_000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  const get = (id: string, field: string, fallback: string): string =>
    draft[id]?.[field] ?? fallback;
  const clearMsg = (id: string) =>
    setMsg((m) => {
      if (!m[id]) return m;
      const next = { ...m };
      delete next[id];
      return next;
    });
  const set = (id: string, field: string, val: string) => {
    setDraft((d) => ({ ...d, [id]: { ...d[id], [field]: val } }));
    clearMsg(id);
  };

  const toolsFor = (a: AgentConfig): string[] => toolsDraft[a.id] ?? a.tools_allow ?? [];
  const setTools = (id: string, tools: string[]) => {
    setToolsDraft((d) => ({ ...d, [id]: tools }));
    clearMsg(id);
  };
  const denyFor = (a: AgentConfig): string[] => denyDraft[a.id] ?? a.connectors_deny ?? [];
  // Mirrors the backend rule: a capability stays ON while ANY of its tracks is
  // not denied (registry.tool_denied denies only when all providers are).
  const groupOn = (a: AgentConfig, ids: string[]): boolean =>
    ids.some((i) => !denyFor(a).includes(i));
  const toggleGroup = (a: AgentConfig, ids: string[], on: boolean) => {
    const cur = new Set(denyFor(a));
    ids.forEach((i) => (on ? cur.delete(i) : cur.add(i)));
    setDenyDraft((d) => ({ ...d, [a.id]: [...cur] }));
    clearMsg(a.id);
  };
  function addToolPattern(a: AgentConfig) {
    const raw = (toolInput[a.id] || "").trim();
    setToolInput((m) => ({ ...m, [a.id]: "" }));
    if (!raw) return;
    const next = [...toolsFor(a)];
    for (const p of raw
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean)) {
      if (!next.includes(p)) next.push(p);
    }
    setTools(a.id, next);
  }

  const enabledProviders = new Set(
    Object.entries(g.providers)
      .filter(([, p]) => p.enabled)
      .map(([id]) => id),
  );
  const connGroups = connectorGroups(conns);

  async function save(a: AgentConfig) {
    const data = {
      name: get(a.id, "name", a.name).trim() || a.id,
      system_prompt: get(a.id, "system_prompt", a.system_prompt),
      tools_allow: toolsFor(a),
      provider: get(a.id, "provider", a.provider),
      model: get(a.id, "model", a.model),
      icon: get(a.id, "icon", a.icon),
      color: get(a.id, "color", a.color),
      connectors_deny: denyFor(a),
    };
    setBusy((b) => ({ ...b, [a.id]: true }));
    clearMsg(a.id);
    try {
      // The backend always returns HTTP 200; failures are {ok:false, error}.
      const r = await api.updateAgent(a.id, data);
      if (r.ok) {
        setMsg((m) => ({ ...m, [a.id]: { text: t("saved"), ok: true } }));
        g.reloadAgents();
      } else {
        setMsg((m) => ({ ...m, [a.id]: { text: r.error || t("saveFailed"), ok: false } }));
      }
    } catch {
      setMsg((m) => ({ ...m, [a.id]: { text: t("connError"), ok: false } }));
    } finally {
      setBusy((b) => ({ ...b, [a.id]: false }));
    }
  }
  async function del(id: string) {
    // Native confirm() is blocked in the Tauri WKWebView (always cancels),
    // which made delete a no-op in the packaged app — use the app modal.
    setDeleteTarget(id);
  }

  async function doDelete(id: string) {
    try {
      const r = await api.deleteAgent(id);
      if (r.ok) {
        g.reloadAgents();
      } else {
        setMsg((m) => ({ ...m, [id]: { text: t("deleteFailed"), ok: false } }));
      }
    } catch {
      setMsg((m) => ({ ...m, [id]: { text: t("connError"), ok: false } }));
    }
  }

  const trimmedId = newId.trim();
  const idError =
    trimmedId && !ID_RE.test(trimmedId)
      ? t("idError")
      : "";

  async function create() {
    const id = newId.trim();
    if (!id || !ID_RE.test(id)) return;
    setCreateBusy(true);
    setCreateMsg(null);
    try {
      const r = await api.createAgent({ id, name: newName.trim() || id });
      if (r.ok) {
        setNewId("");
        setNewName("");
        setCreateMsg({ text: t("created"), ok: true });
        g.reloadAgents();
      } else {
        setCreateMsg({ text: r.error || t("createFailed"), ok: false });
      }
    } catch {
      setCreateMsg({ text: t("connError"), ok: false });
    } finally {
      setCreateBusy(false);
    }
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">{t("title")}</h2>
      <p className="mt-1 text-sm text-muted">{t("description")}</p>
      <p className="mt-0.5 text-xs text-faint">{t("storedIn")}</p>
      <div className="mt-4 space-y-3">
        {g.agents.map((a) => {
          const cur = {
            name: get(a.id, "name", a.name),
            icon: get(a.id, "icon", a.icon),
            color: get(a.id, "color", a.color),
            provider: get(a.id, "provider", a.provider),
          };
          const hex = agentHex(cur.color);
          const fb = msg[a.id];
          const tools = toolsFor(a);
          // agent.provider must be an *enabled* provider or new sessions
          // silently fall back to the global default (server.py
          // _resolve_provider_model) — surface that instead of hiding it.
          const providerWarn =
            cur.provider && !enabledProviders.has(cur.provider)
              ? t("providerWarn", { provider: cur.provider, default: g.defaultProvider || "custom" })
              : "";
          const iconOptions = AGENT_ICONS.includes(cur.icon)
            ? AGENT_ICONS
            : [cur.icon, ...AGENT_ICONS];
          return (
            <div key={a.id} className="rounded-xl border border-line bg-card p-3">
              <div className="flex items-center gap-2">
                <span
                  className="flex h-6 w-6 items-center justify-center rounded-md"
                  style={{ background: hex + "22", color: hex }}
                >
                  <Icon name={cur.icon} className="h-3.5 w-3.5" />
                </span>
                <span className="font-medium text-txt">{cur.name}</span>
                <span className="text-xs text-faint">@{a.id}</span>
                <button onClick={() => del(a.id)} className="ml-auto text-xs text-faint hover:text-red">
                  {t("delete")}
                </button>
              </div>
              <label className="field-label mt-2">{t("nameLabel")}</label>
              <input
                className="field"
                value={cur.name}
                onChange={(e) => set(a.id, "name", e.target.value)}
              />
              <label className="field-label mt-2">{t("systemPromptLabel")}</label>
              <textarea
                className="field"
                rows={4}
                value={get(a.id, "system_prompt", a.system_prompt)}
                onChange={(e) => set(a.id, "system_prompt", e.target.value)}
              />
              <div className="mt-0.5 text-[11px] text-faint">{t("takeEffectNote")}</div>
              <label className="field-label mt-2">{t("toolsAllowLabel")}</label>
              <div className="flex flex-wrap items-center gap-1.5 rounded-lg border border-line px-2 py-1.5">
                {tools.map((tool) => (
                  <span
                    key={tool}
                    className="flex items-center gap-1 rounded-md px-1.5 py-0.5 font-mono text-[11px]"
                    style={{ background: hex + "22", color: hex }}
                  >
                    {tool}
                    <button
                      onClick={() => setTools(a.id, tools.filter((x) => x !== tool))}
                      className="opacity-60 hover:opacity-100"
                      title={t("removeTitle")}
                    >
                      ×
                    </button>
                  </span>
                ))}
                <input
                  className="min-w-[7rem] flex-1 bg-transparent text-xs text-txt outline-none placeholder:text-faint"
                  placeholder={tools.length ? t("addPatternPlaceholder") : t("emptyAllowsAll")}
                  list={`tools-${a.id}`}
                  value={toolInput[a.id] || ""}
                  onChange={(e) => setToolInput((m) => ({ ...m, [a.id]: e.target.value }))}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === ",") {
                      e.preventDefault();
                      addToolPattern(a);
                    } else if (e.key === "Backspace" && !toolInput[a.id]) {
                      setTools(a.id, tools.slice(0, -1));
                    }
                  }}
                  onBlur={() => addToolPattern(a)}
                />
                <datalist id={`tools-${a.id}`}>
                  {TOOL_SUGGESTIONS.map((s) => (
                    <option key={s} value={s} />
                  ))}
                </datalist>
              </div>
              <div className="mt-0.5 text-[11px] text-faint">{t("globsNote")}</div>
              {connGroups.length > 0 && (
                <>
                  <label className="field-label mt-2">{t("connectorsLabel")}</label>
                  <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1.5">
                    {connGroups.map((grp) => {
                      const dot = groupDot(grp.conns);
                      return (
                        <label
                          key={grp.key}
                          className="flex cursor-pointer items-center gap-2 text-sm text-txt"
                        >
                          <input
                            type="checkbox"
                            checked={groupOn(a, grp.ids)}
                            onChange={(e) => toggleGroup(a, grp.ids, e.target.checked)}
                          />
                          <span
                            title={t(`dot.${dot.titleKey}`)}
                            className={`inline-block h-1.5 w-1.5 rounded-full ${dot.cls}`}
                          />
                          {grp.key === "browser" ? t("groups.browser") : grp.label}
                        </label>
                      );
                    })}
                  </div>
                  <div className="mt-0.5 text-[11px] text-faint">{t("connectorsNote")}</div>
                </>
              )}
              <div className="mt-2 grid grid-cols-3 gap-2">
                <div>
                  <label className="field-label">{t("providerLabel")}</label>
                  <select
                    className="field"
                    value={cur.provider}
                    onChange={(e) => set(a.id, "provider", e.target.value)}
                  >
                    <option value="">{t("followDefault", { provider: g.defaultProvider || "custom" })}</option>
                    {cur.provider && !(cur.provider in g.providers) && (
                      <option value={cur.provider}>{cur.provider}</option>
                    )}
                    {Object.entries(g.providers).map(([id, p]) => (
                      <option key={id} value={id}>
                        {id}
                        {p.enabled ? "" : t("disabledSuffix")}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="field-label">{t("modelLabel")}</label>
                  <input
                    className="field"
                    value={get(a.id, "model", a.model)}
                    placeholder={g.providers[cur.provider]?.default_model || t("providerDefaultModel")}
                    onChange={(e) => set(a.id, "model", e.target.value)}
                  />
                </div>
                <div>
                  <label className="field-label">{t("iconLabel")}</label>
                  <select
                    className="field"
                    value={cur.icon}
                    onChange={(e) => set(a.id, "icon", e.target.value)}
                  >
                    {iconOptions.map((i) => (
                      <option key={i} value={i}>
                        {i}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              {providerWarn && (
                <div className="mt-1 rounded-md border border-yellow/40 bg-yellow/10 px-2 py-1 text-[11px] text-yellow">
                  {providerWarn}
                </div>
              )}
              <label className="field-label mt-2">{t("colorLabel")}</label>
              <div className="mt-1 flex items-center gap-1.5">
                {Object.entries(AGENT_HEX).map(([key, h]) => (
                  <button
                    key={key}
                    title={key}
                    onClick={() => set(a.id, "color", key)}
                    className={`h-5 w-5 rounded-full border-2 transition-colors ${
                      cur.color === key ? "border-txt" : "border-transparent hover:border-line2"
                    }`}
                    style={{ background: h }}
                  />
                ))}
              </div>
              <div className="mt-1.5 text-[11px] text-faint">{t("applyNote")}</div>
              <div className="mt-2 flex items-center">
                <button
                  onClick={() => save(a)}
                  disabled={busy[a.id]}
                  className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
                >
                  {busy[a.id] ? t("saving") : t("save")}
                </button>
                {fb && (
                  <span className={`ml-2 text-xs ${fb.ok ? "text-violet" : "text-red"}`}>{fb.text}</span>
                )}
              </div>
            </div>
          );
        })}
        {g.agents.length === 0 && (
          <div className="rounded-xl border border-dashed border-line px-4 py-10 text-center text-xs text-faint">
            {t("empty")}
          </div>
        )}
      </div>
      <div className="mt-4">
        <div className="flex items-center gap-2">
          <input
            className="field w-40"
            placeholder={t("idPlaceholder")}
            value={newId}
            onChange={(e) => {
              setNewId(e.target.value);
              setCreateMsg(null);
            }}
          />
          <input
            className="field w-48"
            placeholder={t("namePlaceholder")}
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
          />
          <button
            onClick={create}
            disabled={!trimmedId || !!idError || createBusy}
            className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            {createBusy ? t("adding") : t("add")}
          </button>
          {createMsg && (
            <span className={`text-xs ${createMsg.ok ? "text-violet" : "text-red"}`}>
              {createMsg.text}
            </span>
          )}
        </div>
        {idError && <div className="mt-1 text-xs text-red">{idError}</div>}
      </div>

      {deleteTarget && (
        <ConfirmModal
          title={t("deleteAgentTitle")}
          message={
            t("confirmDelete", { id: deleteTarget }) +
            (g.agents.length <= 1 ? t("confirmDeleteLast") : "")
          }
          confirmLabel={t("deleteLabel")}
          onConfirm={() => {
            const id = deleteTarget;
            setDeleteTarget(null);
            void doDelete(id);
          }}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}
