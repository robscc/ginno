"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Camera, Download, Globe, Maximize2, Minimize2, Plus, RotateCw, Settings2, X } from "lucide-react";
import * as api from "@/lib/runtime";
import type { BrowserDownload, BrowserState, BrowserTab, ChromeImportStatus } from "@/lib/types";

// Landing page for tabs a human opens by hand. about:blank reads
// as "broken" in the native tile; a portal page is immediately usable.
const HUMAN_HOME = "https://www.baidu.com/";

export function BrowserPane({
  sessionId,
  handoff,
  maximized,
  onToggleMaximize,
  onTakeOver,
  onClose,
}: {
  sessionId?: string | null;
  handoff?: { space?: string; url?: string; reason?: string } | null;
  maximized?: boolean;
  onToggleMaximize?: () => void;
  onTakeOver?: (space: string) => void;
  onClose?: () => void;
}) {
  const [state, setState] = useState<BrowserState | null>(null);
  const [urlDraft, setUrlDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [downloadsOpen, setDownloadsOpen] = useState(false);
  const [tabs, setTabs] = useState<BrowserTab[]>([]);
  const [downloads, setDownloads] = useState<BrowserDownload[]>([]);
  // Don't clobber the address bar with the polled active-tab url while typing.
  const urlFocused = useRef(false);

  const refresh = useCallback(async () => {
    const s = await api.getBrowserState();
    setState(s);
    const active = s.spaces.find((x) => x.name === s.active_space) || s.spaces[0];
    if (active?.url && !urlFocused.current) setUrlDraft(active.url);
  }, []);

  useEffect(() => {
    void refresh();
    const t = window.setInterval(() => void refresh(), 2500);
    return () => window.clearInterval(t);
  }, [refresh]);

  useEffect(() => {
    if (handoff?.url) setUrlDraft(handoff.url);
  }, [handoff?.url]);

  const spaces = state?.spaces ?? [];
  const activeName = handoff?.space || state?.active_space || spaces[0]?.name || null;
  const active = spaces.find((s) => s.name === activeName) || null;
  const owner = active?.owner || "agent";

  useEffect(() => {
    if (!activeName) {
      setTabs([]);
      return;
    }
    let cancelled = false;
    const load = () => {
      void api.listBrowserTabs(activeName).then((r) => {
        if (!cancelled && r?.ok) setTabs(r.tabs || []);
      });
    };
    load();
    const t = window.setInterval(load, 2500);
    return () => {
      cancelled = true;
      window.clearInterval(t);
    };
  }, [activeName, state?.url]);

  useEffect(() => {
    if (!downloadsOpen) return;
    const load = () => {
      void api.listBrowserDownloads(activeName || undefined).then((r) => {
        if (r?.ok) setDownloads(r.downloads || []);
      });
    };
    load();
    const t = window.setInterval(load, 2500);
    return () => window.clearInterval(t);
  }, [downloadsOpen, activeName]);

  const go = async () => {
    if (!activeName || !urlDraft.trim()) return;
    setBusy(true);
    try {
      await api.navigateBrowserSpace(activeName, urlDraft.trim(), { human: true });
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const takeOver = () => {
    if (!activeName) return;
    onTakeOver?.(activeName);
  };

  const snap = async () => {
    if (!activeName) return;
    setBusy(true);
    try {
      await api.screenshotBrowserSpace(activeName, { session_id: sessionId || undefined });
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const refreshTabs = async () => {
    if (!activeName) return;
    const r = await api.listBrowserTabs(activeName);
    if (r?.ok) setTabs(r.tabs || []);
  };

  const addTab = async () => {
    if (!activeName) return;
    setBusy(true);
    try {
      await api.openBrowserTab(activeName, HUMAN_HOME);
      await refresh();
      await refreshTabs();
    } finally {
      setBusy(false);
    }
  };

  const pickTab = async (tabId: string) => {
    if (!activeName) return;
    setBusy(true);
    try {
      await api.activateBrowserTab(activeName, tabId);
      await refresh();
      await refreshTabs();
    } finally {
      setBusy(false);
    }
  };

  const dropTab = async (tabId: string) => {
    if (!activeName) return;
    setBusy(true);
    try {
      await api.closeBrowserTab(activeName, tabId);
      await refresh();
      await refreshTabs();
    } finally {
      setBusy(false);
    }
  };

  return (
    <aside className="flex h-full min-w-0 flex-1 flex-col border-l border-line bg-card">
      <div className="flex items-center gap-1.5 border-b border-line bg-card px-2 py-1.5">
        <Globe className="h-3.5 w-3.5 shrink-0 text-muted" />
        {/* Global tab strip (multi-Space removed): one shared browser, many tabs. */}
        <div className="flex min-w-0 max-w-[45%] shrink-0 items-center gap-1 overflow-x-auto">
          {tabs.map((t) => {
            const label = (t.title || t.url || "新标签").replace(/^https?:\/\//, "");
            return (
              <div
                key={t.id}
                className={`flex shrink-0 items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] ${
                  t.active ? "border-violet/40 bg-violet/10 text-txt" : "border-transparent text-muted hover:text-txt"
                }`}
              >
                <button
                  onClick={() => void pickTab(t.id)}
                  className="max-w-[120px] truncate"
                  title={t.url || label}
                >
                  {label}
                </button>
                {tabs.length > 1 && (
                  <button
                    onClick={() => void dropTab(t.id)}
                    className="rounded p-0.5 text-faint hover:bg-card2 hover:text-red"
                    title="关闭标签"
                  >
                    <X className="h-3 w-3" />
                  </button>
                )}
              </div>
            );
          })}
          <button
            onClick={() => void addTab()}
            title="新建标签"
            className="shrink-0 rounded p-1 text-muted hover:bg-card2 hover:text-txt"
          >
            <Plus className="h-3.5 w-3.5" />
          </button>
        </div>
        <div className="h-4 w-px shrink-0 bg-line" />
        <form
          className="flex min-w-0 flex-1 items-center gap-1"
          onSubmit={(e) => {
            e.preventDefault();
            void go();
          }}
        >
          <input
            value={urlDraft}
            onChange={(e) => setUrlDraft(e.target.value)}
            onFocus={() => (urlFocused.current = true)}
            onBlur={() => (urlFocused.current = false)}
            placeholder="https://…"
            className="min-w-0 flex-1 rounded-md border border-line2 bg-base/60 px-2 py-1 font-mono text-[11px] text-txt outline-none focus:border-violet"
          />
          <button
            type="submit"
            disabled={!activeName}
            className="shrink-0 rounded p-1 text-muted hover:bg-card2 hover:text-txt disabled:opacity-40"
            title="前往"
          >
            <RotateCw className="h-3.5 w-3.5" />
          </button>
        </form>
        <button
          onClick={() => void snap()}
          disabled={!activeName}
          title="截图到 Artifacts"
          className="shrink-0 rounded p-1 text-muted hover:bg-card2 hover:text-txt disabled:opacity-40"
        >
          <Camera className="h-3.5 w-3.5" />
        </button>
        {(
          <button
            onClick={() => setDownloadsOpen((v) => !v)}
            title="下载"
            className={`shrink-0 rounded p-1 hover:bg-card2 ${downloadsOpen ? "text-violet" : "text-muted hover:text-txt"}`}
          >
            <Download className="h-3.5 w-3.5" />
          </button>
        )}
        <button
          onClick={() => setWizardOpen((v) => !v)}
          title="导入 Chrome 登录"
          className={`shrink-0 rounded p-1 hover:bg-card2 ${wizardOpen ? "text-violet" : "text-muted hover:text-txt"}`}
        >
          <Settings2 className="h-3.5 w-3.5" />
        </button>
        {owner === "agentDelegatedToUser" && (
          <button
            onClick={takeOver}
            className="shrink-0 rounded-md bg-violet px-2 py-1 text-[11px] font-medium text-white hover:opacity-90"
          >
            交还
          </button>
        )}
        {owner === "agent" && activeName && (
          <button
            onClick={() => void api.handoffBrowserSpace(activeName, "user takeover").then(() => refresh())}
            className="shrink-0 rounded-md border border-yellow/40 px-2 py-1 text-[11px] text-yellow hover:bg-yellow/10"
          >
            接管
          </button>
        )}
        {onClose && (
          <button onClick={onClose} title="收起浏览器（⌘.）" className="shrink-0 rounded p-1 text-muted hover:bg-card2 hover:text-txt">
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      {handoff?.reason && (
        <div className="shrink-0 border-b border-yellow/40 bg-yellow/10 px-3 py-1 text-[11px] text-yellow">
          需要你：{handoff.reason} — 在伴随浏览器窗口里操作，点完按「交还」。
          {handoff.url ? <span className="ml-2 font-mono text-[10px] text-faint">{handoff.url}</span> : null}
        </div>
      )}

      {wizardOpen && <ImportWizard onDone={() => setWizardOpen(false)} />}
      {downloadsOpen && (
        <div className="max-h-28 shrink-0 overflow-y-auto border-b border-line bg-base/40 px-3 py-1.5 text-[11px]">
          <div className="mb-1 font-medium text-txt">下载</div>
          {downloads.length === 0 ? (
            <div className="text-faint">还没有下载。文件会进 ~/.ginno/browser/downloads，完成的进 Artifacts。</div>
          ) : (
            downloads.map((d) => (
              <div key={d.id} className="flex items-center justify-between gap-2 py-0.5 text-muted">
                <span className="min-w-0 truncate font-mono">{d.filename || d.url || d.id}</span>
                <span className="shrink-0 text-faint">{d.state || ""}</span>
              </div>
            ))
          )}
        </div>
      )}

      {/* Companion-window model: the page lives in a separate companion OS
          window (one visible at a time). This pane is controls-only: tab strip,
          address bar, and handoff actions above. */}
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 px-6 text-center">
        <Globe className="h-8 w-8 text-faint" />
        <div className="text-sm text-txt">伴随浏览器</div>
        <div className="max-w-sm text-[11px] text-faint">
          页面在独立伴随窗口中打开（一次一个）。用上方 tab 条/地址栏管理；手直接点伴随窗口操作。
        </div>
        {state?.engine === "fake" && (
          <button
            disabled={busy}
            onClick={() => {
              setBusy(true);
              void api
                .resetBrowser()
                .then(() => refresh())
                .finally(() => setBusy(false));
            }}
            className="rounded-md bg-violet px-3 py-1.5 text-[11px] font-medium text-white hover:opacity-90 disabled:opacity-40"
          >
            {busy ? "重试中…" : "真浏览器没起来：清掉旧进程并重试"}
          </button>
        )}
      </div>
    </aside>
  );
}

function ImportWizard({ onDone }: { onDone: () => void }) {
  const [status, setStatus] = useState<ChromeImportStatus | null>(null);
  const [profile, setProfile] = useState("Default");
  const [ext, setExt] = useState(false);
  const [force, setForce] = useState(false);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const s = await api.getChromeImportStatus();
    setStatus(s);
    if (s.profiles?.[0]?.id) setProfile(s.profiles[0].id);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async () => {
    setBusy(true);
    setMsg("");
    try {
      const r = await api.importChromeProfile({
        profile,
        include_extensions: ext,
        force,
      });
      if (r.ok) {
        setMsg(`已导入 ${r.copied?.length || 0} 项${r.cookies_ok ? " · Cookies 可读" : ""}`);
        await load();
      } else {
        setMsg(r.error || "导入失败");
      }
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="border-b border-line bg-base/40 px-3 py-2 text-[11px]">
      <div className="mb-1 flex items-center justify-between">
        <div className="font-medium text-txt">从系统 Chrome 导入登录态</div>
        <button onClick={onDone} className="text-faint hover:text-txt">
          收起
        </button>
      </div>
      <p className="mb-2 text-faint">
        复制 Cookies / Login Data 到 <span className="font-mono">~/.ginno/browser/profile</span>
        。内嵌浏览器自己跑无头 Chrome，画面画在右侧分栏，不会弹出系统窗口。Playwright MCP 是另一套匿名无头，跟这套登录态无关。
      </p>
      {status?.chrome_running && (
        <div className="mb-2 rounded border border-yellow/40 bg-yellow/10 px-2 py-1 text-yellow">
          Chrome 正在运行 — 先完全退出，再导入。强制覆盖会在锁文件仍在时复制，可能不完整。
        </div>
      )}
      {status?.imported && (
        <div className="mb-2 text-muted">
          已导入自 <span className="font-mono">{status.imported_from || "—"}</span>
        </div>
      )}
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <select
          className="rounded border border-line2 bg-card px-1.5 py-0.5"
          value={profile}
          onChange={(e) => setProfile(e.target.value)}
        >
          {(status?.profiles || [{ id: "Default", label: "Default" }]).map((p) => (
            <option key={p.id} value={p.id}>
              {p.label || p.id}
              {p.has_cookies ? "" : "（无 Cookies）"}
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
          onClick={() => void run()}
          className="rounded bg-violet px-2 py-0.5 text-white disabled:opacity-40"
        >
          {busy ? "导入中…" : "导入"}
        </button>
      </div>
      {msg && <div className="text-muted">{msg}</div>}
    </div>
  );
}
