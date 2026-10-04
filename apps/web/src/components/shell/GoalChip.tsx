"use client";

/** Goal chip + popover + editor (goal-design.md §4.5).
 *
 * The chip sits in the TopBar and shows the session goal's live status
 * (推进中 / 已暂停 / 受阻 / 用量受限 / 已达成) with elapsed time that ticks
 * while the goal is active. Clicking opens a popover with the objective,
 * progress and the pause/resume/edit/clear actions. Setting a goal uses the
 * editor modal; replacing an unfinished goal goes through a confirmation.
 */

import { useEffect, useRef, useState } from "react";
import { Target } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import type { Goal } from "@/lib/types";
import { ConfirmModal } from "@/components/ConfirmModal";

const STATUS_COLOR: Record<string, { bg: string; fg: string; dot: string }> = {
  active: { bg: "#f9731622", fg: "#fdba74", dot: "#f97316" },
  paused: { bg: "#71717a22", fg: "#a1a1aa", dot: "#71717a" },
  blocked: { bg: "#ef444422", fg: "#fca5a5", dot: "#ef4444" },
  usage_limited: { bg: "#ef444422", fg: "#fca5a5", dot: "#ef4444" },
  complete: { bg: "#22c55e22", fg: "#86efac", dot: "#22c55e" },
};

