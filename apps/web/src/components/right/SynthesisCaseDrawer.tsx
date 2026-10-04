"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Loader2, RotateCcw, X } from "lucide-react";
import * as api from "@/lib/runtime";

type CaseDetail = Awaited<ReturnType<typeof api.getSynthesisCase>>["case"];

/** Shared detail drawer for one synthesis case — used by Settings → 总结质量
 *  and the right-panel 总结 tab. While the case has no `output` yet (synthesis
 *  in flight) it refetches every 1.5s, so attempts accumulate live and the
 *  final status lands without any manual refresh. */
export function SynthesisCaseDrawer({
  synthesisId,
  onClose,
  onReplayed,
}: {
  synthesisId: string;
  onClose: () => void;
  onReplayed: () => void;
}) {
  // i18n：用例抽屉框架文案（trace/parse/错误详情是数据，不翻译）
  const t = useTranslations("right.case");
  const tc = useTranslations("right.common");
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [replaying, setReplaying] = useState(false);
  const [replayMsg, setReplayMsg] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setInterval> | null = null;
    const load = () => {
      api
        .getSynthesisCase(synthesisId)
        .then((r) => {
          if (!alive || !r.ok) return;
          setDetail(r.case);
          // output.json appeared → the case reached a terminal state; stop.
          // (Cases interrupted by a runtime exit never gain one — the poll is
          // a small JSON read and stops when the drawer closes.)
          if (r.case.output && timer) {
            clearInterval(timer);
            timer = null;
          }
        })
        .catch(() => {});
    };
    load();
    timer = setInterval(load, 1500);
    return () => {
      alive = false;
      if (timer) clearInterval(timer);
    };
  }, [synthesisId]);

  const replay = async () => {
    setReplaying(true);
    setReplayMsg(null);
    try {
      const r = await api.replaySynthesis(synthesisId);
      if (r.ok) {
        setReplayMsg(t("replayOk", { n: r.attempts_used ?? 0, v: r.prompt_version ?? "" }));
        onReplayed();
      } else {
        setReplayMsg(t("replayFail", { r: r.fail_stage || (r.errors || []).join("; ") || "unknown" }));
      }
    } catch {
      setReplayMsg(t("replayFailConnect"));
    } finally {
      setReplaying(false);
    }
  };

  const inFlight = !!detail && !detail.output;

  return (
    <div className="fixed inset-0 z-40" onClick={onClose}>
      <div
        className="absolute right-0 top-0 flex h-full w-96 flex-col border-l border-line bg-panel shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-line px-4 py-3">
          <span className="text-sm font-semibold text-txt">{t("title")}</span>
          <span
            className="truncate font-mono text-[10px] text-faint"
            title={`~/.ginno/synthesis/${synthesisId}`}
          >
            {synthesisId}
          </span>
          {inFlight && (
            <span className="flex shrink-0 items-center gap-1 text-[11px] text-blue">
              <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-blue" />
              {t("synthesizing")}
            </span>
          )}
          <button onClick={onClose} className="ml-auto rounded p-1 text-faint hover:bg-card2 hover:text-txt" aria-label={tc("close")}>
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
          {!detail && <div className="py-6 text-center text-xs text-faint">{tc("loading")}</div>}
          {detail && (
            <>
              <div className="rounded-lg border border-line bg-base/30 p-2.5 text-[11px]">
                <div className="text-faint">
                  {t("status")}{" "}
                  {detail.output ? (
                    <span className={detail.output.status === "ok" ? "text-green" : "text-red"}>
                      {detail.output.status}
                    </span>
                  ) : (
                    <span className="text-blue">{t("inProgress")}</span>
                  )}
                  {detail.output?.fail_stage && <> · {detail.output.fail_stage}</>}
                </div>
                <div className="mt-0.5 text-faint">
                  {t("promptVersion", { v: detail.input?.prompt_version ?? "" })}
                  {detail.output?.attempts_used != null && (
                    <>
                      {" · "}
                      {t("attempts", { n: detail.output.attempts_used })}
                    </>
                  )}
                </div>
              </div>

              {(detail.attempts || []).length > 0 && (
                <div>
                  <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-faint">
                    {t("attemptsHeader")}
                  </div>
                  <div className="space-y-1">
                    {(detail.attempts || []).map((a, i) => (
                      <div key={i} className="rounded-md border border-line bg-card px-2 py-1.5 text-[11px]">
                        <span className={a.validate_errors.length ? "text-red" : "text-green"}>
                          {t("attemptN", { n: a.attempt, parse: a.parse })}
                        </span>
                        <span className="ml-2 text-faint">{a.latency_ms}ms</span>
                        {a.validate_errors.length > 0 && (
                          <div className="mt-0.5 text-[10px] text-red">{a.validate_errors.join("; ")}</div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {detail.input?.trace && (
                <div>
                  <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-faint">
                    {t("trace")}
                  </div>
                  <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-all rounded-md border border-line bg-base/50 p-2 font-mono text-[10px] leading-relaxed text-muted">
                    {detail.input.trace}
                  </pre>
                </div>
              )}
            </>
          )}
        </div>
        <div className="border-t border-line p-3">
          {replayMsg && <div className="mb-2 text-[11px] text-muted">{replayMsg}</div>}
          <button
            onClick={() => void replay()}
            disabled={replaying || !detail || !detail.output}
            className="btn-press flex w-full items-center justify-center gap-1.5 rounded-md bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            {replaying ? <Loader2 className="h-3 w-3 animate-spin" /> : <RotateCcw className="h-3 w-3" />}
            {replaying ? t("replaying") : t("replay")}
          </button>
        </div>
      </div>
    </div>
  );
}
