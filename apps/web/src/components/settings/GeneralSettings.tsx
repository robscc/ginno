"use client";

import { useEffect, useState } from "react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";

export function applyTheme(t: string) {
  if (typeof document === "undefined") return;
  document.documentElement.classList.toggle("light", t === "light");
  // Native areas outside the WKWebView show the window background; keep
  // them in sync with the theme (black bar in light mode otherwise).
  try {
    import("@tauri-apps/api/event")
      .then(({ emit }) => emit("ginno:window-bg", t === "light" ? "#f8f8fa" : "#0a0a0f"))
      .catch(() => {});
  } catch {
    /* browser dev */
  }
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
  const [bypass, setBypass] = useState(true);
  const [engine, setEngine] = useState<string>("cef");

  useEffect(() => {
    let t = "dark";
    try {
      t = localStorage.getItem("ginno-theme") || "dark";
    } catch {
      /* ignore */
    }
    setTheme(t);
    applyTheme(t);
    api
      .getSettings()
      .then((s) => {
        setBypass((s as Record<string, unknown>).bypass_permissions !== false);
        setEngine((s as Record<string, unknown>).browser_engine === "chrome" ? "chrome" : "cef");
      })
      .catch(() => {});
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
  async function toggleBypass(v: boolean) {
    try {
      const s = (await api.getSettings()) as Record<string, unknown>;
      s.bypass_permissions = v;
      await api.putSettings(s);
      setBypass(v);
      setMsg(v ? "特权模式已开启：所有工具直接执行，不再询问" : "特权模式已关闭：按权限策略询问 / 拦截");
    } catch {
      setMsg("保存失败");
    }
  }
  async function setEnginePref(v: string) {
    try {
      const s = (await api.getSettings()) as Record<string, unknown>;
      s.browser_engine = v;
      await api.putSettings(s);
      setEngine(v);
      await api.resetBrowser();
      setMsg(v === "cef" ? "浏览器引擎 → 内嵌 CEF（默认）" : "浏览器引擎 → Chrome screencast");
    } catch {
      setMsg("保存失败");
    }
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
          <label className="flex items-center gap-2 text-sm text-txt">
            <input type="checkbox" checked={bypass} onChange={(e) => toggleBypass(e.target.checked)} />
            特权模式（跳过所有权限确认，允许执行一切命令）
          </label>
          <p className="mt-1 text-xs text-faint">
            开启后 Agent 调用任何工具都不再询问、不被权限策略拦截（含 Bash/Write 等危险操作）。默认开启；关闭后按权限策略询问/拦截。注意：你配置的 PreToolUse hook 仍会执行（hook 是自定义规则，始终生效）。
          </p>
        </div>
        <div>
          <label className="field-label">浏览器引擎</label>
          <div className="flex gap-2">
            <button
              onClick={() => setEnginePref("cef")}
              className={
                "rounded-lg border px-3 py-1.5 text-xs " +
                (engine === "cef" ? "border-violet text-txt" : "border-line text-muted")
              }
            >
              内嵌 CEF（默认）
            </button>
            <button
              onClick={() => setEnginePref("chrome")}
              className={
                "rounded-lg border px-3 py-1.5 text-xs " +
                (engine === "chrome" ? "border-violet text-txt" : "border-line text-muted")
              }
            >
              Chrome screencast
            </button>
          </div>
          <p className="mt-1 text-xs text-faint">
            内嵌 CEF：真 Chromium 视图嵌在窗口里（需要打包了 CEF 的构建）。Chrome
            screencast：headless Chrome 截屏画进分栏。切换后立即生效。
          </p>
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
