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
import { useGinno } from "@/lib/store";
import type { Goal } from "@/lib/types";
import { ConfirmModal } from "@/components/ConfirmModal";

const STATUS_LABEL: Record<string, string> = {
  active: "推进中",
  paused: "已暂停",
  blocked: "受阻",
  usage_limited: "用量受限",
  complete: "已达成",
};

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
          placeholder="描述这个长程目标，例如：深入调研 X 并产出一份带来源的报告"
          rows={5}
          className="mt-3 w-full resize-y rounded-lg border border-line2 bg-base/40 p-2.5 text-sm text-txt outline-none focus:border-violet/60"
        />
        <div className="mt-1 text-[11px] text-faint">
          设定后 Agent 会在每轮结束自动续跑，直到目标达成 / 受阻 / 你暂停。可随时用 /goal 或此卡片控制。
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:text-txt"
          >
            取消
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
            {busy ? "设定中…" : "设定目标"}
          </button>
        </div>
      </div>
    </div>
  );
}

export function GoalChip({ sessionId }: { sessionId: string | null }) {
  const g = useGinno();
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
          title="为本会话设定长程目标（Agent 自主多轮推进）"
          className="flex items-center gap-1 rounded-lg border border-dashed border-line2 px-2 py-1 text-[11px] text-faint hover:border-violet/50 hover:text-violet"
        >
          <Target className="h-3 w-3" /> 设定目标
        </button>
        {editing && (
          <GoalEditor
            initial=""
            title="设定长程目标"
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
  const label = STATUS_LABEL[goal.status] ?? goal.status;

  const submitObjective = async (objective: string, confirm: boolean) => {
    const r = await g.setGoalObjective(sessionId, objective, confirm);
    if (r.needs_confirm) {
      setConfirmReplace(objective);
      return;
    }
    if (r.ok) setEditing(false);
  };

  return (
    <div className="relative">
      <button
        onClick={() => setPop((p) => !p)}
        title={`目标：${goal.objective}`}
        className="flex max-w-[260px] items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px]"
        style={{ background: sc.bg, color: sc.fg }}
      >
        <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: sc.dot }} />
        <Target className="h-3 w-3 shrink-0" />
        <span className="truncate">
          {goal.browser_state === "waiting_human" ? "等你操作" : label} · {fmtElapsed(elapsed)}
        </span>
      </button>

      {pop && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setPop(false)} />
          <div className="absolute left-0 z-50 mt-1 w-80 overflow-hidden rounded-lg border border-line bg-card p-3 text-xs shadow-xl">
            <div className="flex items-center gap-1.5 font-semibold text-txt">
              <span className="h-1.5 w-1.5 rounded-full" style={{ background: sc.dot }} />
              目标 · {label}
            </div>
            <div className="mt-2 max-h-32 overflow-y-auto whitespace-pre-wrap break-words text-muted">
              {goal.objective}
            </div>
            <div className="mt-2 text-[11px] text-faint">
              自主推进 {goal.turns_used} 轮 · 已用 {fmtElapsed(elapsed)}
            </div>
            {goal.browser_state === "waiting_human" && (
              <div className="mt-2 rounded border border-yellow/40 bg-yellow/10 px-2 py-1 text-[11px] text-yellow">
                浏览器在等你操作 — 续跑已暂停，交还后继续。
              </div>
            )}
            <div className="mt-3 flex flex-wrap gap-1.5">
              {goal.status === "active" && (
                <button
                  onClick={() => g.setGoalStatus(sessionId, "paused")}
                  className="rounded border border-line2 px-2 py-1 text-muted hover:text-txt"
                >
                  暂停
                </button>
              )}
              {(goal.status === "paused" ||
                goal.status === "blocked" ||
                goal.status === "usage_limited") && (
                <button
                  onClick={() => g.setGoalStatus(sessionId, "active")}
                  className="rounded border border-line2 px-2 py-1 text-muted hover:text-txt"
                >
                  恢复
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
                  编辑
                </button>
              )}
              {goal.status === "complete" && (
                <button
                  onClick={() => {
                    setPop(false);
                    void g.addTodo({ title: goal.objective, done: true, tags: ["goal"] });
                  }}
                  title="把已达成目标归档为一条已完成 TODO"
                  className="rounded border border-line2 px-2 py-1 text-muted hover:text-txt"
                >
                  归档为 TODO
                </button>
              )}
              <button
                onClick={() => {
                  setPop(false);
                  void g.clearGoal(sessionId);
                }}
                className="rounded border border-red/40 px-2 py-1 text-red hover:bg-red/10"
              >
                清除
              </button>
            </div>
          </div>
        </>
      )}

      {editing && (
        <GoalEditor
          initial={goal.objective}
          title="编辑目标"
          onClose={() => setEditing(false)}
          onSubmit={(o) => submitObjective(o, false)}
        />
      )}

      {confirmReplace && (
        <ConfirmModal
          title="替换目标？"
          message={`当前有未完成目标：\n「${goal.objective}」\n替换后旧目标的进度将被清除。`}
          confirmLabel="替换"
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
