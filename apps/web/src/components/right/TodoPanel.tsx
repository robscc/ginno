"use client";

import { useCallback, useEffect, useState } from "react";
import { ListChecks, Plus, Clock, Check, RefreshCw } from "lucide-react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import { PRIORITY_HEX, categoryStyle } from "@/lib/theme";
import type { Priority, TodoProvider, TodoSyncEntry } from "@/lib/types";

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
  const [providers, setProviders] = useState<TodoProvider[]>([]);
  const [sync, setSync] = useState<TodoSyncEntry[]>([]);
  const [menu, setMenu] = useState(false);

  const refreshSync = useCallback(() => {
    api
      .todoSyncStatus()
      .then((r) => r?.entries && setSync(r.entries))
      .catch(() => {});
  }, []);
  useEffect(() => {
    api
      .listTodoProviders()
      .then((r) => r?.providers && setProviders(r.providers))
      .catch(() => {});
    refreshSync();
  }, [refreshSync]);
  // sync ledger advances whenever todos change (auto push on done flip)
  useEffect(() => {
    refreshSync();
  }, [g.todos, refreshSync]);

  function latestSync(todoId: string, provider: string) {
    for (let i = sync.length - 1; i >= 0; i--) {
      const e = sync[i];
      if (e.todo_id === todoId && e.provider === provider) return e;
    }
    return undefined;
  }

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
        <div className="relative ml-auto">
          <button
            onClick={() => setMenu((m) => !m)}
            title="与外部 TODO 平台同步（settings → todo_providers 配置）"
            className="flex items-center gap-1 text-xs text-muted hover:text-txt"
          >
            <RefreshCw className="h-3.5 w-3.5" /> 同步
          </button>
          {menu && (
            <>
              <div className="fixed inset-0 z-40" onClick={() => setMenu(false)} />
              <div className="absolute right-0 z-50 mt-1 w-48 overflow-hidden rounded-lg border border-line bg-card py-1 text-xs shadow-xl">
                {providers.length === 0 && (
                  <div className="px-3 py-1.5 text-faint">
                    未配置 provider（settings → todo_providers）
                  </div>
                )}
                {providers.map((p) => (
                  <button
                    key={p.id}
                    onClick={() => {
                      setMenu(false);
                      void api.pullTodos(p.id).then(() => {
                        g.reloadTodos();
                        refreshSync();
                      });
                    }}
                    className="block w-full px-3 py-1.5 text-left text-muted hover:bg-card2 hover:text-txt"
                  >
                    拉取 {p.label}
                  </button>
                ))}
              </div>
            </>
          )}
        </div>
        <button
          onClick={() => setAdding((v) => !v)}
          className="ml-2 flex items-center gap-1 text-xs text-muted hover:text-txt"
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
        {visible.length === 0 && (
          <div className="px-2 py-8 text-center text-xs text-faint">
            {total === 0
              ? "暂无 TODO。点 + New 添加，或让 Agent 用 todo 工具创建。"
              : "该优先级下暂无任务。"}
          </div>
        )}
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
                  {(t.ext || []).map((x, i) => {
                    const prov = providers.find((p) => p.id === x.provider);
                    const st = latestSync(t.id, x.provider || "");
                    const label = prov?.label || x.provider || "ext";
                    return (
                      <span
                        key={i}
                        className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium"
                        style={{ color: "#3b82f6", background: "#3b82f61a" }}
                        title={`外部待办 ${x.provider}:${x.id ?? ""}`}
                      >
                        {x.url ? (
                          <a href={x.url} target="_blank" rel="noreferrer" className="hover:underline">
                            {label}
                          </a>
                        ) : (
                          label
                        )}
                        {st?.status === "running" && <span className="text-faint">同步中</span>}
                        {st?.status === "ok" && <span className="text-green">✓</span>}
                        {st?.status === "failed" && (
                          <button
                            onClick={() => {
                              void api.pushTodo(t.id, x.provider || "").then(refreshSync);
                            }}
                            className="text-red hover:underline"
                            title={`同步失败：${st.error || "未知错误"}（点击重试）`}
                          >
                            重试
                          </button>
                        )}
                      </span>
                    );
                  })}
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
