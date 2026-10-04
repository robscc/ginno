"use client";

import { useEffect, useRef, useState } from "react";
import { Eye, EyeOff, GripVertical } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno, type RightTab } from "@/lib/store";
import { RIGHT_TAB_BY_ID } from "@/lib/rightTabs";
import { cn } from "@/lib/utils";
import * as api from "@/lib/runtime";
import { useLocaleCtx, setLocale } from "@/i18n/provider";
import type { LanguageSetting } from "@/i18n/config";

export function applyTheme(t: string) {
  if (typeof document === "undefined") return;
  document.documentElement.classList.toggle("light", t === "light");
  try {
    localStorage.setItem("ginno-theme", t);
  } catch {
    /* ignore */
  }
}

/**
 * Right-panel tab rows: pointer-event drag to reorder (HTML5 drag-and-drop is a
 * dead end here — `dragDropEnabled: true` in tauri.conf.json makes the native
 * webview swallow dragstart/dragover, and turning it off would break the app's
 * file-drop feature) plus an eye to toggle visibility.
 *
 * One row per tab in the FULL order (`rightTabOrder`), so a hidden tab keeps its
 * slot and can be re-shown in place rather than jumping to the end.
 */
function RightTabsSection({ onMsg }: { onMsg: (m: string) => void }) {
  const g = useGinno();
  // right 域文案：tab 标签（right.tabs.* 注册表 key 引用）+ 排序列表自身的
  // 标题/提示/aria（同域追加，Settings 页与右栏 dock 共用一套 tab 名）。
  const tr = useTranslations("right");
  const [dragId, setDragId] = useState<RightTab | null>(null);
  // Insertion slot 0..N in the *current* order while dragging; null = not dragging.
  const [dropSlot, setDropSlot] = useState<number | null>(null);
  // Measured row elements, keyed by tab id — the pointer is tracked against their
  // midpoints instead of doing per-pixel index math.
  const rowsRef = useRef<Partial<Record<RightTab, HTMLDivElement | null>>>({});
  const dragging = useRef<RightTab | null>(null);
  const releaseBodyLock = useRef<() => void>(() => {});

  const order = g.rightTabOrder;
  const hidden = new Set(g.rightTabsHidden);
  // The store refuses to hide the last visible tab (an empty strip would leave
  // the panel with nothing to click), so reflect that here instead of offering a
  // silent no-op.
  const canHide = g.visibleRightTabs.length > 1;

  // The section can unmount mid-drag (user switches settings pages): still release
  // the body-level select/cursor lock.
  useEffect(() => {
    return () => {
      if (dragging.current) releaseBodyLock.current();
    };
  }, []);

  /** Insertion slot for a pointer y: how many rows the pointer is past (0..N). */
  function slotAt(y: number): number {
    let slot = 0;
    for (const id of order) {
      const el = rowsRef.current[id];
      if (!el) continue;
      const r = el.getBoundingClientRect();
      if (y > r.top + r.height / 2) slot++;
    }
    return slot;
  }

  /** Move `id` to insertion slot `slot`, accounting for its own removal. */
  function commitSlot(id: RightTab, slot: number) {
    const from = order.indexOf(id);
    if (from < 0) return;
    const to = slot > from ? slot - 1 : slot;
    if (to === from) return;
    const next = order.slice();
    next.splice(from, 1);
    next.splice(to, 0, id);
    g.setRightTabOrder(next);
  }

  function toggle(id: RightTab) {
    const isHidden = hidden.has(id);
    if (!isHidden && !canHide) {
      onMsg(tr("tabs.lastVisible"));
      return;
    }
    g.setRightTabHidden(id, !isHidden);
  }

  function startDrag(e: React.PointerEvent<HTMLElement>, id: RightTab) {
    e.stopPropagation();
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    dragging.current = id;
    setDragId(id);
    setDropSlot(order.indexOf(id));
    // Lock the page against text selection and stray cursors while dragging;
    // restored when the drag ends or the section unmounts.
    document.body.classList.add("select-none");
    const prevCursor = document.body.style.cursor;
    document.body.style.cursor = "grabbing";
    releaseBodyLock.current = () => {
      document.body.classList.remove("select-none");
      document.body.style.cursor = prevCursor;
    };
  }

  function moveDrag(e: React.PointerEvent<HTMLElement>) {
    if (!dragging.current) return;
    setDropSlot(slotAt(e.clientY));
  }

  function endDrag(e: React.PointerEvent<HTMLElement>, cancelled: boolean) {
    const id = dragging.current;
    if (!id) return;
    dragging.current = null;
    setDragId(null);
    setDropSlot(null);
    releaseBodyLock.current();
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      // capture already released (e.g. pointercancel)
    }
    // A cancelled gesture is discarded; a real drop commits the pending slot.
    if (!cancelled) commitSlot(id, slotAt(e.clientY));
  }

  /** Arrow keys move the focused row one slot; Space/Enter toggles visibility. */
  function onRowKeyDown(e: React.KeyboardEvent<HTMLDivElement>, id: RightTab, index: number) {
    // The eye button owns its own keys — don't toggle twice on Space/Enter.
    if (e.target !== e.currentTarget) return;
    if (e.key === "ArrowUp" && index > 0) {
      e.preventDefault();
      const next = order.slice();
      next.splice(index, 1);
      next.splice(index - 1, 0, id);
      g.setRightTabOrder(next);
    } else if (e.key === "ArrowDown" && index < order.length - 1) {
      e.preventDefault();
      const next = order.slice();
      next.splice(index, 1);
      next.splice(index + 1, 0, id);
      g.setRightTabOrder(next);
    } else if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      toggle(id);
    }
  }

  return (
    <div>
      <label className="field-label">{tr("tabs.settingsTitle")}</label>
      <p className="text-xs text-faint">{tr("tabs.settingsHint")}</p>
      <div
        role="list"
        aria-label={tr("tabs.settingsListLabel")}
        className="relative mt-2 flex flex-col gap-1"
      >
        {order.map((id, i) => {
          const meta = RIGHT_TAB_BY_ID[id];
          const Icon = meta.icon;
          const label = tr(meta.labelKey);
          const isHidden = hidden.has(id);
          const locked = !isHidden && !canHide;
          return (
            <div key={id} className="relative">
              {dropSlot === i && (
                <span
                  aria-hidden
                  className="pointer-events-none absolute -top-[3px] inset-x-0 h-0.5 rounded-full bg-violet"
                />
              )}
              <div
                ref={(el) => {
                  rowsRef.current[id] = el;
                }}
                role="listitem"
                tabIndex={0}
                aria-label={tr("tabs.rowLabel", {
                  tab: label,
                  index: i + 1,
                  total: order.length,
                  state: isHidden ? tr("tabs.stateHidden") : tr("tabs.stateVisible"),
                })}
                onKeyDown={(e) => onRowKeyDown(e, id, i)}
                className={cn(
                  "flex items-center gap-2 rounded-lg border bg-base/40 px-2 py-1.5 outline-none transition-colors focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-violet/60",
                  dragId === id ? "border-violet bg-base/60" : "border-line hover:border-line2",
                )}
              >
                <span
                  role="button"
                  tabIndex={-1}
                  aria-label={tr("tabs.dragHandle", { tab: label })}
                  onPointerDown={(e) => startDrag(e, id)}
                  onPointerMove={moveDrag}
                  onPointerUp={(e) => endDrag(e, false)}
                  onPointerCancel={(e) => endDrag(e, true)}
                  className={cn(
                    "flex shrink-0 cursor-grab touch-none items-center rounded p-0.5 text-faint hover:text-txt",
                    dragId === id && "cursor-grabbing text-txt",
                  )}
                >
                  <GripVertical size={14} aria-hidden />
                </span>
                <Icon size={14} aria-hidden className={isHidden ? "text-faint" : "text-muted"} />
                <span className={cn("flex-1 truncate text-sm", isHidden ? "text-faint" : "text-txt")}>
                  {label}
                </span>
                <button
                  type="button"
                  disabled={locked}
                  title={locked ? tr("tabs.lastVisible") : undefined}
                  aria-label={
                    isHidden ? tr("tabs.showTab", { tab: label }) : tr("tabs.hideTab", { tab: label })
                  }
                  onClick={() => toggle(id)}
                  className={cn(
                    "shrink-0 rounded p-1 transition-colors",
                    locked ? "cursor-not-allowed text-faint" : "text-muted hover:text-txt",
                  )}
                >
                  {isHidden ? <EyeOff size={14} aria-hidden /> : <Eye size={14} aria-hidden />}
                </button>
              </div>
            </div>
          );
        })}
        {dropSlot === order.length && (
          <span
            aria-hidden
            className="pointer-events-none absolute -bottom-[3px] inset-x-0 h-0.5 rounded-full bg-violet"
          />
        )}
      </div>
      <div className="mt-2 flex items-center gap-3">
        <button
          type="button"
          onClick={() => {
            g.resetRightTabs();
            onMsg(tr("tabs.resetToast"));
          }}
          className="rounded-lg border border-line px-3 py-1.5 text-xs text-muted transition-colors hover:text-txt"
        >
          {tr("tabs.reset")}
        </button>
        <span className="text-xs text-faint">
          {tr("tabs.visibleCount", { visible: g.visibleRightTabs.length, total: order.length })}
        </span>
      </div>
    </div>
  );
}

