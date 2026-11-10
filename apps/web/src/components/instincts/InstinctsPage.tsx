"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Plus, RefreshCw, Sparkles, Trash2 } from "lucide-react";
import {
  createInstinct,
  deleteInstinct,
  listInstincts,
  listObservations,
  updateInstinct,
  type Instinct,
} from "@/lib/runtime";

/**
 * 本能管理页 P0（docs/instincts-design.md §11 子集）：条目列表（状态过滤）
 * + 新建/编辑抽屉 + 观察事件 feed（只读）。pending 待确认置顶。
 * P1+（冲突区/evolve/promote）留位。
 */

const CONF_COLOR = (c: number) =>
  c >= 0.9 ? "text-green" : c >= 0.7 ? "text-violet" : c >= 0.5 ? "text-amber" : "text-faint";

export function InstinctsPage() {
  const t = useTranslations("instincts");
  const [items, setItems] = useState<Instinct[]>([]);
  const [obs, setObs] = useState<Record<string, unknown>[]>([]);
  const [status, setStatus] = useState(""); // ""=全部
  const [editing, setEditing] = useState<Instinct | null>(null);
  const [creating, setCreating] = useState(false);
  const [msg, setMsg] = useState("");

  const reload = useCallback(async () => {
    try {
      setItems(await listInstincts(status ? { status } : {}));
      setObs(await listObservations(30));
    } catch {
      setMsg(t("loadFail"));
    }
  }, [status, t]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const sorted = [...items].sort((a, b) => {
    const rank = (x: Instinct) => (x.status === "pending" ? 0 : x.status === "active" ? 1 : 2);
    return rank(a) - rank(b) || b.confidence - a.confidence;
  });

  return (
    <div className="mx-auto max-w-3xl px-4 py-6">
      <header className="mb-4 flex items-center gap-2">
        <Sparkles className="h-4 w-4 text-violet" aria-hidden />
        <h1 className="text-base font-semibold">
          {t("title")}{" "}
          <span className="ml-1 rounded bg-violet/15 px-1 py-px align-middle text-[10px] font-medium text-violet">
            BETA
          </span>
        </h1>
        <p className="ml-2 flex-1 truncate text-xs text-faint">{t("subtitle")}</p>
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          className="rounded-md border border-line bg-card2 px-2 py-1 text-xs"
        >
          <option value="">{t("filter.all")}</option>
          <option value="pending">{t("filter.pending")}</option>
          <option value="active">{t("filter.active")}</option>
          <option value="pruned">{t("filter.pruned")}</option>
        </select>
        <button
          onClick={() => void reload()}
          title={t("refresh")}
          className="rounded-md p-1.5 text-faint hover:bg-card2 hover:text-muted"
        >
          <RefreshCw className="h-3.5 w-3.5" aria-hidden />
        </button>
        <button
          onClick={() => setCreating(true)}
          className="flex items-center gap-1 rounded-md bg-violet/15 px-2 py-1 text-xs text-violet hover:bg-violet/25"
        >
          <Plus className="h-3 w-3" aria-hidden />
          {t("create")}
        </button>
      </header>
      {msg && <div className="mb-3 text-xs text-red">{msg}</div>}

      <div className="space-y-2">
        {sorted.length === 0 && (
          <div className="rounded-lg border border-dashed border-line2 px-4 py-8 text-center text-xs text-faint">
            {t("empty")}
          </div>
        )}
        {sorted.map((it) => (
          <div
            key={`${it.scope}/${it.project_slug}/${it.id}`}
            className="rounded-lg border border-line bg-card px-3 py-2.5"
          >
            <div className="flex items-center gap-2">
              <span className={`text-xs font-semibold ${CONF_COLOR(it.confidence)}`}>
                {it.confidence.toFixed(1)}
              </span>
              <span
                className={`rounded px-1 py-px text-[10px] ${
                  it.status === "pending"
                    ? "bg-amber/15 text-amber"
                    : it.status === "active"
                      ? "bg-green/10 text-green"
                      : "bg-line text-faint"
                }`}
              >
                {t(`status.${it.status}`)}
              </span>
              <span className="text-[10px] text-faint">
                {it.scope === "global" ? t("scope.global") : it.project_slug}
              </span>
              <span className="ml-auto flex items-center gap-1">
                <button
                  onClick={() => setEditing(it)}
                  className="rounded px-1.5 py-0.5 text-[11px] text-violet hover:bg-violet/10"
                >
                  {t("edit")}
                </button>
                <button
                  onClick={async () => {
                    await deleteInstinct(it.id, it.scope, it.project_slug);
                    void reload();
                  }}
                  title={t("delete")}
                  className="rounded p-1 text-faint hover:bg-red/10 hover:text-red"
                >
                  <Trash2 className="h-3 w-3" aria-hidden />
                </button>
              </span>
            </div>
            <div className="mt-1 text-[13px] leading-snug">
              <span className="text-muted">{t("field.trigger") + "："}</span>
              {it.trigger}
            </div>
            <div className="text-[13px] leading-snug">
              <span className="text-muted">{t("field.behavior") + "："}</span>
              {it.behavior}
            </div>
            <div className="mt-1 text-[10px] text-faint">
              {t("field.observed")} {it.evidence.observed} · {it.source} · {it.updated.slice(0, 16)}
            </div>
          </div>
        ))}
      </div>

      {(creating || editing) && (
        <Editor
          base={editing}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSaved={() => {
            setCreating(false);
            setEditing(null);
            void reload();
          }}
        />
      )}

      <section className="mt-6">
        <h2 className="mb-2 text-xs font-semibold text-muted">{t("obs.title")}</h2>
        <div className="max-h-64 space-y-1 overflow-y-auto rounded-lg border border-line bg-panel px-3 py-2 font-mono text-[11px] text-faint">
          {obs.length === 0 && <div className="py-4 text-center">{t("obs.empty")}</div>}
          {obs.map((o, i) => (
            <div key={i} className="truncate">
              <span className="text-line2">{String(o.ts ?? "").slice(11, 19)}</span>{" "}
              <span className="text-violet">{String(o.type ?? "")}</span>{" "}
              {String(o.tool ?? o.signal ?? "")} {String(o.args_repr ?? "").slice(0, 60)}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

function Editor({
  base,
  onClose,
  onSaved,
}: {
  base: Instinct | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const t = useTranslations("instincts");
  const [id, setId] = useState(base?.id ?? "");
  const [trigger, setTrigger] = useState(base?.trigger ?? "");
  const [behavior, setBehavior] = useState(base?.behavior ?? "");
  const [confidence, setConfidence] = useState(base?.confidence ?? 0.7);
  const [status, setStatus] = useState<string>(base?.status ?? "pending");
  const [slug, setSlug] = useState(base?.project_slug ?? "default");
  const [err, setErr] = useState("");

  const save = async () => {
    if (!id.trim() || !trigger.trim() || !behavior.trim()) {
      setErr(t("editor.required"));
      return;
    }
    const body = {
      id: id.trim(),
      trigger: trigger.trim(),
      behavior: behavior.trim(),
      confidence,
      domain: "tool-usage",
      scope: "project" as const,
      project_slug: slug.trim() || "default",
      source: base?.source ?? "manual",
      status,
      evidence: base?.evidence ?? null,
    };
    try {
      if (base) await updateInstinct(base.id, body);
      else await createInstinct(body);
      onSaved();
    } catch (e) {
      setErr(String(e).slice(0, 120));
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onClose}>
      <div
        className="w-[520px] max-w-[92vw] rounded-xl border border-line2 bg-card p-4 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="mb-3 text-sm font-semibold">
          {base ? t("editor.editTitle") : t("editor.createTitle")}
        </h3>
        <div className="space-y-3 text-xs">
          <div>
            <label className="mb-1 block text-faint">{t("field.trigger")}</label>
            <input
              value={trigger}
              onChange={(e) => setTrigger(e.target.value)}
              className="w-full rounded-md border border-line bg-panel px-2 py-1.5 text-[13px]"
              placeholder={t("editor.triggerPh")}
            />
          </div>
          <div>
            <label className="mb-1 block text-faint">{t("field.behavior")}</label>
            <textarea
              value={behavior}
              onChange={(e) => setBehavior(e.target.value)}
              className="h-20 w-full resize-none rounded-md border border-line bg-panel px-2 py-1.5 text-[13px]"
              placeholder={t("editor.behaviorPh")}
            />
          </div>
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="mb-1 block text-faint">ID</label>
              <input
                value={id}
                onChange={(e) => setId(e.target.value)}
                disabled={!!base}
                className="w-full rounded-md border border-line bg-panel px-2 py-1.5 font-mono text-[12px] disabled:opacity-50"
              />
            </div>
            <div className="w-24">
              <label className="mb-1 block text-faint">{t("field.confidence")}</label>
              <input
                type="number"
                step="0.1"
                min="0.3"
                max="0.9"
                value={confidence}
                onChange={(e) => setConfidence(Number(e.target.value))}
                className="w-full rounded-md border border-line bg-panel px-2 py-1.5"
              />
            </div>
            <div className="w-28">
              <label className="mb-1 block text-faint">{t("field.status")}</label>
              <select
                value={status}
                onChange={(e) => setStatus(e.target.value)}
                className="w-full rounded-md border border-line bg-panel px-2 py-1.5"
              >
                <option value="pending">pending</option>
                <option value="active">active</option>
                <option value="pruned">pruned</option>
              </select>
            </div>
            {!base && (
              <div className="w-28">
                <label className="mb-1 block text-faint">{t("field.project")}</label>
                <input
                  value={slug}
                  onChange={(e) => setSlug(e.target.value)}
                  className="w-full rounded-md border border-line bg-panel px-2 py-1.5 font-mono text-[12px]"
                />
              </div>
            )}
          </div>
          {err && <div className="text-red">{err}</div>}
          <div className="flex justify-end gap-2 pt-1">
            <button onClick={onClose} className="rounded-md px-3 py-1.5 text-muted hover:bg-card2">
              {t("editor.cancel")}
            </button>
            <button
              onClick={() => void save()}
              className="rounded-md bg-violet px-3 py-1.5 text-white hover:bg-violet/85"
            >
              {t("editor.save")}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
