"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Loader2, X } from "lucide-react";
import * as api from "@/lib/runtime";
import type { PromotePreview } from "@/lib/types";

/** Gate 3 preview+confirm modal: a global-memory section becomes a knowledge
 * page in the vault's landing zone (``Ginno/Memory/``). The dedup gate runs
 * against the retrieval index; a near-duplicate suggests merging instead.
 * Nothing is written until the user confirms. */
export function PromoteModal({
  text,
  sectionLines,
  onDone,
  onClose,
}: {
  text: string;
  /** Lines of the promoted section in MEMORY.md — removed on apply when the
   * checkbox is on. Undefined disables the removal option. */
  sectionLines?: string[];
  onDone: (note: string) => void;
  onClose: () => void;
}) {
  // i18n：晋升确认弹层框架文案（页面正文/路径是用户数据，不翻译）
  const t = useTranslations("right.promote");
  const tc = useTranslations("right.common");
  const [preview, setPreview] = useState<PromotePreview | null>(null);
  const [raw, setRaw] = useState("");
  const [path, setPath] = useState("");
  const [remove, setRemove] = useState(true);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    api
      .kbPromotePreview(text)
      .then((r) => {
        if (!r.ok) {
          setMsg(r.error || t("previewFailed"));
          return;
        }
        setPreview(r);
        setRaw(r.draft?.raw ?? "");
        setPath(r.draft?.path ?? "");
      })
      .catch(() => setMsg(t("previewFailedConnect")))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

  async function apply() {
    setBusy(true);
    setMsg("");
    try {
      const r = await api.kbPromoteApply(path, raw, remove && sectionLines ? sectionLines : undefined);
      if (r.ok) {
        onDone(t("done", { path: r.path ?? "" }));
        onClose();
      } else {
        setMsg(r.error || t("applyFailed"));
      }
    } catch {
      setMsg(t("applyFailedConnect"));
    } finally {
      setBusy(false);
    }
  }

  const suggestion = preview?.suggestion;
  const mergeTarget = preview?.merge_target;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-xl border border-line2 bg-card shadow-2xl"
      >
        <div className="flex items-center border-b border-line px-4 py-3">
          <span className="rounded-full bg-violet/20 px-2 py-0.5 text-[11px] text-violet">
            {t("title")}
          </span>
          <span className="ml-2 truncate text-xs text-muted">{path || "…"}</span>
          <button
            onClick={onClose}
            aria-label={tc("close")}
            className="ml-auto rounded-lg p-1 text-muted hover:bg-card2 hover:text-txt"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          {loading ? (
            <div className="flex items-center gap-2 py-8 text-xs text-muted">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              {t("searching")}
            </div>
          ) : (
            <>
              {suggestion === "merge" && mergeTarget && (
                <div className="mb-3 rounded-lg border border-yellow/40 bg-yellow/10 px-3 py-2 text-xs text-txt">
                  {t("mergeSuggestion", {
                    title: mergeTarget.title,
                    pct: Math.round(mergeTarget.score * 100),
                  })}
                </div>
              )}
              {suggestion === "create" && (
                <div className="mb-3 text-[11px] text-faint">{t("createSuggestion")}</div>
              )}
              {(preview?.similar?.length ?? 0) > 0 && (
                <div className="mb-3 flex flex-wrap gap-1.5">
                  {preview!.similar!.map((s) => (
                    <span
                      key={s.path}
                      className="pill border border-line2 text-muted"
                      title={s.path}
                    >
                      {s.title} · {Math.round(s.score * 100)}%
                    </span>
                  ))}
                </div>
              )}
              <textarea
                value={raw}
                onChange={(e) => setRaw(e.target.value)}
                spellCheck={false}
                className="h-64 w-full resize-y rounded-lg border border-line bg-base/40 p-3 font-mono text-xs leading-relaxed text-txt focus:border-violet focus:outline-none"
              />
              {sectionLines && (
                <label className="mt-3 flex items-center gap-2 text-xs text-muted">
                  <input
                    type="checkbox"
                    checked={remove}
                    onChange={(e) => setRemove(e.target.checked)}
                    className="accent-violet"
                  />
                  {t("removeSection")}
                </label>
              )}
            </>
          )}
          {msg && <div className="mt-2 text-xs text-red">{msg}</div>}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-line px-4 py-3">
          <button
            onClick={onClose}
            disabled={busy}
            className="rounded-lg border border-line px-3 py-1.5 text-xs text-muted hover:bg-card2 disabled:opacity-50"
          >
            {tc("cancel")}
          </button>
          <button
            onClick={apply}
            disabled={busy || loading || !path || suggestion === undefined}
            className="flex items-center gap-1 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            {busy && <Loader2 className="h-3 w-3 animate-spin" />}
            {t("confirm")}
          </button>
        </div>
      </div>
    </div>
  );
}
