"use client";

import { useState } from "react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
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

type Feedback = { text: string; ok: boolean };

export function AgentsSettings() {
  const g = useGinno();
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
  function addToolPattern(a: AgentConfig) {
    const raw = (toolInput[a.id] || "").trim();
    setToolInput((t) => ({ ...t, [a.id]: "" }));
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

  async function save(a: AgentConfig) {
    const data = {
      name: get(a.id, "name", a.name).trim() || a.id,
      system_prompt: get(a.id, "system_prompt", a.system_prompt),
      tools_allow: toolsFor(a),
      provider: get(a.id, "provider", a.provider),
      model: get(a.id, "model", a.model),
      icon: get(a.id, "icon", a.icon),
      color: get(a.id, "color", a.color),
    };
    setBusy((b) => ({ ...b, [a.id]: true }));
    clearMsg(a.id);
    try {
      // The backend always returns HTTP 200; failures are {ok:false, error}.
      const r = await api.updateAgent(a.id, data);
      if (r.ok) {
        setMsg((m) => ({ ...m, [a.id]: { text: "已保存", ok: true } }));
        g.reloadAgents();
      } else {
        setMsg((m) => ({ ...m, [a.id]: { text: r.error || "保存失败", ok: false } }));
      }
    } catch {
      setMsg((m) => ({ ...m, [a.id]: { text: "无法连接运行时（sidecar 未启动？）", ok: false } }));
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
        setMsg((m) => ({ ...m, [id]: { text: "删除失败", ok: false } }));
      }
    } catch {
      setMsg((m) => ({ ...m, [id]: { text: "无法连接运行时（sidecar 未启动？）", ok: false } }));
    }
  }

  const trimmedId = newId.trim();
  const idError =
    trimmedId && !ID_RE.test(trimmedId)
      ? "id 仅支持小写字母、数字、- 和 _，且以字母或数字开头"
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
        setCreateMsg({ text: "已创建", ok: true });
        g.reloadAgents();
      } else {
        setCreateMsg({ text: r.error || "创建失败", ok: false });
      }
    } catch {
      setCreateMsg({ text: "无法连接运行时（sidecar 未启动？）", ok: false });
    } finally {
      setCreateBusy(false);
    }
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">Agent 管理</h2>
      <p className="mt-1 text-sm text-muted">每个 Agent 独立的 persona / 工具子集 / 模型 / 外观。</p>
      <p className="mt-0.5 text-xs text-faint">存于 ~/.ginno/agents/&lt;id&gt;.json。</p>
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
              ? `provider「${cur.provider}」未启用，新会话将回退到默认（${g.defaultProvider || "custom"}）`
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
                  delete
                </button>
              </div>
              <label className="field-label mt-2">name</label>
              <input
                className="field"
                value={cur.name}
                onChange={(e) => set(a.id, "name", e.target.value)}
              />
              <label className="field-label mt-2">System prompt</label>
              <textarea
                className="field"
                rows={4}
                value={get(a.id, "system_prompt", a.system_prompt)}
                onChange={(e) => set(a.id, "system_prompt", e.target.value)}
              />
              <div className="mt-0.5 text-[11px] text-faint">
                名称 / System prompt / 工具白名单保存后对所有会话的下一轮立即生效。
              </div>
              <label className="field-label mt-2">tools_allow</label>
              <div className="flex flex-wrap items-center gap-1.5 rounded-lg border border-line px-2 py-1.5">
                {tools.map((t) => (
                  <span
                    key={t}
                    className="flex items-center gap-1 rounded-md px-1.5 py-0.5 font-mono text-[11px]"
                    style={{ background: hex + "22", color: hex }}
                  >
                    {t}
                    <button
                      onClick={() => setTools(a.id, tools.filter((x) => x !== t))}
                      className="opacity-60 hover:opacity-100"
                      title="移除"
                    >
                      ×
                    </button>
                  </span>
                ))}
                <input
                  className="min-w-[7rem] flex-1 bg-transparent text-xs text-txt outline-none placeholder:text-faint"
                  placeholder={tools.length ? "添加 pattern…" : "留空 = 允许所有工具"}
                  list={`tools-${a.id}`}
                  value={toolInput[a.id] || ""}
                  onChange={(e) => setToolInput((t) => ({ ...t, [a.id]: e.target.value }))}
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
              <div className="mt-0.5 text-[11px] text-faint">
                支持 glob（如 mcp_*、todo_*）；render / workflow / artifact 类工具始终可用，不受此限制。
              </div>
              <div className="mt-2 grid grid-cols-3 gap-2">
                <div>
                  <label className="field-label">provider</label>
                  <select
                    className="field"
                    value={cur.provider}
                    onChange={(e) => set(a.id, "provider", e.target.value)}
                  >
                    <option value="">跟随默认（{g.defaultProvider || "custom"}）</option>
                    {cur.provider && !(cur.provider in g.providers) && (
                      <option value={cur.provider}>{cur.provider}</option>
                    )}
                    {Object.entries(g.providers).map(([id, p]) => (
                      <option key={id} value={id}>
                        {id}
                        {p.enabled ? "" : "（未启用）"}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="field-label">model</label>
                  <input
                    className="field"
                    value={get(a.id, "model", a.model)}
                    placeholder={g.providers[cur.provider]?.default_model || "provider 默认模型"}
                    onChange={(e) => set(a.id, "model", e.target.value)}
                  />
                </div>
                <div>
                  <label className="field-label">icon</label>
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
              <label className="field-label mt-2">color</label>
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
              <div className="mt-1.5 text-[11px] text-faint">
                provider / model 仅对之后新建的会话生效；颜色、图标即时生效（已有会话的图标沿用创建时的）。
              </div>
              <div className="mt-2 flex items-center">
                <button
                  onClick={() => save(a)}
                  disabled={busy[a.id]}
                  className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
                >
                  {busy[a.id] ? "Saving…" : "Save"}
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
            暂无 Agent — 可能运行时未就绪；删除最后一个 Agent 后会自动恢复默认的 dev / research / writer。
          </div>
        )}
      </div>
      <div className="mt-4">
        <div className="flex items-center gap-2">
          <input
            className="field w-40"
            placeholder="id (a-z, 0-9, -, _)"
            value={newId}
            onChange={(e) => {
              setNewId(e.target.value);
              setCreateMsg(null);
            }}
          />
          <input
            className="field w-48"
            placeholder="name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
          />
          <button
            onClick={create}
            disabled={!trimmedId || !!idError || createBusy}
            className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            {createBusy ? "Adding…" : "Add"}
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
          title="删除 Agent"
          message={
            `删除 Agent「${deleteTarget}」？\n` +
            `其记忆目录 ~/.ginno/agents/${deleteTarget}/ 不会被删除。` +
            (g.agents.length <= 1
              ? "\n这是最后一个 Agent，删除后会自动恢复默认的 dev / research / writer。"
              : "")
          }
          confirmLabel="删除"
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
