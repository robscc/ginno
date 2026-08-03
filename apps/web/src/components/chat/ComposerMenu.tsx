"use client";

import { useEffect, useRef } from "react";
import { Bot, Brain, FileText, Sparkles, Terminal, Workflow } from "lucide-react";
import type { MenuItem } from "./commandMenu";

const KIND_ICON: Record<MenuItem["kind"], typeof Terminal> = {
  command: Terminal,
  skill: Sparkles,
  artifact: FileText,
  agent: Bot,
  workflow: Workflow,
  memory: Brain,
};

const KIND_COLOR: Record<MenuItem["kind"], string> = {
  command: "#a78bfa",
  skill: "#a78bfa",
  artifact: "#60a5fa",
  agent: "#34d399",
  workflow: "#fbbf24",
  memory: "#f472b6",
};

/**
 * Autocomplete panel rendered above the composer. Pure presentation — the
 * parent owns trigger detection, filtering, and keyboard navigation state.
 */
export function ComposerMenu({
  items,
  active,
  onPick,
  onHover,
}: {
  items: MenuItem[];
  active: number;
  onPick: (item: MenuItem) => void;
  onHover: (index: number) => void;
}) {
  const listRef = useRef<HTMLDivElement | null>(null);

  // Keep the active row in view while arrow-keying.
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [active]);

  let lastGroup = "";
  return (
    <div
      className="absolute bottom-full left-0 right-0 z-20 mb-2 max-h-72 overflow-y-auto rounded-xl border border-line bg-card shadow-xl"
      role="listbox"
      aria-label="命令与提及补全"
    >
      <div ref={listRef}>
        {items.map((item, i) => {
          const showHeader = item.group !== lastGroup;
          lastGroup = item.group;
          const Icon = KIND_ICON[item.kind];
          const isActive = i === active;
          return (
            <div key={`${item.kind}:${item.id}`}>
              {showHeader && (
                <div className="px-3 pb-1 pt-2 text-[10px] font-medium uppercase tracking-wide text-faint">
                  {item.group}
                </div>
              )}
              <button
                type="button"
                data-idx={i}
                role="option"
                aria-selected={isActive}
                onMouseEnter={() => onHover(i)}
                onMouseDown={(e) => {
                  // mousedown (not click) so the textarea keeps focus/blur order
                  e.preventDefault();
                  onPick(item);
                }}
                className={`flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-sm transition-colors ${
                  isActive ? "bg-card2" : "hover:bg-card2/60"
                }`}
              >
                <Icon className="h-3.5 w-3.5 shrink-0" style={{ color: KIND_COLOR[item.kind] }} />
                <span className="shrink-0 font-mono text-[13px] text-txt">{item.label}</span>
                {item.detail && (
                  <span className="min-w-0 flex-1 truncate text-xs text-faint">{item.detail}</span>
                )}
              </button>
            </div>
          );
        })}
      </div>
      <div className="border-t border-line px-3 py-1 text-[10px] text-faint">
        ↑↓ 选择 · Tab/Enter 确认 · Esc 关闭
      </div>
    </div>
  );
}
