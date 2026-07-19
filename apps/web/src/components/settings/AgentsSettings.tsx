"use client";

import { useState } from "react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import { agentHex } from "@/lib/theme";
import type { AgentConfig } from "@/lib/types";

export function AgentsSettings() {
  const g = useGinno();
  const [draft, setDraft] = useState<Record<string, Record<string, string>>>({});
  const [newId, setNewId] = useState("");
  const [newName, setNewName] = useState("");

  const get = (id: string, field: string, fallback: string): string =>
    draft[id]?.[field] ?? fallback;
  const set = (id: string, field: string, val: string) =>
    setDraft((d) => ({ ...d, [id]: { ...d[id], [field]: val } }));

  async function save(a: AgentConfig) {
    const data = {
      system_prompt: get(a.id, "system_prompt", a.system_prompt),
      tools_allow: get(a.id, "tools_allow", (a.tools_allow || []).join(", "))
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      provider: get(a.id, "provider", a.provider),
      model: get(a.id, "model", a.model),
    };
    await api.updateAgent(a.id, data);
    g.reloadAgents();
  }
  async function del(id: string) {
    if (confirm("delete " + id + "?")) {
      await api.deleteAgent(id);
      g.reloadAgents();
    }
  }
  async function create() {
    if (!newId.trim()) return;
    await api.createAgent({ id: newId.trim(), name: newName.trim() || newId.trim() });
    setNewId("");
    setNewName("");
    g.reloadAgents();
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">Agent 管理</h2>
      <p className="mt-1 text-sm text-muted">每个 Agent 独立的 persona / 工具子集 / 模型 / 记忆。</p>
      <div className="mt-4 space-y-3">
        {g.agents.map((a) => {
          const hex = agentHex(a.color);
          return (
            <div key={a.id} className="rounded-xl border border-line bg-card p-3">
              <div className="flex items-center gap-2">
                <span
                  className="flex h-6 w-6 items-center justify-center rounded-md text-xs"
                  style={{ background: hex + "22", color: hex }}
                >
                  ●
                </span>
                <span className="font-medium text-txt">{a.name}</span>
                <span className="text-xs text-faint">@{a.id}</span>
                <button onClick={() => del(a.id)} className="ml-auto text-xs text-faint hover:text-red">
                  delete
                </button>
              </div>
              <label className="field-label mt-2">System prompt</label>
              <textarea
                className="field"
                rows={3}
                value={get(a.id, "system_prompt", a.system_prompt)}
                onChange={(e) => set(a.id, "system_prompt", e.target.value)}
              />
              <label className="field-label mt-2">tools_allow (comma-separated patterns)</label>
              <input
                className="field"
                value={get(a.id, "tools_allow", (a.tools_allow || []).join(", "))}
                onChange={(e) => set(a.id, "tools_allow", e.target.value)}
              />
              <div className="mt-2 grid grid-cols-2 gap-2">
                <div>
                  <label className="field-label">provider</label>
                  <input
                    className="field"
                    value={get(a.id, "provider", a.provider)}
                    onChange={(e) => set(a.id, "provider", e.target.value)}
                  />
                </div>
                <div>
                  <label className="field-label">model</label>
                  <input
                    className="field"
                    value={get(a.id, "model", a.model)}
                    onChange={(e) => set(a.id, "model", e.target.value)}
                  />
                </div>
              </div>
              <button
                onClick={() => save(a)}
                className="mt-2 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white"
              >
                Save
              </button>
            </div>
          );
        })}
      </div>
      <div className="mt-4 flex items-center gap-2">
        <input className="field w-40" placeholder="id" value={newId} onChange={(e) => setNewId(e.target.value)} />
        <input className="field w-48" placeholder="name" value={newName} onChange={(e) => setNewName(e.target.value)} />
        <button onClick={create} className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white">
          Add
        </button>
      </div>
    </div>
  );
}
