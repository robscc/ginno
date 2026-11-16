"use client";

// SessionContextMenu — 会话行右键上下文菜单（用户需求 2026-11-16）：动作
// 与行尾扇形菜单（RowFanMenu）完全同源（useRowActions），以 icon+label
// 竖列呈现（扇叶 icon-only 不可读，右键菜单是桌面惯例的完整清单）。
// portal 到 body + fixed 定位在鼠标点，Esc/点外/滚动关闭。

import { useRowActions } from "./rowActions";
import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

export interface ContextMenuAnchor {
  x: number;
  y: number;
}

export function SessionContextMenu({
  anchor,
  onClose,
  pinned,
  onPin,
  onRename,
  onDelete,
  worker,
  onWorker,
  forkSid,
  onFork,
  onTags,
  unread,
  onUnread,
}: {
  anchor: ContextMenuAnchor;
  onClose: () => void;
  pinned: boolean;
  onPin: (pinned: boolean) => void;
  onRename: () => void;
  onDelete: () => void;
  worker?: boolean;
  onWorker?: (worker: boolean) => void;
  forkSid?: string;
  onFork?: () => void;
  /** tags 面板打开回调（行组件持有面板状态——右键菜单只转发）。 */
  onTags?: () => void;
  unread?: boolean;
  onUnread?: (unread: boolean) => void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);

  const items = useRowActions({
    pinned,
    onPin,
    onRename,
    onDelete,
    worker,
    onWorker,
    forkSid,
    onFork,
    onTags,
    unread,
    onUnread,
  });

  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (!hostRef.current?.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    // capture 滚动即关（fixed 菜单不随滚动走，留着会悬空错位）。
    window.addEventListener("scroll", onClose, true);
    window.addEventListener("resize", onClose);
    window.addEventListener("pointerdown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("scroll", onClose, true);
      window.removeEventListener("resize", onClose);
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  // 视口边缘防溢出：先按锚点摆，右/下溢出时向内收（菜单宽 ~200px、高按
  // 项数估 30px/项）。
  const MENU_W = 208;
  const MENU_H = items.length * 30 + 10;
  const left = Math.min(anchor.x, window.innerWidth - MENU_W - 8);
  const top = Math.min(anchor.y, window.innerHeight - MENU_H - 8);

  return createPortal(
    <div
      ref={hostRef}
      role="menu"
      className="fixed z-50 min-w-52 overflow-hidden rounded-lg border border-line2 bg-card py-1 shadow-xl"
      style={{ left: Math.max(8, left), top: Math.max(8, top) }}
      onContextMenu={(e) => e.preventDefault()}
    >
      {items.map((it) => (
        <button
          key={it.key}
          role="menuitem"
          title={it.hint}
          onClick={(e) => {
            e.stopPropagation();
            onClose();
            it.run();
          }}
          className={`flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-xs ${
            it.danger ? "text-red hover:bg-red/10" : "text-txt hover:bg-card2"
          }`}
        >
          <span className="flex h-4 w-4 items-center justify-center opacity-80">
            {it.icon}
          </span>
          <span className="flex-1 truncate">{it.label}</span>
        </button>
      ))}
    </div>,
    document.body,
  );
}
