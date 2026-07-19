"use client";

import { useState } from "react";
import { ListChecks, Plus, Clock, Check } from "lucide-react";
import { useGinno } from "@/lib/store";
import { PRIORITY_HEX, categoryStyle } from "@/lib/theme";
import type { Priority } from "@/lib/types";

type Filter = "all" | Priority;
const FILTERS: { id: Filter; label: string; color?: string }[] = [
  { id: "all", label: "All" },
  { id: "high", label: "High", color: PRIORITY_HEX.high },
  { id: "medium", label: "Medium", color: PRIORITY_HEX.medium },
  { id: "low", label: "Low", color: PRIORITY_HEX.low },
];

export function TodoPanel() {
  const g = useGinno();
  const [filter, setFilter] = useState<Filter>("all");
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");

  const visible = g.todos.filter((t) => filter === "all" || t.priority === filter);
  const total = g.todos.length;
  const done = g.todos.filter((t) => t.done).length;
  const pct = total ? Math.round((done / total) * 100) : 0;

  function submit() {
    const title = draft.trim();
    if (!title) return;
    g.addTodo({ title, priority: "medium", category: "Dev", due: "" });
    setDraft("");
    setAdding(false);
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center px-4 pb-2 pt-4">
        <ListChecks className="mr-2 h-4 w-4 text-muted" />
        <span className="text-sm font-semibold text-txt">Daily TODO</span>
        <span className="ml-2 rounded-full bg-card2 px-2 py-0.5 text-[11px] text-muted">{total}</span>
        <button
          onClick={() => setAdding((v) => !v)}
          className="ml-auto flex items-center gap-1 text-xs text-muted hover:text-txt"
        >
          <Plus className="h-3.5 w-3.5" /> New
        </button>
      </div>

      {/* filters */}
      <div className="flex gap-1.5 px-4 pb-2">
        {FILTERS.map((f) => {
          const sel = filter === f.id;
          return (
            <button
              key={f.id}
              onClick={() => setFilter(f.id)}
              className="rounded-md px-2.5 py-1 text-[11px] font-medium transition-colors"
              style={{
                background: sel ? (f.color ? f.color + "22" : "#262632") : "transparent",
                color: sel ? (f.color || "#e9e9f0") : "#9a9aa6",
                border: `1px solid ${sel ? (f.color ? f.color + "55" : "#34343f") : "#262632"}`,
              }}
            >
              {f.label}
            </button>
          );
        })}
      </div>

      {adding && (
        <div className="px-4 pb-2">
          <input
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submit();
              if (e.key === "Escape") setAdding(false);
            }}
            placeholder="New task…"
            className="field"
          />
        </div>
      )}

      {/* list */}
      <div className="flex-1 px-2">
        {visible.map((t) => {
          const cs = categoryStyle(t.category);
          return (
            <div key={t.id} className="flex items-start gap-2.5 rounded-lg px-2 py-2.5 hover:bg-card/50">
              <button
                onClick={() => g.patchTodo(t.id, { done: !t.done })}
                className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors"
                style={{
                  borderColor: t.done ? "#8b5cf6" : "#34343f",
                  background: t.done ? "#8b5cf6" : "transparent",
                }}
              >
                {t.done && <Check className="h-3 w-3 text-white" />}
              </button>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  {!t.done && (
                    <span
                      className="h-1.5 w-1.5 shrink-0 rounded-full"
                      style={{ background: PRIORITY_HEX[t.priority] }}
                    />
                  )}
                  <span
                    className={`truncate text-sm ${t.done ? "text-faint line-through" : "text-txt"}`}
                  >
                    {t.title}
                  </span>
                </div>
                <div className="mt-1 flex items-center gap-2">
                  {t.category && (
                    <span
                      className="rounded px-1.5 py-0.5 text-[10px] font-medium"
                      style={{ color: cs.color, background: cs.bg }}
                    >
                      {t.category}
                    </span>
                  )}
                  {t.due && (
                    <span className="flex items-center gap-1 text-[11px] text-faint">
                      <Clock className="h-3 w-3" />
                      {t.due}
                    </span>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* progress */}
      <div className="border-t border-line px-4 py-3">
        <div className="mb-1.5 flex items-center justify-between text-xs">
          <span className="text-muted">Today&apos;s progress</span>
          <span className="font-medium text-txt">
            {done} / {total}
          </span>
        </div>
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-card2">
          <div className="h-full rounded-full bg-violet transition-all" style={{ width: `${pct}%` }} />
        </div>
      </div>
    </div>
  );
}
