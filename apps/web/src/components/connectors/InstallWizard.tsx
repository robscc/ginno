"use client";

/* 5-step install wizard (browser-companion design §7.3, hosted by the
 * Connector module): intro → folder ready → chrome://extensions → dev mode →
 * load unpacked → automatic connect confirmation. Steps come from the
 * connector's install_steps (sidecar defines the copy). */

import { useCallback, useEffect, useState } from "react";
import * as api from "@/lib/runtime";
import type { InstallStep } from "@/lib/runtime";
import { Check, Copy, ExternalLink, FolderOpen, Loader2, X } from "lucide-react";

export function InstallWizard({
  connectorId,
  onClose,
}: {
  connectorId: string;
  onClose: () => void;
}) {
  const [steps, setSteps] = useState<InstallStep[]>([]);
  const [i, setI] = useState(0);
  const [connected, setConnected] = useState(false);
  const [copied, setCopied] = useState(false);
  const [waited, setWaited] = useState(0);

  useEffect(() => {
    api.getConnector(connectorId).then((c) => setSteps(c.installSteps || [])).catch(() => {});
    const t = setInterval(async () => {
      try {
        const r = await api.listConnectors();
        const c = (r.connectors || []).find((x) => x.id === connectorId);
        if (c?.status === "connected") setConnected(true);
      } catch { /* ignore */ }
    }, 1200);
    return () => clearInterval(t);
  }, [connectorId]);

  // "waitConnect" step: show elapsed seconds, auto-finish on connect
  const step = steps[i];
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
            安装 Ginno 浏览器扩展
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
            <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-green-100 dark:bg-green-900/40">
              <Check className="h-6 w-6 text-green-600 dark:text-green-400" />
            </div>
            <div className="text-sm font-medium text-txt">✅ 扩展已连接 Ginno</div>
            <div className="mt-1 text-xs text-faint">
              浏览器工具已就绪——去会话里让 agent 操作你的 Chrome 吧。
            </div>
            <button
              onClick={onClose}
              className="mt-4 rounded-lg bg-violet-600 px-4 py-2 text-sm font-medium text-white hover:bg-violet-500"
            >
              开始使用
            </button>
          </div>
        ) : (
          step && (
            <>
              <div className="text-sm font-medium text-txt">
                第 {i + 1} 步 · {step.title}
              </div>
              <p className="mt-2 whitespace-pre-wrap text-sm leading-relaxed text-faint">
                {step.body}
              </p>

              {step.action === "reveal_folder" && (
                <button
                  onClick={() => api.connectorAction(connectorId, "reveal_folder")}
                  className="mt-3 inline-flex items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-xs text-txt hover:bg-hover"
                >
                  <FolderOpen className="h-3.5 w-3.5" /> 在 Finder 中显示
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
                    title="复制"
                  >
                    {copied ? <Check className="h-3.5 w-3.5 text-green-600" /> : <Copy className="h-3.5 w-3.5" />}
                  </button>
                </div>
              )}

              {step.waitConnect && (
                <div className="mt-3 flex items-center gap-2 text-xs text-faint">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  已等待 {waited}s…
                </div>
              )}

              {/* 没连上?排查清单(Step 5 末尾,设计 §7.3) */}
              {step.waitConnect && waited > 30 && (
                <div className="mt-4 rounded-lg border border-yellow/40 bg-yellow/10 p-3 text-xs leading-relaxed text-yellow-900 dark:text-yellow-200">
                  没连上?按顺序检查:
                  <ol className="mt-1 list-decimal space-y-0.5 pl-4">
                    <li>Chrome 扩展页里出现了「Ginno Browser Connector」且已启用(没出现 → 回到第 4 步重新加载)</li>
                    <li>扩展卡片上没有红色「错误」按钮</li>
                    <li>Ginno 桌面应用正在运行</li>
                    <li>点扩展图标,确认状态为「已连接」;不是则检查端口(默认 8787)</li>
                  </ol>
                </div>
              )}

              <div className="mt-5 flex items-center justify-between">
                <button
                  onClick={() => setI(Math.max(0, i - 1))}
                  disabled={i === 0}
                  className="rounded-lg px-3 py-1.5 text-xs text-faint disabled:opacity-40"
                >
                  上一步
                </button>
                <div className="flex items-center gap-2">
                  <button onClick={onClose} className="px-3 py-1.5 text-xs text-faint">
                    以后再说
                  </button>
                  {!step.waitConnect && (
                    <button
                      onClick={() => setI(Math.min(steps.length - 1, i + 1))}
                      disabled={isLast}
                      className="rounded-lg bg-violet-600 px-4 py-1.5 text-xs font-medium text-white hover:bg-violet-500 disabled:opacity-40"
                    >
                      下一步
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
