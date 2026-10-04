"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import {
  Boxes,
  Check,
  ChevronDown,
  Clock,
  CornerDownRight,
  ListChecks,
  Loader2,
  MessageSquare,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  SmilePlus,
  Trash2,
  X,
} from "lucide-react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import { PRIORITY_HEX, categoryStyle } from "@/lib/theme";
import { Icon } from "@/components/icons";
import { ConfirmModal } from "@/components/ConfirmModal";
import { ArtifactMetaCard, ICON, KIND_LABEL, type Rect } from "./ArtifactsPanel";
import type { Artifact, Priority, SessionMeta, Todo, TodoProvider, TodoSyncEntry } from "@/lib/types";

type Filter = "all" | Priority;
// 文案按 `filter.<id>` 取（渲染时走 useTranslations），这里只保留 id 与配色。
const FILTERS: { id: Filter; color?: string }[] = [
  { id: "all" },
  { id: "high", color: PRIORITY_HEX.high },
  { id: "medium", color: PRIORITY_HEX.medium },
  { id: "low", color: PRIORITY_HEX.low },
];

const PRIO_RANK: Record<Priority, number> = { high: 0, medium: 1, low: 2 };

/** 相对时间文案：labels 由调用方按当前 locale 组装（模块层不能调 hook）。 */
function relTime(
  ts: number,
  labels: {
    now: string;
    min: (n: number) => string;
    hour: (n: number) => string;
    day: (n: number) => string;
  },
): string {
  const d = Date.now() / 1000 - ts;
  if (d < 60) return labels.now;
  if (d < 3600) return labels.min(Math.floor(d / 60));
  if (d < 86400) return labels.hour(Math.floor(d / 3600));
  if (d < 86400 * 30) return labels.day(Math.floor(d / 86400));
  return new Date(ts * 1000).toLocaleDateString();
}

// Curated picker — the common cases. Anything else can be typed in the
// custom input at the bottom of the popover.
const EMOJIS = [
  "🚀", "🐛", "🔍", "📝", "📊", "🎨", "📚", "💡", "⚙️", "🧪",
  "🌐", "📅", "✅", "⭐", "🔥", "🎯", "📈", "🛠️", "📦", "🔒",
  "💬", "📣", "🧠", "❤️", "☕", "🏁", "📌", "🗂️", "🖥️", "📱",
  "🤖", "🎬", "🎵", "📷", "✈️", "🏠", "💰", "🧾", "🌱", "🏆",
];

/** Sessions linked to a todo — explicit list plus the legacy origin link. */
function linkedSessionIds(todo: Todo): string[] {
  const ids = [...(todo.session_ids || [])];
  const legacy = todo.links?.session_id;
  if (legacy && !ids.includes(legacy)) ids.push(legacy);
  return ids;
}

function sortTodos(list: Todo[]): Todo[] {
  return [...list].sort((a, b) => {
    if (a.done !== b.done) return a.done ? 1 : -1;
    if (PRIO_RANK[a.priority] !== PRIO_RANK[b.priority])
      return PRIO_RANK[a.priority] - PRIO_RANK[b.priority];
    return a.created - b.created;
  });
}

