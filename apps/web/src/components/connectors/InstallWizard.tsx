"use client";

/* 5-step install wizard (browser-companion design §7.3, hosted by the
 * Connector module): intro → folder ready → chrome://extensions → dev mode →
 * load unpacked → automatic connect confirmation. Steps come from the
 * connector's install_steps (sidecar defines the copy). */

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import type { InstallStep } from "@/lib/runtime";
import { Check, Copy, ExternalLink, FolderOpen, Loader2, X } from "lucide-react";

const WIZARD_STEP_KEY = "ginno-wizard-step";

export function InstallWizard({
  connectorId,
  onClose,
}: {
  connectorId: string;
  onClose: () => void;
}) {
  // conn 域 catalog（messages/{en,zh-CN}/conn.json）。壳文案走 tConn；步骤数据
  // 走契约（i18n-design.md §3）：runtime 下发 i18n_key + params 时翻译，否则回退原文。
  const tConn = useTranslations("conn");
  const tRoot = useTranslations(); // 根级：i18n_key 是含 conn. 前缀的 dot 路径
  const [steps, setSteps] = useState<InstallStep[]>([]);
  const [i, setI] = useState(() => {
    // 重入(设计 §2.3):进度存 localStorage,刷新/重开不从头来;
    // 完成过向导(连接成功)则清零。
    try {
      const saved = localStorage.getItem(`${WIZARD_STEP_KEY}:${connectorId}`);
      return saved ? Math.max(0, parseInt(saved, 10) || 0) : 0;
    } catch {
      return 0;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(`${WIZARD_STEP_KEY}:${connectorId}`, String(i));
    } catch { /* storage 不可用 */ }
  }, [connectorId, i]);
  const [connected, setConnected] = useState(false);
  const [copied, setCopied] = useState(false);
  const [waited, setWaited] = useState(0);

  useEffect(() => {
    api.getConnector(connectorId).then((c) => setSteps(c.installSteps || [])).catch(() => {});
    const t = setInterval(async () => {
      try {
        const r = await api.listConnectors();
        const c = (r.connectors || []).find((x) => x.id === connectorId);
        if (c?.status === "connected") {
          setConnected(true);
          try {
            localStorage.removeItem(`${WIZARD_STEP_KEY}:${connectorId}`);
          } catch { /* ignore */ }
        }
      } catch { /* ignore */ }
    }, 1200);
    return () => clearInterval(t);
  }, [connectorId]);

  // "waitConnect" step: show elapsed seconds, auto-finish on connect
  const step = steps[i];

  // 步骤文案契约（同 blocks.tsx 的 useEventI18nText 范本）：i18n_key 存在且
  // catalog 命中时按 <key>.<field> 翻译（附 params），否则回退英文兜底原文。
  const stepText = (field: "title" | "body", fallback: string): string => {
    if (!step) return fallback;
    const key = typeof step.i18n_key === "string" ? step.i18n_key : "";
    const full = key ? `${key}.${field}` : "";
    const tr = tRoot as unknown as {
      (key: string, values?: Record<string, string | number>): string;
      has(key: string): boolean;
    };
    if (!full || !tr.has(full)) return fallback;
    try {
      return tr(full, step.params);
    } catch {
      return fallback; // params 结构异常等——翻译永不挂掉渲染
    }
  };

  useEffect(() => {
    if (!step?.waitConnect || connected) return;
    const t = setInterval(() => setWaited((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, [step, connected]);

  const copy = useCallback(async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* clipboard unavailable */ }
  }, []);

  if (!steps.length) return null;
  const isLast = i >= steps.length - 1;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-lg rounded-2xl border border-line bg-card p-6 shadow-xl">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold text-txt">
            {tConn("wizard.title")}
          </h2>
          <button onClick={onClose} className="rounded p-1 text-faint hover:text-txt">
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* step chips */}
        <div className="mb-4 flex items-center gap-1">
          {steps.map((s, idx) => (
            <div
              key={s.key}
              className={`h-1 flex-1 rounded-full ${
                idx <= (connected ? steps.length - 1 : i) ? "bg-violet-600" : "bg-line"
              }`}
            />
          ))}
        </div>

        {connected ? (
          <div className="py-6 text-center">
            <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-green/15">
              <Check className="h-6 w-6 text-green" />
            </div>
            <div className="text-sm font-medium text-txt">{tConn("wizard.connectedTitle")}</div>
            <div className="mt-1 text-xs text-faint">
              {tConn("wizard.connectedBody")}
            </div>
            <button
              onClick={onClose}
              className="mt-4 rounded-lg bg-violet-600 px-4 py-2 text-sm font-medium text-white hover:bg-violet-500"
            >
              {tConn("wizard.getStarted")}
            </button>
          </div>
        ) : (
          step && (
            <>
              <div className="text-sm font-medium text-txt">
                {tConn("wizard.stepLabel", { n: i + 1 })} · {stepText("title", step.title)}
              </div>
              <p className="mt-2 whitespace-pre-wrap text-sm leading-relaxed text-faint">
                {stepText("body", step.body)}
              </p>

              {step.action === "reveal_folder" && (
                <button
                  onClick={() => api.connectorAction(connectorId, "reveal_folder")}
                  className="mt-3 inline-flex items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-xs text-txt hover:bg-hover"
                >
                  <FolderOpen className="h-3.5 w-3.5" /> {tConn("wizard.showInFinder")}
                </button>
              )}

              {step.copy && (
                <div className="mt-3 flex items-center gap-2">
                  <code className="flex-1 truncate rounded-lg bg-hover px-3 py-2 font-mono text-xs text-txt">
                    {step.copy}
                  </code>
                  <button
                    onClick={() => copy(step.copy!)}
                    className="rounded-lg border border-line p-2 text-faint hover:text-txt"
                    title={tConn("wizard.copy")}
                  >
                    {copied ? <Check className="h-3.5 w-3.5 text-green" /> : <Copy className="h-3.5 w-3.5" />}
                  </button>
                </div>
              )}

              {step.waitConnect && (
                <div className="mt-3 flex items-center gap-2 text-xs text-faint">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  {tConn("wizard.waited", { s: waited })}
                </div>
              )}

              {/* 没连上?排查清单(Step 5 末尾,设计 §7.3) */}
              {step.waitConnect && waited > 30 && (
                <div className="mt-4 rounded-lg border border-yellow/40 bg-yellow/10 p-3 text-xs leading-relaxed text-yellow-900 dark:text-yellow-200">
                  {tConn("wizard.troubleshootTitle")}
                  <ol className="mt-1 list-decimal space-y-0.5 pl-4">
                    <li>{tConn("wizard.troubleshoot1")}</li>
                    <li>{tConn("wizard.troubleshoot2")}</li>
                    <li>{tConn("wizard.troubleshoot3")}</li>
                    <li>{tConn("wizard.troubleshoot4")}</li>
                  </ol>
                </div>
              )}

              <div className="mt-5 flex items-center justify-between">
                <button
                  onClick={() => setI(Math.max(0, i - 1))}
                  disabled={i === 0}
                  className="rounded-lg px-3 py-1.5 text-xs text-faint disabled:opacity-40"
                >
                  {tConn("wizard.back")}
                </button>
                <div className="flex items-center gap-2">
                  <button onClick={onClose} className="px-3 py-1.5 text-xs text-faint">
                    {tConn("wizard.later")}
                  </button>
                  {!step.waitConnect && (
                    <button
                      onClick={() => setI(Math.min(steps.length - 1, i + 1))}
                      disabled={isLast}
                      className="rounded-lg bg-violet-600 px-4 py-1.5 text-xs font-medium text-white hover:bg-violet-500 disabled:opacity-40"
                    >
                      {tConn("wizard.next")}
                    </button>
                  )}
                </div>
              </div>
            </>
          )
        )}
      </div>
    </div>
  );
}
