"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import { DEFAULT_TOOL_LABELS, refreshToolLabels } from "@/lib/toolLabels";

export function ToolLabelsSettings() {
  // 面板界面文案走 tools.ui 域（工具标签数据本体在 P0 的 tool.labels，勿混用）
  const t = useTranslations("tools.ui");
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
      .catch(() => setMsg(t("loadFailed")));
  }, []);

  // IME（拼音等）合成期间每次按键都触发 onChange；若直接落盘会并发保存
  // 互相覆盖，且可能把拼音中间态写进配置。输入只改本地 state，去抖后落盘。
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const saveSeq = useRef(0);
  const composing = useRef(false);

  async function save(next: Record<string, string>) {
    // 串行化：序号过期的旧保存不再刷 UI / 拉取，避免交错覆盖
    const seq = ++saveSeq.current;
    try {
      const s = await api.getSettings();
      s.tool_labels = next;
      await api.putSettings(s);
      if (seq !== saveSeq.current) return;
      await refreshToolLabels();
      setMsg(t("saved"));
      setTimeout(() => setMsg(""), 2000);
    } catch {
      setMsg(t("saveFailed"));
    }
  }

  function scheduleSave(next: Record<string, string>, immediate = false) {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    const flush = () => {
      saveTimer.current = null;
      if (composing.current) return; // 拼音选字过程中不落盘
      save(next);
    };
    if (immediate) flush();
    else saveTimer.current = setTimeout(flush, 600);
  }

  function blurFlush() {
    if (saveTimer.current) scheduleSave(labels, true);
  }

  function updateLabel(key: string, value: string) {
    const next = { ...labels, [key]: value };
    setLabels(next); // 输入框立即反映，不等网络往返
    scheduleSave(next);
  }

  function removeLabel(key: string) {
    const next = { ...labels };
    delete next[key];
    scheduleSave(next, true);
  }

  function addLabel() {
    const k = newKey.trim();
    const v = newVal.trim();
    if (!k || !v) return;
    if (labels[k]) {
      setMsg(t("alreadyExists", { key: k }));
      return;
    }
    const next = { ...labels, [k]: v };
    setLabels(next);
    scheduleSave(next, true);
    setNewKey("");
    setNewVal("");
  }

  const entries = Object.entries(labels).sort(([a], [b]) => a.localeCompare(b));

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">{t("title")}</h2>
      <p className="mt-1 text-xs text-faint">
        {t.rich("description", {
          code: (chunks) => <code className="text-muted">{chunks}</code>,
          // ICU 模板里不便直写花括号，作为参数注入字面量 "{server}"
          mcpDefault: "{server}",
        })}
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
                onCompositionStart={() => (composing.current = true)}
                onCompositionEnd={() => (composing.current = false)}
                onBlur={blurFlush}
              />
              <button
                onClick={() => removeLabel(key)}
                className="shrink-0 rounded px-1.5 py-0.5 text-xs text-faint hover:bg-card2 hover:text-txt"
                title={t("delete")}
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
            placeholder={t("newKeyPlaceholder")}
            value={newKey}
            onChange={(e) => setNewKey(e.target.value)}
          />
          <span className="text-faint">→</span>
          <input
            className="field flex-1 !py-1 text-xs"
            placeholder={t("newValPlaceholder")}
            value={newVal}
            onChange={(e) => setNewVal(e.target.value)}
            // IME guard (same as the composer): Enter commits a CJK
            // candidate first and must not add mid-composition.
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing && e.keyCode !== 229) {
                addLabel();
              }
            }}
          />
          <button
            onClick={addLabel}
            className="shrink-0 rounded-lg border border-violet/40 px-2.5 py-1 text-xs text-violet hover:bg-violet/10"
          >
            {t("add")}
          </button>
        </div>

        {msg && <div className="mt-3 text-xs text-muted">{msg}</div>}
      </div>
    </div>
  );
}