// ---------------------------------------------------------------- emoji ----
function EmojiPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  // i18n：图标选择器框架文案
  const t = useTranslations("right.todo");
  const [open, setOpen] = useState(false);
  const [custom, setCustom] = useState("");
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [open]);

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        title={value ? t("emojiTitle", { v: value }) : t("emojiPick")}
        className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-line2 bg-base/40 text-sm hover:border-violet"
      >
        {value ? <span className="leading-none">{value}</span> : <SmilePlus className="h-3.5 w-3.5 text-faint" />}
      </button>
      {open && (
        <div className="absolute left-0 top-8 z-40 w-56 rounded-xl border border-line bg-panel p-2 shadow-2xl">
          <div className="grid grid-cols-8 gap-0.5">
            {EMOJIS.map((e) => (
              <button
                key={e}
                type="button"
                onClick={() => {
                  onChange(e);
                  setOpen(false);
                }}
                className={`flex h-6 w-6 items-center justify-center rounded text-sm hover:bg-card2 ${
                  value === e ? "bg-violet/20 ring-1 ring-violet/50" : ""
                }`}
              >
                {e}
              </button>
            ))}
          </div>
          <div className="mt-2 flex items-center gap-1 border-t border-line pt-2">
            <input
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && custom.trim()) {
                  onChange(custom.trim().slice(0, 4));
                  setOpen(false);
                }
              }}
              placeholder={t("emojiCustom")}
              className="min-w-0 flex-1 rounded border border-line2 bg-base/40 px-1.5 py-0.5 text-xs text-txt outline-none focus:border-violet"
            />
            {value && (
              <button
                type="button"
                onClick={() => {
                  onChange("");
                  setOpen(false);
                }}
                className="rounded px-1.5 py-0.5 text-[11px] text-muted hover:text-txt"
              >
                {t("emojiNone")}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ----------------------------------------------------------------- tags ----
function TagsInput({
  value,
  onChange,
  placeholder,
}: {
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
}) {
  // i18n：标签输入占位文案（标签本身是用户数据，不翻译）
  const t = useTranslations("right.todo");
  const [draft, setDraft] = useState("");
  const commit = () => {
    const parts = draft
      .replace(/[,，]/g, " ")
      .split(/\s+/)
      .map((x) => x.trim().replace(/^#/, ""))
      .filter(Boolean);
    if (parts.length) {
      const next = [...value];
      for (const p of parts) if (!next.includes(p)) next.push(p);
      onChange(next.slice(0, 8));
    }
    setDraft("");
  };
  return (
    <div className="flex min-h-7 flex-1 flex-wrap items-center gap-1 rounded-md border border-line2 bg-base/40 px-1.5 py-1">
      {value.map((tag) => (
        <span key={tag} className="flex items-center gap-0.5 rounded bg-violet/15 px-1.5 py-px text-[10px] text-violet">
          #{tag}
          <button
            type="button"
            onClick={() => onChange(value.filter((x) => x !== tag))}
            className="hover:text-txt"
          >
            <X className="h-2.5 w-2.5" />
          </button>
        </span>
      ))}
      <input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " " || e.key === ",") {
            e.preventDefault();
            commit();
          } else if (e.key === "Backspace" && !draft && value.length) {
            onChange(value.slice(0, -1));
          }
        }}
        onBlur={commit}
        placeholder={value.length ? "" : (placeholder ?? t("tagsPlaceholder"))}
        className="min-w-12 flex-1 bg-transparent text-xs text-txt outline-none placeholder:text-faint"
      />
    </div>
  );
}

// --------------------------------------------------------------- editor ----
interface Draft {
  title: string;
  emoji: string;
  priority: Priority;
  category: string;
  due: string;
  tags: string[];
}

function TodoEditor({
  initial,
  submitLabel,
  onSubmit,
  onCancel,
}: {
  initial: Draft;
  submitLabel: string;
  onSubmit: (d: Draft) => void;
  onCancel: () => void;
}) {
  // i18n：编辑器框架文案
  const t = useTranslations("right.todo");
  const tc = useTranslations("right.common");
  const [d, setD] = useState<Draft>(initial);
  const submit = () => {
    if (!d.title.trim()) return;
    onSubmit({ ...d, title: d.title.trim() });
  };
  return (
    <div className="space-y-2 rounded-lg border border-line bg-card/40 p-2.5">
      <div className="flex items-center gap-2">
        <EmojiPicker value={d.emoji} onChange={(emoji) => setD((x) => ({ ...x, emoji }))} />
        <input
          autoFocus
          value={d.title}
          onChange={(e) => setD((x) => ({ ...x, title: e.target.value }))}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
            if (e.key === "Escape") onCancel();
          }}
          placeholder={t("titlePlaceholder")}
          className="min-w-0 flex-1 rounded-md border border-line2 bg-base/40 px-2 py-1 text-sm text-txt outline-none focus:border-violet"
        />
      </div>
      <div className="flex items-center gap-1.5">
        <select
          value={d.priority}
          onChange={(e) => setD((x) => ({ ...x, priority: e.target.value as Priority }))}
          className="rounded-md border border-line2 bg-base/40 px-1.5 py-1 text-[11px] text-txt outline-none"
        >
          <option value="high" className="bg-panel">{t("filter.high")}</option>
          <option value="medium" className="bg-panel">{t("filter.medium")}</option>
          <option value="low" className="bg-panel">{t("filter.low")}</option>
        </select>
        <input
          value={d.category}
          onChange={(e) => setD((x) => ({ ...x, category: e.target.value }))}
          placeholder={t("categoryPlaceholder")}
          className="w-20 rounded-md border border-line2 bg-base/40 px-1.5 py-1 text-[11px] text-txt outline-none focus:border-violet"
        />
        <input
          value={d.due}
          onChange={(e) => setD((x) => ({ ...x, due: e.target.value }))}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
          placeholder={t("duePlaceholder")}
          className="min-w-0 flex-1 rounded-md border border-line2 bg-base/40 px-1.5 py-1 text-[11px] text-txt outline-none focus:border-violet"
        />
      </div>
      <TagsInput value={d.tags} onChange={(tags) => setD((x) => ({ ...x, tags }))} />
      <div className="flex justify-end gap-2">
        <button
          onClick={onCancel}
          className="rounded-md px-2.5 py-1 text-xs text-muted hover:text-txt"
        >
          {tc("cancel")}
        </button>
        <button
          onClick={submit}
          disabled={!d.title.trim()}
          className="rounded-md bg-violet px-2.5 py-1 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40"
        >
          {submitLabel}
        </button>
      </div>
    </div>
  );
}

