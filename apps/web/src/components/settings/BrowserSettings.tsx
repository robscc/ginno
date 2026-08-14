"use client";

import { useCallback, useEffect, useState } from "react";
import { Globe, Save } from "lucide-react";
import * as api from "@/lib/runtime";
import type { ChromeImportStatus } from "@/lib/types";

const DEFAULT_RISKY = [
  "*://*.bank*",
  "*://*.alipay*",
  "*://*.weixin*",
  "*://*.tenpay*",
  "*://*.paypal*",
];

export function BrowserSettings() {
  const [domains, setDomains] = useState<string[]>(DEFAULT_RISKY);
  const [draft, setDraft] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<ChromeImportStatus | null>(null);
  const [profile, setProfile] = useState("Default");
  const [ext, setExt] = useState(false);
  const [force, setForce] = useState(false);

  const load = useCallback(async () => {
    try {
      const s = (await api.getSettings()) as Record<string, unknown>;
      const browser = (s.browser || {}) as Record<string, unknown>;
      const raw = browser.risky_domains;
      setDomains(Array.isArray(raw) && raw.length ? (raw as string[]) : DEFAULT_RISKY);
    } catch {
      setMsg("加载失败：运行时未连接");
    }
    try {
      const st = await api.getChromeImportStatus();
      setStatus(st);
      if (st.profiles?.[0]?.id) setProfile(st.profiles[0].id);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    setBusy(true);
    setMsg("");
    try {
      const cur = (await api.getSettings()) as Record<string, unknown>;
      if (!cur || typeof cur !== "object" || Object.keys(cur).length === 0) {
        setMsg("读取现有设置失败，已中止保存。");
        return;
      }
      const cleaned = domains.map((d) => d.trim()).filter(Boolean);
      await api.putSettings({
        ...cur,
        browser: { ...((cur.browser as object) || {}), risky_domains: cleaned },
      });
      setDomains(cleaned);
      setMsg("已保存。高风险域名导航会强制交还给人，即使特权模式开着。");
    } catch (e) {
      setMsg(`保存失败: ${String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const add = () => {
    const v = draft.trim();
    if (!v) return;
    if (!domains.includes(v)) setDomains([...domains, v]);
    setDraft("");
  };

  const runImport = async () => {
    setBusy(true);
    setMsg("");
    try {
      const r = await api.importChromeProfile({ profile, include_extensions: ext, force });
      setMsg(r.ok ? `已导入 ${r.copied?.length || 0} 项` : r.error || "导入失败");
      await load();
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-2xl px-8 py-8">
      <div className="mb-6 flex items-center gap-2">
        <Globe className="h-5 w-5 text-violet" />
        <h2 className="text-[1rem] font-semibold">内嵌浏览器</h2>
      </div>

      <div className="flex flex-col gap-6">
        <section className="rounded-lg border border-line/60 p-4 text-sm">
          <div className="font-medium text-txt">两套浏览器，别混</div>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-muted">
            <li>
              <span className="text-txt">内嵌浏览器</span>（browser_eval / Space）— 无头 Chrome +
              独立 profile（~/.ginno/browser/profile），画面画在右侧分栏。已登录站点、接管、handoff 走这里。
            </li>
            <li>
              <span className="text-txt">Playwright MCP</span>（mcp_playwright_*）—
              <strong className="text-yellow"> 匿名无头</strong>
              ，没有你的登录态。适合公开页抓取，不要当成已登录浏览器。
            </li>
          </ul>
        </section>

        <section>
          <div className="mb-1 text-sm font-medium text-txt">从系统 Chrome 导入登录态</div>
          <p className="mb-2 text-xs text-faint">
            复制 Cookies / Login Data。Chrome 必须先退出，否则 profile 锁会两边一起坏。
          </p>
          {status?.chrome_running && (
            <div className="mb-2 rounded border border-yellow/40 bg-yellow/10 px-2 py-1 text-xs text-yellow">
              Chrome 正在运行 — 先完全退出再导入。
            </div>
          )}
          {status?.imported && (
            <div className="mb-2 text-xs text-muted">
              已导入自 <span className="font-mono">{status.imported_from || "—"}</span>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <select
              className="field w-auto"
              value={profile}
              onChange={(e) => setProfile(e.target.value)}
            >
              {(status?.profiles || [{ id: "Default", label: "Default" }]).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.label || p.id}
                </option>
              ))}
            </select>
            <label className="flex items-center gap-1 text-muted">
              <input type="checkbox" checked={ext} onChange={(e) => setExt(e.target.checked)} />
              含扩展
            </label>
            <label className="flex items-center gap-1 text-muted">
              <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
              强制覆盖
            </label>
            <button
              disabled={busy}
              onClick={() => void runImport()}
              className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
            >
              导入
            </button>
          </div>
        </section>

        <section>
          <div className="mb-1 text-sm font-medium text-txt">高风险域名</div>
          <p className="mb-2 text-xs text-faint">
            匹配即强制 handoff，即使特权模式开着。支持 glob，如 <code>*://*.alipay*</code>。
          </p>
          <div className="space-y-1.5">
            {domains.map((d, i) => (
              <div key={i} className="flex gap-2">
                <input
                  className="field flex-1 font-mono text-xs"
                  value={d}
                  onChange={(e) => setDomains(domains.map((x, j) => (j === i ? e.target.value : x)))}
                />
                <button
                  onClick={() => setDomains(domains.filter((_, j) => j !== i))}
                  className="rounded-lg border border-line px-2 text-muted hover:text-red"
                >
                  ×
                </button>
              </div>
            ))}
            <div className="flex gap-2">
              <input
                className="field flex-1 font-mono text-xs"
                placeholder="*://*.example-bank*"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && add()}
              />
              <button
                onClick={add}
                className="rounded-lg border border-line2 px-3 text-xs text-muted hover:text-txt"
              >
                添加
              </button>
            </div>
          </div>
        </section>

        <div className="flex items-center gap-2">
          <button
            className="flex items-center gap-1.5 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
            onClick={() => void save()}
            disabled={busy}
          >
            <Save className="h-4 w-4" /> 保存
          </button>
          {msg && <p className="text-xs text-muted">{msg}</p>}
        </div>
      </div>
    </div>
  );
}