export function GeneralSettings() {
  const g = useGinno();
  const [theme, setTheme] = useState<string>("dark");
  const [msg, setMsg] = useState("");
  const [bypass, setBypass] = useState(true);
  // subagent 并发上限（P3 共享契约 3）：settings 键 subagent.max_concurrent，
  // 默认 5、范围 1-16；runtime 调度器动态读取，保存后即生效。
  const [subMax, setSubMax] = useState("5");
  // 界面语言：settings.language（auto/en/zh-CN，i18n-design.md §2）。
  // 值由根部的 I18nProvider 加载/校正，这里只读 context + setLocale 写回。
  const { languageSetting } = useLocaleCtx();
  // settings 域 catalog（messages/{en,zh-CN}/settings.json）。注意本组件内
  // 既有局部变量/形参 `t`（applyTheme/map 回调），翻译函数避开命名用 tSettings。
  const tSettings = useTranslations("settings");

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
        const sub = (s as Record<string, unknown>).subagent as
          | { max_concurrent?: unknown }
          | undefined;
        if (typeof sub?.max_concurrent === "number") setSubMax(String(sub.max_concurrent));
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
    setMsg(tSettings("general.providerToast", { name: p }));
  }
  async function toggleBypass(v: boolean) {
    try {
      const s = (await api.getSettings()) as Record<string, unknown>;
      s.bypass_permissions = v;
      await api.putSettings(s);
      setBypass(v);
      setMsg(v ? tSettings("general.privilegedOn") : tSettings("general.privilegedOff"));
    } catch {
      setMsg(tSettings("general.saveFailed"));
    }
  }
  // 界面语言：经 i18n setLocale 写 settings.language（get→改→put，同一 API
  // 通道）+ localStorage 镜像 + context 更新，UI 即时切换、不刷新页面。
  async function saveLanguage(v: LanguageSetting) {
    try {
      await setLocale(v);
      setMsg(tSettings("language.saved"));
    } catch {
      setMsg(tSettings("language.saveFailed"));
    }
  }
  // 保存 subagent 并发上限：走既有 get→改→put 链路（同 toggleBypass），键为
  // 嵌套的 settings.subagent.max_concurrent，其余 subagent 字段原样保留。
  async function saveSubagentMax(raw: string) {
    const n = Math.round(Number(raw));
    const clamped = Number.isFinite(n) ? Math.min(16, Math.max(1, n)) : 5;
    setSubMax(String(clamped));
    if (Number.isFinite(n) && n !== clamped) setMsg(tSettings("general.subClamped", { n: clamped }));
    try {
      const s = (await api.getSettings()) as Record<string, unknown>;
      const sub = (s.subagent as Record<string, unknown> | undefined) ?? {};
      sub.max_concurrent = clamped;
      s.subagent = sub;
      await api.putSettings(s);
      setMsg(tSettings("general.subSaved", { n: clamped }));
    } catch {
      setMsg(tSettings("general.saveFailed"));
    }
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">{tSettings("general.title")}</h2>
      <div className="mt-4 max-w-md space-y-4">
        <div>
          <label className="field-label">{tSettings("general.providerLabel")}</label>
          <select className="field" value={g.defaultProvider} onChange={(e) => setDefault(e.target.value)}>
            {Object.keys(g.providers).map((p) => (
              <option key={p} value={p} disabled={!g.providers[p].enabled}>
                {p}
                {g.providers[p].enabled ? "" : tSettings("general.providerDisabled")}
              </option>
            ))}
          </select>
          <p className="mt-1 text-xs text-faint">{tSettings("general.providerHelp")}</p>
        </div>
        <div>
          <label className="field-label">{tSettings("general.themeLabel")}</label>
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
          <label className="field-label">{tSettings("language.label")}</label>
          <select
            className="field"
            value={languageSetting}
            onChange={(e) => void saveLanguage(e.target.value as LanguageSetting)}
            aria-label={tSettings("language.label")}
          >
            <option value="auto">{tSettings("language.auto")}</option>
            <option value="en">{tSettings("language.en")}</option>
            <option value="zh-CN">{tSettings("language.zh-CN")}</option>
          </select>
          <p className="mt-1 text-xs text-faint">{tSettings("language.help")}</p>
        </div>
        <div>
          <label className="flex items-center gap-2 text-sm text-txt">
            <input type="checkbox" checked={bypass} onChange={(e) => toggleBypass(e.target.checked)} />
            {tSettings("general.privileged")}
          </label>
          <p className="mt-1 text-xs text-faint">{tSettings("general.privilegedHelp")}</p>
        </div>
        <div>
          <label className="field-label">{tSettings("general.subLabel")}</label>
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={1}
              max={16}
              value={subMax}
              onChange={(e) => setSubMax(e.target.value)}
              onBlur={(e) => void saveSubagentMax(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void saveSubagentMax((e.target as HTMLInputElement).value);
                }
              }}
              className="field w-24"
              aria-label={tSettings("general.subAria")}
            />
            <span className="text-xs text-faint">{tSettings("general.subHint")}</span>
          </div>
          <p className="mt-1 text-xs text-faint">{tSettings("general.subHelp")}</p>
        </div>
        <div>
          <label className="field-label">{tSettings("general.workingDirLabel")}</label>
          <div className="field bg-base/40 text-muted">{tSettings("general.workingDirValue")}</div>
        </div>
        <RightTabsSection onMsg={setMsg} />
        {msg && <div className="text-xs text-muted">{msg}</div>}
      </div>
    </div>
  );
}
