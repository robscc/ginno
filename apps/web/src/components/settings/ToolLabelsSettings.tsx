"use client";

import { useEffect, useState } from "react";
import * as api from "@/lib/runtime";
import { DEFAULT_TOOL_LABELS, refreshToolLabels } from "@/lib/toolLabels";

export function ToolLabelsSettings() {
  const [labels, setLabels] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState("");
  const [newKey, setNewKey] = useState("");
  const [newVal, setNewVal] = useState("");

  useEffect(() => {
    api
      .getSettings()
      .then((s) => {
        // Show built-in defaults merged with any user overrides so every
        // label is visible/editable even on pre-existing settings files.
        const user = (s.tool_labels as Record<string, string>) || {};
        setLabels({ ...DEFAULT_TOOL_LABELS, ...user });
      })
      .catch(() => setMsg("加载失败"));
  }, []);

  async function save(next: Record<string, string>) {
    try {
      const s = await api.getSettings();
      s.tool_labels = next;
      await api.putSettings(s);
      setLabels(next);
      await refreshToolLabels();
      setMsg("已保存");
      setTimeout(() => setMsg(""), 2000);
    } catch {
      setMsg("保存失败");
    }
  }

  function updateLabel(key: string, value: string) {
    const next = { ...labels, [key]: value };
    save(next);
  }

  function removeLabel(key: string) {
    const next = { ...labels };
    delete next[key];
    save(next);
  }

  function addLabel() {
    const k = newKey.trim();
    const v = newVal.trim();
    if (!k || !v) return;
    if (labels[k]) {
      setMsg(`"${k}" 已存在`);
      return;
    }
    const next = { ...labels, [k]: v };
    save(next);
    setNewKey("");
    setNewVal("");
  }

  const entries = Object.entries(labels).sort(([a], [b]) => a.localeCompare(b));

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">工具标签</h2>
      <p className="mt-1 text-xs text-faint">
        自定义工具调用气泡的显示名称。MCP 工具（以 <code className="text-muted">mcp_</code> 开头）未配置时自动显示为&ldquo;正在调用MCP：&#123;server&#125;&rdquo;。
      </p>

      <div className="mt-5 max-w-lg">
        {/* Existing labels */}
        <div className="space-y-2">
          {entries.map(([key, val]) => (
            <div key={key} className="flex items-center gap-2">
              <code className="w-36 shrink-0 truncate text-xs text-muted" title={key}>
                {key}
              </code>
              <span className="text-faint">→</span>
              <input
                className="field flex-1 !py-1 text-xs"
                value={val}
                onChange={(e) => updateLabel(key, e.target.value)}
              />
              <button
                onClick={() => removeLabel(key)}
                className="shrink-0 rounded px-1.5 py-0.5 text-xs text-faint hover:bg-card2 hover:text-txt"
                title="删除"
              >
                ✕
              </button>
            </div>
          ))}
        </div>

        {/* Add new */}
        <div className="mt-4 flex items-center gap-2">
          <input
            className="field w-36 shrink-0 !py-1 text-xs"
            placeholder="工具名"
            value={newKey}
            onChange={(e) => setNewKey(e.target.value)}
          />
          <span className="text-faint">→</span>
          <input
            className="field flex-1 !py-1 text-xs"
            placeholder="显示名称"
            value={newVal}
            onChange={(e) => setNewVal(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && addLabel()}
          />
          <button
            onClick={addLabel}
            className="shrink-0 rounded-lg border border-violet/40 px-2.5 py-1 text-xs text-violet hover:bg-violet/10"
          >
            添加
          </button>
        </div>

        {msg && <div className="mt-3 text-xs text-muted">{msg}</div>}
      </div>
    </div>
  );
}
