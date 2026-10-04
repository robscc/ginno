"use client";

/**
 * Row context menu for the code-panel file tree — docs/code-panel-design.md
 * §3.4 ("文件操作") and brief §3.4.
 *
 * Two pieces live here:
 *   - `ContextMenu`, a generic, keyboard-navigable menu primitive (focus on
 *     open, ↑↓ to move, Enter/Space to pick, Esc to close). Keyboard access is
 *     a hard requirement — a pointer-only control is a real defect in a desktop
 *     app (brief §3.4 硬约束 2).
 *   - `buildFileMenuItems`, the file-tree action set, so `FileTree` only maps
 *     ids to callbacks and stays free of menu layout.
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Copy,
  ExternalLink,
  FilePlus2,
  FolderOpen,
  FolderPlus,
  Pencil,
  Trash2,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { cn } from "@/lib/utils";

export interface ContextMenuItem {
  id: string;
  label: string;
  icon?: ReactNode;
  /** Destructive action → red label. */
  danger?: boolean;
  disabled?: boolean;
  /** Chinese explanation shown (as a tooltip) when `disabled`. */
  disabledHint?: string;
  /** Right-aligned shortcut hint (e.g. "F2"). */
  shortcut?: string;
  /** Draw a separator above this item. */
  separatorBefore?: boolean;
}

export interface ContextMenuProps {
  /** Viewport coordinates (clientX/clientY) to anchor the menu at. */
  x: number;
  y: number;
  items: ContextMenuItem[];
  onSelect: (id: string) => void;
  onClose: () => void;
  /** Optional header naming the target (name or path). */
  title?: string;
}

/** Generic menu: a fixed, viewport-clamped popup with a full-screen backdrop
 *  that closes on any outside click. */
export function ContextMenu({ x, y, items, onSelect, onClose, title }: ContextMenuProps) {
  const t = useTranslations("code.menu");
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ x, y });
  const enabledIdx = useMemo(
    () => items.map((it, i) => (it.disabled ? -1 : i)).filter((i) => i >= 0),
    [items],
  );
  const [active, setActive] = useState(() => enabledIdx[0] ?? -1);

  // Focus the menu so ↑↓/Enter work without a mouse (brief §3.4 硬约束 2).
  useEffect(() => {
    ref.current?.focus();
  }, []);

  // Keep the popup inside the viewport (a menu opened near the right/bottom edge
  // would otherwise be unreachable).
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const nx = Math.max(8, Math.min(x, window.innerWidth - r.width - 8));
    const ny = Math.max(8, Math.min(y, window.innerHeight - r.height - 8));
    setPos({ x: nx, y: ny });
  }, [x, y, items, title]);

  const move = (dir: 1 | -1) => {
    if (!enabledIdx.length) return;
    const cur = enabledIdx.indexOf(active);
    const next = cur < 0 ? enabledIdx[0] : enabledIdx[Math.max(0, Math.min(enabledIdx.length - 1, cur + dir))];
    setActive(next);
  };

  const pick = (i: number) => {
    const it = items[i];
    if (!it || it.disabled) return;
    onSelect(it.id);
    onClose();
  };

  return (
    <>
      <div className="fixed inset-0 z-40" aria-hidden onClick={onClose} onContextMenu={(e) => e.preventDefault()} />
      <div
        ref={ref}
        role="menu"
        tabIndex={-1}
        aria-label={title ? t("actionsFor", { name: title }) : t("actions")}
        style={{ left: pos.x, top: pos.y }}
        className="fixed z-50 min-w-[176px] overflow-hidden rounded-lg border border-line bg-card py-1 text-xs shadow-2xl outline-none"
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            move(1);
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            move(-1);
          } else if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            pick(active);
          } else if (e.key === "Escape") {
            e.preventDefault();
            e.stopPropagation();
            onClose();
          } else if (e.key === "Tab") {
            // Trapping Tab inside a transient popup is worse than closing it.
            e.preventDefault();
            onClose();
          }
        }}
      >
        {title ? (
          <div className="truncate border-b border-line px-2.5 pb-1 pt-0.5 text-[10px] text-faint" title={title}>
            {title}
          </div>
        ) : null}
        {items.map((it, i) => (
          <div key={it.id}>
            {it.separatorBefore ? <div className="my-1 h-px bg-line" /> : null}
            <button
              type="button"
              role="menuitem"
              aria-disabled={it.disabled || undefined}
              disabled={it.disabled}
              title={it.disabled ? it.disabledHint : undefined}
              onMouseEnter={() => !it.disabled && setActive(i)}
              onClick={() => pick(i)}
              className={cn(
                "flex w-full items-center gap-2 px-2.5 py-1 text-left transition-colors",
                it.disabled
                  ? "cursor-default text-faint/60"
                  : i === active
                    ? "bg-card2 text-txt"
                    : it.danger
                      ? "text-red hover:bg-card2"
                      : "text-muted hover:bg-card2 hover:text-txt",
              )}
            >
              <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center">
                {it.icon ?? null}
              </span>
              <span className="min-w-0 flex-1 truncate">{it.label}</span>
              {it.shortcut ? <span className="shrink-0 text-[10px] text-faint">{it.shortcut}</span> : null}
            </button>
          </div>
        ))}
      </div>
    </>
  );
}

