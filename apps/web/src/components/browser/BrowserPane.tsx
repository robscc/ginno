"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Camera, Download, Globe, Maximize2, Minimize2, Plus, RotateCw, Settings2, X } from "lucide-react";
import * as api from "@/lib/runtime";
import { emitBrowserTile } from "@/lib/desktop";
import type { BrowserDownload, BrowserOwner, BrowserSpace, BrowserState, BrowserTab, ChromeImportStatus } from "@/lib/types";

const OWNER_TONE: Record<BrowserOwner, string> = {
  agent: "border-violet/40 bg-violet/10 text-violet",
  agentDelegatedToUser: "border-yellow/50 bg-yellow/15 text-yellow",
  user: "border-line2 bg-card2 text-muted",
};

// Landing page for tabs/spaces a human opens by hand. about:blank reads
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
  const [frameTs, setFrameTs] = useState(0);
  const tileRef = useRef<HTMLDivElement>(null);
  const lastViewport = useRef<string>("");
  const viewportRef = useRef({ width: 0, height: 0 });
  const lastMove = useRef(0);

  const refresh = useCallback(async () => {
    const s = await api.getBrowserState();
    setState(s);
    const active = s.spaces.find((x) => x.name === s.active_space) || s.spaces[0];
    if (active?.url) setUrlDraft(active.url);
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
  const owner = (active?.owner || "agent") as BrowserOwner;

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

  const liveCef = state?.engine === "cef";
  const watching = owner === "agent";

  // Open the transparent hole: the native CEF view sits behind the
  // WKWebView, so body / root backgrounds must get out of the way of the
  // tile rect while a CEF page is live.
  useEffect(() => {
    const need = liveCef && !!activeName;
    document.documentElement.classList.toggle("ginno-hole", need);
    return () => {
      document.documentElement.classList.remove("ginno-hole");
    };
  }, [liveCef, activeName]);

  // Paint the live page into this tile (headless Chrome screencast).
  // A live CEF child paints natively through the hole — don't poll JPEGs.
  useEffect(() => {
    if (liveCef) return;
    const tick = window.setInterval(() => setFrameTs(Date.now()), 120);
    return () => window.clearInterval(tick);
  }, [liveCef]);

  // Chrome CSS viewport = this tile. Clicks then land 1:1 on the page.
  useEffect(() => {
    const el = tileRef.current;
    if (!el) return;
    let raf = 0;
    const report = () => {
      const r = el.getBoundingClientRect();
      const width = Math.round(r.width);
      const height = Math.round(r.height);
      const key = `${width}x${height}:${activeName || ""}`;
      if (width < 80 || height < 80) return;
      viewportRef.current = { width, height };
      void emitBrowserTile({
        x: Math.round(r.left),
        y: Math.round(r.top),
        width,
        height,
        space: activeName,
        visible: true,
        // Human input is delivered to the embedded CEF page over CDP
        // (dispatch_input), never via native hit-test forwarding. Keeps the
        // WKWebView DOM fully interactive for the pane chrome.
        passthrough: false,
      });
      if (key === lastViewport.current) return;
      lastViewport.current = key;
      void api.setBrowserViewport({
        width,
        height,
        space: activeName || undefined,
        dpr: 1,
      });
    };
    const onResize = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(report);
    };
    report();
    const ro = new ResizeObserver(onResize);
    ro.observe(el);
    window.addEventListener("resize", onResize);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      window.removeEventListener("resize", onResize);
      void emitBrowserTile({ x: 0, y: 0, width: 0, height: 0, space: activeName, visible: false, passthrough: false });
    };
  }, [activeName, liveCef, owner]);

  const select = async (name: string) => {
    setBusy(true);
    try {
      await api.createBrowserSpace({ name, session_id: sessionId || undefined });
      await refresh();
    } finally {
      setBusy(false);
    }
  };

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

  const newMine = async () => {
    setBusy(true);
    try {
      const r = await api.createBrowserSpace({ name: "我的", owner: "user", session_id: sessionId || undefined });
      // A human's own Space should land on a usable page, not about:blank.
      const url = r?.space?.url || "";
      if (r?.ok && (!url || url === "about:blank")) {
        await api.navigateBrowserSpace("我的", HUMAN_HOME, { human: true });
      }
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

  // The page is always clickable. Agent-owned Spaces flip to delegated on
  // the first real click / key / wheel so the person does not have to hunt
  // for 「接管」 first. (watching / liveCef are declared above the tile effect.)

  const point = (e: { clientX: number; clientY: number }) => {
    const el = tileRef.current;
    if (!el) return { x: 0, y: 0 };
    const r = el.getBoundingClientRect();
    const cssW = viewportRef.current.width || Math.round(r.width);
    const cssH = viewportRef.current.height || Math.round(r.height);
    // object-fill: JPEG is stretched to the tile. Map tile CSS → Chrome CSS
    // viewport (same numbers once set_viewport has landed).
    return {
      x: ((e.clientX - r.left) / Math.max(r.width, 1)) * cssW,
      y: ((e.clientY - r.top) / Math.max(r.height, 1)) * cssH,
    };
  };

  const send = (event: Parameters<typeof api.sendBrowserInput>[1]) => {
    if (!activeName) return;
    void api.sendBrowserInput(activeName, event).then((r) => {
      if (r?.ok && (r as { handoff?: boolean }).handoff) void refresh();
    });
  };

  return (
    <aside
      className={`flex h-full min-w-0 flex-1 flex-col border-l border-line ${
        liveCef && activeName ? "bg-transparent" : "bg-card"
      }`}
    >
      <div className="flex items-center gap-1.5 border-b border-line bg-card px-2 py-1.5">
        <Globe className="h-3.5 w-3.5 shrink-0 text-muted" />
        <div className="flex max-w-[38%] shrink-0 items-center gap-1 overflow-x-auto">
          {spaces.length === 0 && (
            <span className="whitespace-nowrap text-[11px] text-faint">还没有 Space</span>
          )}
          {(liveCef
            ? spaces.filter((s) => s.owner === "user" || s.name === activeName)
            : spaces
          ).map((s) => (
            <SpaceChip key={s.name} space={s} active={s.name === activeName} onClick={() => void select(s.name)} />
          ))}
        </div>
        <button
          onClick={() => void newMine()}
          title="新建我的 Space"
          className="shrink-0 rounded p-1 text-muted hover:bg-card2 hover:text-txt"
        >
          <Plus className="h-3.5 w-3.5" />
        </button>
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
        {!liveCef && (
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
        {onToggleMaximize && (
          <button
            onClick={onToggleMaximize}
            title={maximized ? "还原聊天分栏（⌘⇧.）" : "最大化网页（⌘⇧.）"}
            className="shrink-0 rounded p-1 text-muted hover:bg-card2 hover:text-txt"
          >
            {maximized ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
          </button>
        )}
        {onClose && (
          <button onClick={onClose} title="收起浏览器（⌘.）" className="shrink-0 rounded p-1 text-muted hover:bg-card2 hover:text-txt">
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      {activeName && tabs.length > 0 && (
        <div className={`flex shrink-0 items-center gap-1 overflow-x-auto border-b border-line px-2 py-1 ${liveCef && activeName ? "bg-card" : "bg-base/40"}`}>
          {tabs.map((t) => {
            const label = (t.title || t.url || "新标签").replace(/^https?:\/\//, "");
            return (
              <div
                key={t.id}
                className={`flex max-w-[180px] shrink-0 items-center gap-0.5 rounded-md border px-1.5 py-0.5 text-[11px] ${
                  t.active
                    ? "border-violet/40 bg-violet/10 text-txt"
                    : "border-transparent text-muted hover:bg-card2 hover:text-txt"
                }`}
              >
                <button
                  type="button"
                  title={t.url || label}
                  onClick={() => void pickTab(t.id)}
                  className="min-w-0 truncate"
                >
                  {label}
                </button>
                {tabs.length > 1 && (
                  <button
                    type="button"
                    title="关闭标签"
                    onClick={() => void dropTab(t.id)}
                    className="rounded p-0.5 text-faint hover:text-txt"
                  >
                    <X className="h-2.5 w-2.5" />
                  </button>
                )}
              </div>
            );
          })}
          <button
            type="button"
            disabled={busy || !activeName}
            title="新标签"
            onClick={() => void addTab()}
            className="shrink-0 rounded p-0.5 text-muted hover:bg-card2 hover:text-txt disabled:opacity-40"
          >
            <Plus className="h-3 w-3" />
          </button>
        </div>
      )}

      {handoff?.reason && (
        <div className="shrink-0 border-b border-yellow/40 bg-yellow/10 px-3 py-1 text-[11px] text-yellow">
          需要你：{handoff.reason} — 直接点这块画面操作，点完按「交还」。
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

      <div
        ref={tileRef}
        className={`relative min-h-0 flex-1 overflow-hidden cursor-auto ${
          liveCef && activeName ? "bg-transparent" : "bg-black"
        }`}
        tabIndex={0}
        onPointerMove={(e) => {
          if (watching) return;
          const now = performance.now();
          if (now - lastMove.current < 32) return;
          lastMove.current = now;
          const p = point(e);
          send({ type: "mouseMoved", x: p.x, y: p.y, buttons: e.buttons });
        }}
        onPointerDown={(e) => {
          (e.currentTarget as HTMLDivElement).focus();
          const p = point(e);
          send({
            type: "mousePressed",
            x: p.x,
            y: p.y,
            button: e.button === 2 ? "right" : e.button === 1 ? "middle" : "left",
            clickCount: e.detail || 1,
          });
        }}
        onPointerUp={(e) => {
          const p = point(e);
          send({
            type: "mouseReleased",
            x: p.x,
            y: p.y,
            button: e.button === 2 ? "right" : e.button === 1 ? "middle" : "left",
            clickCount: e.detail || 1,
          });
        }}
        onWheel={(e) => {
          e.preventDefault();
          const p = point(e);
          send({ type: "mouseWheel", x: p.x, y: p.y, deltaX: e.deltaX, deltaY: e.deltaY });
        }}
        onKeyDown={(e) => {
          e.preventDefault();
          const mods = (e.altKey ? 1 : 0) + (e.ctrlKey || e.metaKey ? 2 : 0) + (e.shiftKey ? 8 : 0);
          send({
            type: "keyDown",
            key: e.key,
            text: e.key.length === 1 ? e.key : "",
            modifiers: mods,
            windowsVirtualKeyCode: e.keyCode,
          });
        }}
        onKeyUp={(e) => {
          e.preventDefault();
          send({ type: "keyUp", key: e.key, windowsVirtualKeyCode: e.keyCode });
        }}
        onContextMenu={(e) => e.preventDefault()}
      >
        {state?.engine === "fake" ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
            <Globe className="h-8 w-8 text-faint" />
            <div className="text-sm text-txt">真 Chrome 没起来，现在是占位画面</div>
            <div className="max-w-sm text-[11px] text-faint">
              {state.engine_error ||
                "常见原因：上一只 Ginno 留下的 Chrome 还锁着 ~/.ginno/browser/profile。完全退出旧进程后再点重试。"}
            </div>
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
              {busy ? "重试中…" : "清掉旧进程并重试"}
            </button>
          </div>
        ) : liveCef && activeName ? (
          <div className="h-full w-full" aria-hidden />
        ) : activeName ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={api.browserFrameUrl(activeName, frameTs)}
            alt={active?.title || activeName}
            className="pointer-events-none h-full w-full object-fill select-none"
            draggable={false}
          />
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
            <Globe className="h-8 w-8 text-faint" />
            <div className="text-sm text-txt">内嵌浏览器</div>
            <div className="max-w-sm text-[11px] text-faint">
              点顶栏「浏览器」，或让 Agent 用 /browse。页面画在这里，不会再弹出系统 Chrome。
            </div>
          </div>
        )}
        {watching && activeName && state?.engine !== "fake" && (
          <div className="pointer-events-none absolute inset-x-3 bottom-3 z-10 rounded-md border border-line2 bg-base/80 px-2 py-1 text-[10px] text-muted">
            Agent 正在操作 — 直接点画面即可接管
          </div>
        )}
      </div>
    </aside>
  );
}

function SpaceChip({
  space,
  active,
  onClick,
}: {
  space: BrowserSpace;
  active: boolean;
  onClick: () => void;
}) {
  const tone = OWNER_TONE[space.owner] || OWNER_TONE.agent;
  return (
    <button
      onClick={onClick}
      title={`${space.name} · ${space.owner}${space.url ? ` · ${space.url}` : ""}`}
      className={`shrink-0 rounded-md border px-2 py-0.5 text-[11px] ${
        active ? tone : "border-line2 bg-transparent text-muted hover:text-txt"
      }`}
    >
      {space.owner === "agentDelegatedToUser" ? "● " : ""}
      {space.name}
    </button>
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
