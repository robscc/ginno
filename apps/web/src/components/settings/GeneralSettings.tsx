"use client";

import { useEffect, useState } from "react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";

export function applyTheme(t: string) {
  if (typeof document === "undefined") return;
  document.documentElement.classList.toggle("light", t === "light");
  try {
    localStorage.setItem("ginno-theme", t);
  } catch {
    /* ignore */
  }
}

export function GeneralSettings() {
  const g = useGinno();
  const [theme, setTheme] = useState<string>("dark");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    let t = "dark";
    try {
      t = localStorage.getItem("ginno-theme") || "dark";
    } catch {
      /* ignore */
    }
    setTheme(t);
    applyTheme(t);
  }, []);

  function setThemeAndApply(t: string) {
    setTheme(t);
    applyTheme(t);
  }
  async function setDefault(p: string) {
    await api.putProviders(g.providers, p);
    g.reloadProviders();
    setMsg("default provider → " + p);
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">通用设置</h2>
      <div className="mt-4 max-w-md space-y-4">
        <div>
          <label className="field-label">默认模型提供商</label>
          <select className="field" value={g.defaultProvider} onChange={(e) => setDefault(e.target.value)}>
            {Object.keys(g.providers).map((p) => (
              <option key={p} value={p}>
                {p}
                {g.providers[p].enabled ? "" : " (disabled)"}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="field-label">主题</label>
          <div className="flex gap-2">
            {["dark", "light"].map((t) => (
              <button
                key={t}
                onClick={() => setThemeAndApply(t)}
                className={
                  "rounded-lg border px-3 py-1.5 text-xs " +
                  (theme === t ? "border-violet text-txt" : "border-line text-muted")
                }
              >
                {t}
              </button>
            ))}
          </div>
        </div>
        <div>
          <label className="field-label">工作目录</label>
          <div className="field bg-base/40 text-muted">
            ~/workspace/&lt;project&gt; （Agent 元数据在 ~/.ginno/projects/）
          </div>
        </div>
        {msg && <div className="text-xs text-muted">{msg}</div>}
      </div>
    </div>
  );
}