function fmtElapsed(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${s}s`;
}

/** Editor modal for creating / editing the objective. */
export function GoalEditor({
  initial,
  title,
  onSubmit,
  onClose,
}: {
  initial: string;
  title: string;
  onSubmit: (objective: string) => Promise<void>;
  onClose: () => void;
}) {
  const tr = useTranslations("goal");
  const [text, setText] = useState(initial);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    ref.current?.focus();
  }, []);
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onMouseDown={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className="w-full max-w-lg rounded-xl border border-line bg-card p-4 shadow-2xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="text-sm font-semibold text-txt">{title}</div>
        <textarea
          ref={ref}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={tr("editor.placeholder")}
          rows={5}
          className="mt-3 w-full resize-y rounded-lg border border-line2 bg-base/40 p-2.5 text-sm text-txt outline-none focus:border-violet/60"
        />
        <div className="mt-1 text-[11px] text-faint">
          {tr("editor.hint")}
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:text-txt"
          >
            {tr("editor.cancel")}
          </button>
          <button
            disabled={busy || !text.trim()}
            onClick={async () => {
              setBusy(true);
              try {
                await onSubmit(text.trim());
              } finally {
                setBusy(false);
              }
            }}
            className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
          >
            {busy ? tr("editor.setting") : tr("editor.set")}
          </button>
        </div>
      </div>
    </div>
  );
}

export function GoalChip({ sessionId }: { sessionId: string | null }) {
  const g = useGinno();
  const tr = useTranslations("goal");
  const [pop, setPop] = useState(false);
  const [editing, setEditing] = useState(false);
  const [confirmReplace, setConfirmReplace] = useState<string | null>(null);
  const goal: Goal | null = sessionId ? g.goalBySession[sessionId] ?? null : null;

  // Live-tick elapsed time while active. Derived from the SERVER updated_at so
  // it is monotonic and identical across session switches / reloads (a
  // client-side "seenAt" would reset the timer on every switch — bug). The
  // server accounts time at turn boundaries; between boundaries we add the wall
  // time since the last goal mutation.
  const [, force] = useState(0);
  useEffect(() => {
    if (goal?.status !== "active") return;
    const t = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [goal?.status]);

  if (!sessionId) return null;

  const elapsed = goal
    ? goal.time_used_seconds +
      (goal.status === "active"
        ? Math.max(0, Date.now() / 1000 - (goal.updated_at || Date.now() / 1000))
        : 0)
    : 0;

  // No goal yet → a subtle affordance to set one.
  if (!goal) {
    return (
      <>
        <button
          onClick={() => setEditing(true)}
          title={tr("chip.setTitle")}
          className="flex shrink-0 items-center gap-1 whitespace-nowrap rounded-lg border border-dashed border-line2 px-2 py-1 text-[11px] text-faint hover:border-violet/50 hover:text-violet"
        >
          <Target className="h-3 w-3 shrink-0" /> {tr("chip.set")}
        </button>
        {editing && (
          <GoalEditor
            initial=""
            title={tr("editor.setTitle")}
            onClose={() => setEditing(false)}
            onSubmit={async (objective) => {
              const r = await g.setGoalObjective(sessionId, objective);
              if (r.ok) setEditing(false);
            }}
          />
        )}
      </>
    );
  }

  const sc = STATUS_COLOR[goal.status] ?? STATUS_COLOR.paused;
  // 状态显示名（goal.status.*；未知状态原样显示协议值）——hook 作用域内映射。
  const statusLabel = (st: string): string => {
    switch (st) {
      case "active":
        return tr("status.active");
      case "paused":
        return tr("status.paused");
      case "blocked":
        return tr("status.blocked");
      case "usage_limited":
        return tr("status.usageLimited");
      case "complete":
        return tr("status.complete");
      default:
        return st;
    }
  };
  const label = statusLabel(goal.status);

  const submitObjective = async (objective: string, confirm: boolean) => {
    const r = await g.setGoalObjective(sessionId, objective, confirm);
    if (r.needs_confirm) {
      setConfirmReplace(objective);
      return;
    }
    if (r.ok) setEditing(false);
  };

  return (
    <div className="relative shrink-0">
      <button
        onClick={() => setPop((p) => !p)}
        title={tr("chip.goalTitle", { objective: goal.objective })}
        className="flex max-w-[260px] items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-1 text-[11px]"
        style={{ background: sc.bg, color: sc.fg }}
      >
        <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: sc.dot }} />
        <Target className="h-3 w-3 shrink-0" />
        <span className="truncate">
          {label} · {fmtElapsed(elapsed)}
        </span>
      </button>

      {pop && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setPop(false)} />
          <div className="absolute left-0 z-50 mt-1 w-80 max-w-[min(24rem,calc(100vw-2rem))] overflow-hidden rounded-lg border border-line bg-card p-3 text-xs shadow-xl">
            <div className="flex items-center gap-1.5 font-semibold text-txt">
              <span className="h-1.5 w-1.5 rounded-full" style={{ background: sc.dot }} />
              {tr("panel.header", { label })}
            </div>
            <div className="mt-2 max-h-32 overflow-y-auto whitespace-pre-wrap break-words text-muted">
              {goal.objective}
            </div>
            <div className="mt-2 text-[11px] text-faint">
              {tr("panel.usage", { turns: goal.turns_used, elapsed: fmtElapsed(elapsed) })}
            </div>
            <div className="mt-3 flex flex-wrap gap-1.5">
              {goal.status === "active" && (
                <button
                  onClick={() => g.setGoalStatus(sessionId, "paused")}
                  className="rounded border border-line2 px-2 py-1 text-muted hover:text-txt"
                >
                  {tr("actions.pause")}
                </button>
              )}
              {(goal.status === "paused" ||
                goal.status === "blocked" ||
                goal.status === "usage_limited") && (
                <button
                  onClick={() => g.setGoalStatus(sessionId, "active")}
                  className="rounded border border-line2 px-2 py-1 text-muted hover:text-txt"
                >
                  {tr("actions.resume")}
                </button>
              )}
              {goal.status !== "complete" && (
                <button
                  onClick={() => {
                    setPop(false);
                    setEditing(true);
                  }}
                  className="rounded border border-line2 px-2 py-1 text-muted hover:text-txt"
                >
                  {tr("actions.edit")}
                </button>
              )}
              {goal.status === "complete" && (
                <button
                  onClick={() => {
                    setPop(false);
                    void g.addTodo({ title: goal.objective, done: true, tags: ["goal"] });
                  }}
                  title={tr("actions.archiveTitle")}
                  className="rounded border border-line2 px-2 py-1 text-muted hover:text-txt"
                >
                  {tr("actions.archive")}
                </button>
              )}
              <button
                onClick={() => {
                  setPop(false);
                  void g.clearGoal(sessionId);
                }}
                className="rounded border border-red/40 px-2 py-1 text-red hover:bg-red/10"
              >
                {tr("actions.clear")}
              </button>
            </div>
          </div>
        </>
      )}

      {editing && (
        <GoalEditor
          initial={goal.objective}
          title={tr("editor.editTitle")}
          onClose={() => setEditing(false)}
          onSubmit={(o) => submitObjective(o, false)}
        />
      )}

      {confirmReplace && (
        <ConfirmModal
          title={tr("confirm.title")}
          message={tr("confirm.message", { objective: goal.objective })}
          confirmLabel={tr("confirm.confirmLabel")}
          onCancel={() => setConfirmReplace(null)}
          onConfirm={async () => {
            const obj = confirmReplace;
            setConfirmReplace(null);
            await submitObjective(obj, true);
          }}
        />
      )}
    </div>
  );
}