// ---- file-tree action set ---------------------------------------------------

/** Actions the tree maps onto panel callbacks. */
export type CodeContextAction =
  | "new-file"
  | "new-folder"
  | "rename"
  | "delete"
  | "copy-rel"
  | "copy-abs"
  | "reveal"
  | "open-external";

export interface CodeMenuFlags {
  /** What was right-clicked. `"root"` = the empty area (new file/folder only). */
  target: "file" | "dir" | "root";
  /** An `ro` mount / missing root: create, rename and delete are refused
   *  (server-side `resolve_code_target(..., write=True)` would reject anyway —
   *  disabling up front is the honest UI). */
  writable: boolean;
  /** Which integration callbacks the panel wired; an action without its
   *  callback is omitted rather than rendered as a dead control. */
  canCreate: boolean;
  canRename: boolean;
  canDelete: boolean;
  canCopyPath: boolean;
  canReveal: boolean;
  canOpenExternal: boolean;
}

/** 菜单项文案集合：`buildFileMenuItems` 是纯函数（非 hook），拿不到
 *  useTranslations，由调用方（FileTree）用 t() 构建好传入。 */
export interface CodeMenuLabels {
  newFile: string;
  newFolder: string;
  rename: string;
  delete: string;
  copyRel: string;
  copyAbs: string;
  reveal: string;
  openExternal: string;
  /** 只读挂载时禁用写操作的原因提示。 */
  readOnlyHint: string;
}

/** Build the menu for one tree row (or the root area). */
export function buildFileMenuItems(
  f: CodeMenuFlags,
  labels: CodeMenuLabels,
): ContextMenuItem[] {
  const groups: ContextMenuItem[][] = [];
  const isDirLike = f.target !== "file";
  const writeHint = labels.readOnlyHint;

  if (isDirLike && f.canCreate) {
    groups.push([
      {
        id: "new-file",
        label: labels.newFile,
        icon: <FilePlus2 className="h-3.5 w-3.5" />,
        disabled: !f.writable,
        disabledHint: writeHint,
      },
      {
        id: "new-folder",
        label: labels.newFolder,
        icon: <FolderPlus className="h-3.5 w-3.5" />,
        disabled: !f.writable,
        disabledHint: writeHint,
      },
    ]);
  }

  if (f.target !== "root") {
    const editGroup: ContextMenuItem[] = [];
    if (f.canRename) {
      editGroup.push({
        id: "rename",
        label: labels.rename,
        icon: <Pencil className="h-3.5 w-3.5" />,
        shortcut: "F2",
        disabled: !f.writable,
        disabledHint: writeHint,
      });
    }
    if (f.canDelete) {
      editGroup.push({
        id: "delete",
        label: labels.delete,
        icon: <Trash2 className="h-3.5 w-3.5" />,
        shortcut: "Delete",
        danger: true,
        disabled: !f.writable,
        disabledHint: writeHint,
      });
    }
    if (editGroup.length) groups.push(editGroup);
  }

  // Path/OS actions address one entry; the root area has no such entry
  // (`path` would be ""), so the root menu is only ever "create here".
  if (f.target !== "root" && f.canCopyPath) {
    groups.push([
      { id: "copy-rel", label: labels.copyRel, icon: <Copy className="h-3.5 w-3.5" /> },
      { id: "copy-abs", label: labels.copyAbs, icon: <Copy className="h-3.5 w-3.5" /> },
    ]);
  }

  const osGroup: ContextMenuItem[] = [];
  if (f.target !== "root" && f.canReveal) {
    osGroup.push({
      id: "reveal",
      label: labels.reveal,
      icon: <FolderOpen className="h-3.5 w-3.5" />,
    });
  }
  if (f.target === "file" && f.canOpenExternal) {
    osGroup.push({
      id: "open-external",
      label: labels.openExternal,
      icon: <ExternalLink className="h-3.5 w-3.5" />,
    });
  }
  if (osGroup.length) groups.push(osGroup);

  const out: ContextMenuItem[] = [];
  groups.forEach((g, gi) => {
    g.forEach((it, i) => out.push(gi > 0 && i === 0 ? { ...it, separatorBefore: true } : it));
  });
  return out;
}