// --------------------------------------------------------------- detail ----
/** Expanded section: sessions + artifacts linked to the todo. */
function TodoDetail({
  todo,
  sessions,
  artifacts,
  loadingArtifacts,
  onJumpSession,
  onJumpArtifact,
  onHoverArtifact,
  onLeaveArtifact,
  onUnlinkSession,
  onUnlinkArtifact,
}: {
  todo: Todo;
  sessions: SessionMeta[];
  artifacts: Artifact[];
  loadingArtifacts: boolean;
  onJumpSession: (s: SessionMeta) => void;
  onJumpArtifact: (a: Artifact) => void;
  onHoverArtifact: (a: Artifact, el: HTMLElement) => void;
  onLeaveArtifact: () => void;
  onUnlinkSession: (sid: string) => void;
  onUnlinkArtifact: (aid: string) => void;
}) {
  // i18n：详情区框架文案（会话标题/产物名是用户数据，不翻译）
  const t = useTranslations("right.todo");
  const tc = useTranslations("right.common");
  const ta = useTranslations("right.artifacts");
  const rel = (ts: number) =>
    relTime(ts, {
      now: tc("relNow"),
      min: (n) => tc("relMinutes", { n }),
      hour: (n) => tc("relHours", { n }),
      day: (n) => tc("relDays", { n }),
    });
  const kindLabel = (k: string) => {
    const key = KIND_LABEL[k];
    return key ? ta(key) : k;
  };
  const knownIds = new Set(sessions.map((s) => s.id));
  const linkedSids = linkedSessionIds(todo);
  const resolved = linkedSids
    .map((id) => sessions.find((s) => s.id === id))
    .filter((s): s is SessionMeta => !!s)
    .sort((a, b) => b.updated - a.updated);
  const missing = linkedSids.filter((id) => !knownIds.has(id));

  return (
    <div className="mb-1 ml-6 space-y-2 rounded-lg border border-line bg-card/30 px-2.5 py-2">
      {/* sessions */}
      <div>
        <div className="mb-1 flex items-center gap-1 text-[11px] font-medium text-muted">
          <MessageSquare className="h-3 w-3" /> {t("relatedSessions")}
          <span className="text-faint">({resolved.length + missing.length})</span>
        </div>
        {resolved.length === 0 && missing.length === 0 && (
          <div className="py-1 text-[11px] leading-relaxed text-faint">{t("noSessions")}</div>
        )}
        {resolved.map((s) => (
          <div
            key={s.id}
            onClick={() => onJumpSession(s)}
            title={t("jumpSession")}
            className="group flex cursor-pointer items-center gap-2 rounded-md px-1.5 py-1.5 hover:bg-card"
          >
            <Icon name={s.icon || "message-square"} className="h-3.5 w-3.5 shrink-0 text-muted" />
            <span className="min-w-0 flex-1 truncate text-xs text-txt">
              {s.title || t("untitledSession")}
            </span>
            <span className="shrink-0 text-[10px] text-faint">{rel(s.updated)}</span>
            <button
              onClick={(e) => {
                e.stopPropagation();
                onUnlinkSession(s.id);
              }}
              title={t("unlink")}
              className="shrink-0 rounded p-0.5 text-faint hover:text-red invisible group-hover:visible"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        ))}
        {missing.map((id) => (
          <div key={id} className="flex items-center gap-2 px-1.5 py-1 text-[11px] text-faint">
            <Icon name="message-square" className="h-3.5 w-3.5" />
            {t("deletedSession")} <span className="font-mono">{id.slice(0, 8)}</span>
            <button
              onClick={() => onUnlinkSession(id)}
              title={t("unlink")}
              className="rounded p-0.5 hover:text-red"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        ))}
      </div>

      {/* artifacts */}
      <div className="border-t border-line pt-2">
        <div className="mb-1 flex items-center gap-1 text-[11px] font-medium text-muted">
          <Boxes className="h-3 w-3" /> {t("relatedArtifacts")}
          <span className="text-faint">({artifacts.length})</span>
        </div>
        {loadingArtifacts && (
          <div className="flex items-center gap-1.5 py-1 text-[11px] text-faint">
            <Loader2 className="h-3 w-3 animate-spin" /> {tc("loading")}
          </div>
        )}
        {!loadingArtifacts && artifacts.length === 0 && (
          <div className="py-1 text-[11px] leading-relaxed text-faint">{t("noArtifacts")}</div>
        )}
        {artifacts.map((a) => {
          const Ic = ICON[a.kind] || ICON.file;
          return (
            <div
              key={a.id}
              onClick={() => onJumpArtifact(a)}
              onMouseEnter={(e) => onHoverArtifact(a, e.currentTarget)}
              onMouseLeave={onLeaveArtifact}
              title={t("jumpArtifact")}
              className="group flex cursor-pointer items-center gap-2 rounded-md px-1.5 py-1.5 hover:bg-card"
            >
              <Ic className="h-3.5 w-3.5 shrink-0 text-violet" />
              <span className="min-w-0 flex-1 truncate text-xs text-txt">{a.name}</span>
              <span className="shrink-0 rounded bg-card2 px-1 py-px text-[10px] text-faint">
                {kindLabel(a.kind)}
              </span>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onUnlinkArtifact(a.id);
                }}
                title={t("unlink")}
                className="shrink-0 rounded p-0.5 text-faint hover:text-red invisible group-hover:visible"
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- panel ----
export function TodoPanel() {
  const g = useGinno();
  const router = useRouter();
  // i18n：t 面板框架文案，tc 通用文案（标题/标签/待办条目是用户数据，不翻译）
  const t = useTranslations("right.todo");
  const tc = useTranslations("right.common");
  const [filter, setFilter] = useState<Filter>("all");
  const [tagFilter, setTagFilter] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [adding, setAdding] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Todo | null>(null);

  // Artifact associations are resolved against the FULL project artifact list
  // (the store's g.artifacts is scoped to the active session).
  const [allArtifacts, setAllArtifacts] = useState<Artifact[] | null>(null);
  const [loadingArtifacts, setLoadingArtifacts] = useState(false);

  // Hover meta card (same inspector as the Artifacts panel).
  const [hover, setHover] = useState<{ a: Artifact; rect: Rect } | null>(null);
  const enterTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const leaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // External TODO-platform sync (providers configured in settings).
  const [providers, setProviders] = useState<TodoProvider[]>([]);
  const [sync, setSync] = useState<TodoSyncEntry[]>([]);
  const [menu, setMenu] = useState(false);
  const [syncBusy, setSyncBusy] = useState<string | null>(null);
  const [syncMsg, setSyncMsg] = useState<{ text: string; ok: boolean } | null>(null);

  const clearTimers = () => {
    if (enterTimer.current) clearTimeout(enterTimer.current);
    if (leaveTimer.current) clearTimeout(leaveTimer.current);
  };
  function scheduleOpen(a: Artifact, el: HTMLElement) {
    clearTimers();
    enterTimer.current = setTimeout(() => {
      const r = el.getBoundingClientRect();
      setHover({ a, rect: { top: r.top, bottom: r.bottom, left: r.left } });
    }, 200);
  }
  function scheduleClose() {
    clearTimers();
    leaveTimer.current = setTimeout(() => setHover(null), 250);
  }
  function cancelClose() {
    if (leaveTimer.current) clearTimeout(leaveTimer.current);
  }

  // Load the full artifact list lazily the first time a todo is expanded.
  useEffect(() => {
    if (!expandedId || allArtifacts !== null) return;
    let alive = true;
    setLoadingArtifacts(true);
    api
      .listArtifacts("default")
      .then((arts) => {
        if (alive) setAllArtifacts(arts);
      })
      .catch(() => {
        if (alive) setAllArtifacts([]);
      })
      .finally(() => {
        if (alive) setLoadingArtifacts(false);
      });
    return () => {
      alive = false;
    };
  }, [expandedId, allArtifacts]);

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

  // Pull with visible progress + outcome. The run is background; poll its
  // status and then report how many items were mirrored, so "sync" never
  // looks like a no-op (previously a silent button when the platform had
  // nothing new).
  async function pull(p: TodoProvider) {
    if (syncBusy) return;
    setMenu(false);
    setSyncBusy(p.id);
    setSyncMsg(null);
    const before = new Set(g.todos.map((todo) => todo.id));
    const flash = (text: string, ok: boolean) => {
      setSyncMsg({ text, ok });
      window.setTimeout(() => setSyncMsg(null), 8000);
    };
    try {
      const r = await api.pullTodos(p.id);
      if (!r?.ok || !r.run) {
        flash(t("syncFailed", { e: r?.error || t("syncFailedTrigger") }), false);
        return;
      }
      // A todo-sync run is HEADLESS (no present_in session), so it emits no
      // run.* events into any chat and the Workflow panel only hears about it
      // via the completion `workflows.changed` push. Refresh the run list right
      // away so the run shows up in the Workflow tab immediately (as running) —
      // previously it stayed invisible for the whole run until that final push.
      void g.reloadWorkflowRuns();
      let status = "running";
      for (let i = 0; i < 40; i++) {
        await new Promise((res) => setTimeout(res, 4000));
        const run = await api.getWorkflowRun(r.run.id);
        status = run?.run?.status ?? status;
        // Keep the Workflow panel's copy in step while the user watches it (the
        // panel's own 1.5s poll only runs when it is the visible tab).
        void g.reloadWorkflowRuns();
        if (status === "done" || status === "failed") break;
      }
      const after = await api.listTodos();
      const added = after.filter((todo) => !before.has(todo.id)).length;
      await g.reloadTodos();
      await g.reloadWorkflowRuns();
      refreshSync();
      if (status === "failed") flash(t("syncFailedPanel"), false);
      else if (added > 0) flash(t("syncDone", { n: added }), true);
      else flash(t("syncNone"), true);
    } catch {
      flash(t("syncFailedConnect"), false);
    } finally {
      setSyncBusy(null);
    }
  }

  const total = g.todos.length;
  const done = g.todos.filter((t) => t.done).length;
  const pct = total ? Math.round((done / total) * 100) : 0;

  const allTags = useMemo(() => {
    const counts = new Map<string, number>();
    for (const todo of g.todos)
      for (const tag of todo.tags || []) counts.set(tag, (counts.get(tag) || 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8).map(([tag]) => tag);
  }, [g.todos]);

  const visible = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return sortTodos(
      g.todos.filter((todo) => {
        if (filter !== "all" && todo.priority !== filter) return false;
        if (tagFilter && !(todo.tags || []).includes(tagFilter)) return false;
        if (needle && !todo.title.toLowerCase().includes(needle)) return false;
        return true;
      }),
    );
  }, [g.todos, filter, tagFilter, q]);

  const expanded = expandedId ? g.todos.find((todo) => todo.id === expandedId) : undefined;

  // ---- actions ----------------------------------------------------------
  function toggleExpand(id: string) {
    setExpandedId((cur) => (cur === id ? null : id));
    setHover(null);
  }

  function jumpSession(s: SessionMeta) {
    g.setActiveSession(s.id);
    router.push("/");
  }

  function jumpArtifact(a: Artifact) {
    setHover(null);
    if (a.session_id) g.setActiveSession(a.session_id);
    g.setRightTab("artifacts", { manual: true });
    g.flashArtifacts([a.id]);
    router.push("/");
  }

  function unlinkSession(todo: Todo, sid: string) {
    const next = (todo.session_ids || []).filter((x) => x !== sid);
    const patch: Partial<Todo> = { session_ids: next };
    // also drop the legacy origin link when it points at the same session
    if (todo.links?.session_id === sid) patch.links = { ...todo.links, session_id: undefined };
    void g.patchTodo(todo.id, patch);
  }
  function unlinkArtifact(todo: Todo, aid: string) {
    void g.patchTodo(todo.id, { artifact_ids: (todo.artifact_ids || []).filter((x) => x !== aid) });
  }

  async function clearCompleted() {
    const targets = g.todos.filter((todo) => todo.done);
    for (const todo of targets) await g.removeTodo(todo.id);
  }

  const expandedArtifacts: Artifact[] = expanded
    ? (expanded.artifact_ids || [])
        .map((id) => (allArtifacts || []).find((a) => a.id === id))
        .filter((a): a is Artifact => !!a)
    : [];

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center px-4 pb-2 pt-4">
        <ListChecks className="mr-2 h-4 w-4 text-muted" />
        <span className="text-sm font-semibold text-txt">{t("title")}</span>
        <span className="ml-2 rounded-full bg-card2 px-2 py-0.5 text-[11px] text-muted">{total}</span>
        <div className="relative ml-auto">
          <button
            onClick={() => setMenu((m) => !m)}
            disabled={!!syncBusy}
            title={t("syncTitle")}
            className="flex items-center gap-1 text-xs text-muted hover:text-txt disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${syncBusy ? "animate-spin" : ""}`} />
            {syncBusy ? t("syncing") : t("sync")}
          </button>
          {menu && (
            <>
              <div className="fixed inset-0 z-40" onClick={() => setMenu(false)} />
              <div className="absolute right-0 z-50 mt-1 w-48 overflow-hidden rounded-lg border border-line bg-card py-1 text-xs shadow-xl">
                {providers.length === 0 && (
                  <div className="px-3 py-1.5 text-faint">{t("noProvider")}</div>
                )}
                {providers.map((p) => (
                  <button
                    key={p.id}
                    onClick={() => void pull(p)}
                    className="block w-full px-3 py-1.5 text-left text-muted hover:bg-card2 hover:text-txt"
                  >
                    {t("pullFrom", { name: p.label })}
                  </button>
                ))}
              </div>
            </>
          )}
        </div>
        <button
          onClick={() => {
            setAdding((v) => !v);
            setEditingId(null);
          }}
          className="ml-2 flex items-center gap-1 text-xs text-muted hover:text-txt"
        >
          <Plus className="h-3.5 w-3.5" /> {t("new")}
        </button>
      </div>

      {syncMsg && (
        <div className={`px-4 pb-1 text-[11px] ${syncMsg.ok ? "text-muted" : "text-red"}`}>
          {syncMsg.text}
        </div>
      )}

      {/* filters */}
      <div className="flex flex-wrap items-center gap-1.5 px-4 pb-2">
        {FILTERS.map((f) => {
          const sel = filter === f.id;
          return (
            <button
              key={f.id}
              onClick={() => setFilter(f.id)}
              className="rounded-md px-2.5 py-1 text-[11px] font-medium transition-colors"
              style={{
                background: sel ? (f.color ? f.color + "22" : "#262632") : "transparent",
                color: sel ? f.color || "#e9e9f0" : "#9a9aa6",
                border: `1px solid ${sel ? (f.color ? f.color + "55" : "#34343f") : "#262632"}`,
              }}
            >
              {t(`filter.${f.id}`)}
            </button>
          );
        })}
        {allTags.map((tag) => {
          const sel = tagFilter === tag;
          return (
            <button
              key={tag}
              onClick={() => setTagFilter(sel ? null : tag)}
              title={t("filterTag", { tag })}
              className={`rounded-md border px-1.5 py-1 text-[10px] transition-colors ${
                sel
                  ? "border-violet/55 bg-violet/15 text-violet"
                  : "border-line2 text-muted hover:text-txt"
              }`}
            >
              #{tag}
            </button>
          );
        })}
      </div>

      {/* search */}
      {g.todos.length > 5 && (
        <div className="px-4 pb-2">
          <div className="flex items-center gap-1.5 rounded-md border border-line2 bg-base/40 px-2 py-1">
            <Search className="h-3 w-3 shrink-0 text-faint" />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t("searchPlaceholder")}
              className="min-w-0 flex-1 bg-transparent text-xs text-txt outline-none placeholder:text-faint"
            />
            {q && (
              <button onClick={() => setQ("")} className="text-faint hover:text-txt">
                <X className="h-3 w-3" />
              </button>
            )}
          </div>
        </div>
      )}

      {adding && (
        <div className="px-4 pb-2">
          <TodoEditor
            initial={{ title: "", emoji: "", priority: "medium", category: "", due: "", tags: [] }}
            submitLabel={t("add")}
            onCancel={() => setAdding(false)}
            onSubmit={(d) => {
              void g.addTodo({
                title: d.title,
                priority: d.priority,
                category: d.category,
                due: d.due,
                emoji: d.emoji,
                tags: d.tags,
              });
              setAdding(false);
            }}
          />
        </div>
      )}

      {/* list */}
      <div
        className="flex-1 px-2"
        onScroll={() => {
          clearTimers();
          setHover(null);
        }}
      >
        {visible.length === 0 && (
          <div className="px-2 py-8 text-center text-xs text-faint">
            {total === 0 ? t("emptyAll") : t("emptyFiltered")}
          </div>
        )}
        {visible.map((todo) => {
          const cs = categoryStyle(todo.category);
          const isExpanded = expandedId === todo.id;
          const isEditing = editingId === todo.id;
          const nSessions = linkedSessionIds(todo).length;
          const nArtifacts = (todo.artifact_ids || []).length;
          if (isEditing) {
            return (
              <div key={todo.id} className="px-1 py-1">
                <TodoEditor
                  initial={{
                    title: todo.title,
                    emoji: todo.emoji || "",
                    priority: todo.priority,
                    category: todo.category,
                    due: todo.due,
                    tags: todo.tags || [],
                  }}
                  submitLabel={t("save")}
                  onCancel={() => setEditingId(null)}
                  onSubmit={(d) => {
                    void g.patchTodo(todo.id, {
                      title: d.title,
                      priority: d.priority,
                      category: d.category,
                      due: d.due,
                      emoji: d.emoji,
                      tags: d.tags,
                    });
                    setEditingId(null);
                  }}
                />
              </div>
            );
          }
          return (
            <div key={todo.id}>
              <div
                onClick={() => toggleExpand(todo.id)}
                className={`group flex cursor-pointer items-start gap-2.5 rounded-lg px-2 py-2.5 hover:bg-card/50 ${
                  isExpanded ? "bg-card/40" : ""
                }`}
              >
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    void g.patchTodo(todo.id, { done: !todo.done });
                  }}
                  title={todo.done ? t("markUndone") : t("markDone")}
                  className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors"
                  style={{
                    borderColor: todo.done ? "#8b5cf6" : "#34343f",
                    background: todo.done ? "#8b5cf6" : "transparent",
                  }}
                >
                  {todo.done && <Check className="h-3 w-3 text-white" />}
                </button>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    {!todo.done && (
                      <span
                        className="h-1.5 w-1.5 shrink-0 rounded-full"
                        style={{ background: PRIORITY_HEX[todo.priority] }}
                      />
                    )}
                    {todo.emoji && <span className="shrink-0 text-sm leading-none">{todo.emoji}</span>}
                    <span className={`truncate text-sm ${todo.done ? "text-faint line-through" : "text-txt"}`}>
                      {todo.title}
                    </span>
                    <ChevronDown
                      className={`ml-auto h-3 w-3 shrink-0 text-faint transition-transform ${
                        isExpanded ? "" : "-rotate-90"
                      }`}
                    />
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-2">
                    {todo.category && (
                      <span
                        className="rounded px-1.5 py-0.5 text-[10px] font-medium"
                        style={{ color: cs.color, background: cs.bg }}
                      >
                        {todo.category}
                      </span>
                    )}
                    {(todo.tags || []).map((tag) => (
                      <button
                        key={tag}
                        onClick={(e) => {
                          e.stopPropagation();
                          setTagFilter(tagFilter === tag ? null : tag);
                        }}
                        title={t("filterTag", { tag: `#${tag}` })}
                        className={`rounded px-1.5 py-0.5 text-[10px] ${
                          tagFilter === tag
                            ? "bg-violet/25 text-violet"
                            : "bg-card2 text-muted hover:text-txt"
                        }`}
                      >
                        #{tag}
                      </button>
                    ))}
                    {todo.due && (
                      <span className="flex items-center gap-1 text-[11px] text-faint">
                        <Clock className="h-3 w-3" />
                        {todo.due}
                      </span>
                    )}
                    {(todo.ext || []).map((x, i) => {
                      const prov = providers.find((p) => p.id === x.provider);
                      const st = latestSync(todo.id, x.provider || "");
                      const label = prov?.label || x.provider || "ext";
                      return (
                        <span
                          key={i}
                          className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium"
                          style={{ color: "#3b82f6", background: "#3b82f61a" }}
                          title={t("externalTodo", { ref: `${x.provider}:${x.id ?? ""}` })}
                        >
                          {x.url ? (
                            <a
                              href={x.url}
                              target="_blank"
                              rel="noreferrer"
                              onClick={(e) => {
                                if (/^https?:\/\//i.test(x.url!)) {
                                  // WKWebView ignores target=_blank — open via
                                  // the sidecar's default-browser handoff.
                                  e.preventDefault();
                                  api.openLinkExternal(x.url!);
                                }
                              }}
                              className="hover:underline"
                            >
                              {label}
                            </a>
                          ) : (
                            label
                          )}
                          {st?.status === "running" && <span className="text-faint">{t("syncing")}</span>}
                          {st?.status === "ok" && <span className="text-green">✓</span>}
                          {["failed", "cancelled", "interrupted"].includes(st?.status ?? "") && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                void api.pushTodo(todo.id, x.provider || "").then(refreshSync);
                              }}
                              className="text-red hover:underline"
                              title={t("retryTitle", { e: st?.error || t("unknownError") })}
                            >
                              {t("retry")}
                            </button>
                          )}
                        </span>
                      );
                    })}
                    {nSessions > 0 && (
                      <span
                        title={t("nSessionsTitle", { n: nSessions })}
                        className="flex items-center gap-0.5 text-[10px] text-faint"
                      >
                        <MessageSquare className="h-3 w-3" /> {nSessions}
                      </span>
                    )}
                    {nArtifacts > 0 && (
                      <span
                        title={t("nArtifactsTitle", { n: nArtifacts })}
                        className="flex items-center gap-0.5 text-[10px] text-faint"
                      >
                        <Boxes className="h-3 w-3" /> {nArtifacts}
                      </span>
                    )}
                    <span className="ml-auto flex shrink-0 items-center gap-0.5 invisible group-hover:visible">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setEditingId(todo.id);
                          setAdding(false);
                          setExpandedId(null);
                        }}
                        title={t("edit")}
                        className="rounded p-0.5 text-muted hover:bg-card2 hover:text-txt"
                      >
                        <Pencil className="h-3 w-3" />
                      </button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setDeleteTarget(todo);
                        }}
                        title={tc("delete")}
                        className="rounded p-0.5 text-muted hover:bg-card2 hover:text-red"
                      >
                        <Trash2 className="h-3 w-3" />
                      </button>
                    </span>
                  </div>
                </div>
              </div>
              {isExpanded && expanded && (
                <TodoDetail
                  todo={expanded}
                  sessions={g.sessions}
                  artifacts={expandedArtifacts}
                  loadingArtifacts={loadingArtifacts && allArtifacts === null}
                  onJumpSession={jumpSession}
                  onJumpArtifact={jumpArtifact}
                  onHoverArtifact={scheduleOpen}
                  onLeaveArtifact={scheduleClose}
                  onUnlinkSession={(sid) => unlinkSession(expanded, sid)}
                  onUnlinkArtifact={(aid) => unlinkArtifact(expanded, aid)}
                />
              )}
            </div>
          );
        })}
      </div>

      {/* hover artifact inspector — same card as the Artifacts panel */}
      {hover && (() => {
        const live = (allArtifacts || []).find((x) => x.id === hover.a.id);
        if (!live) return null;
        return (
          <div onMouseEnter={cancelClose} onMouseLeave={scheduleClose}>
            <ArtifactMetaCard
              artifact={live}
              rect={hover.rect}
              editing={false}
              setEditing={() => {
                /* editing happens in the Artifacts panel */
              }}
              onClose={() => setHover(null)}
            />
          </div>
        );
      })()}

      {/* progress */}
      <div className="border-t border-line px-4 py-3">
        <div className="mb-1.5 flex items-center justify-between text-xs">
          <span className="text-muted">{t("progress")}</span>
          <span className="flex items-center gap-2 font-medium text-txt">
            {done > 0 && (
              <button
                onClick={() => void clearCompleted()}
                title={t("clearTitle")}
                className="flex items-center gap-0.5 text-[10px] font-normal text-faint hover:text-red"
              >
                <CornerDownRight className="h-3 w-3" /> {t("clearDone", { n: done })}
              </button>
            )}
            <span>
              {done} / {total}
            </span>
          </span>
        </div>
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-card2">
          <div className="h-full rounded-full bg-violet transition-all" style={{ width: `${pct}%` }} />
        </div>
      </div>

      {deleteTarget && (
        <ConfirmModal
          title={t("deleteTitle")}
          message={t("deleteMessage", {
            name: `${deleteTarget.emoji ? deleteTarget.emoji + " " : ""}${deleteTarget.title}`,
          })}
          confirmLabel={tc("delete")}
          onConfirm={() => {
            void g.removeTodo(deleteTarget.id);
            setDeleteTarget(null);
            if (expandedId === deleteTarget.id) setExpandedId(null);
          }}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}
