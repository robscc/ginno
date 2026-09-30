"use client";

import { useEffect, useRef, useState } from "react";
import { Eye, EyeOff, GripVertical } from "lucide-react";
import { useGinno, type RightTab } from "@/lib/store";
import { RIGHT_TAB_BY_ID } from "@/lib/rightTabs";
import { cn } from "@/lib/utils";
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
      onMsg("至少要保留一个标签");
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
      <label className="field-label">右栏标签</label>
      <p className="text-xs text-faint">
        拖动左侧手柄调整顺序，点击眼睛图标显示 / 隐藏。隐藏的标签会保留位置，随时可以重新显示。
      </p>
      <div role="list" aria-label="右栏标签顺序" className="relative mt-2 flex flex-col gap-1">
        {order.map((id, i) => {
          const meta = RIGHT_TAB_BY_ID[id];
          const Icon = meta.icon;
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
                aria-label={`${meta.label} 标签（第 ${i + 1} / ${order.length} 个，${
                  isHidden ? "已隐藏" : "显示中"
                }）`}
                onKeyDown={(e) => onRowKeyDown(e, id, i)}
                className={cn(
                  "flex items-center gap-2 rounded-lg border bg-base/40 px-2 py-1.5 outline-none transition-colors focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-violet/60",
                  dragId === id ? "border-violet bg-base/60" : "border-line hover:border-line2",
                )}
              >
                <span
                  role="button"
                  tabIndex={-1}
                  aria-label={`拖动排序：${meta.label}`}
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
                  {meta.label}
                </span>
                <button
                  type="button"
                  disabled={locked}
                  title={locked ? "至少要保留一个标签" : undefined}
                  aria-label={isHidden ? `显示 ${meta.label} 标签` : `隐藏 ${meta.label} 标签`}
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
            onMsg("已恢复默认标签顺序");
          }}
          className="rounded-lg border border-line px-3 py-1.5 text-xs text-muted transition-colors hover:text-txt"
        >
          恢复默认
        </button>
        <span className="text-xs text-faint">
          {g.visibleRightTabs.length} / {order.length} 个标签显示中
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
      .then((s) => setBypass((s as Record<string, unknown>).bypass_permissions !== false))
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
          <label className="field-label">工作目录</label>
          <div className="field bg-base/40 text-muted">
            ~/workspace/&lt;project&gt; （Agent 元数据在 ~/.ginno/projects/）
          </div>
        </div>
        <RightTabsSection onMsg={setMsg} />
        {msg && <div className="text-xs text-muted">{msg}</div>}
      </div>
    </div>
  );
}
