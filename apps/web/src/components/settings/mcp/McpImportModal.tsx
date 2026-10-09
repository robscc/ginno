"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { X } from "lucide-react";
import {
  parseMcpServersJson,
  serverLine,
  transportBadgeClass,
  transportOf,
  type McpServerEntry,
  type ParsedServer,
} from "./mcpShared";

// Import JSON 模态（原型 v2 / 设计文档 §5.3 P1）：粘贴整段 mcpServers，
// Detect & Parse 后逐服务器勾选，Import 走 putMcp 合并写回——不自动 reload，
// 连接靠下轮 5s 轮询 / Reconnect failed 入口。
export function McpImportModal({
  onClose,
  onImport,
  onToast,
}: {
  onClose: () => void;
  /** 勾选的 server 合并写回（putMcp，不 reload）。 */
  onImport: (servers: ParsedServer[]) => Promise<void>;
  onToast: (msg: string, tone?: "ok" | "err" | "warn") => void;
}) {
  const t = useTranslations("settings.mcp");
  const [text, setText] = useState("");
  const [parsed, setParsed] = useState<ParsedServer[] | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);

  const detect = () => {
    try {
      const out = parseMcpServersJson(text);
      setParsed(out);
      setPicked(new Set(out.map((p) => p.name)));
    } catch {
      setParsed(null);
      onToast(t("impFail"), "err");
    }
  };

  const go = async () => {
    if (!parsed) return;
    setBusy(true);
    try {
      await onImport(parsed.filter((p) => picked.has(p.name)));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-5">
      <div className="max-h-[88vh] w-[min(640px,94vw)] overflow-auto rounded-xl border border-line2 bg-panel shadow-2xl">
        <div className="flex items-center border-b border-line px-5 py-3.5">
          <h3 className="text-[15px] font-semibold text-txt">{t("impTitle")}</h3>
          <button
            type="button"
            aria-label={t("wzClose")}
            onClick={onClose}
            className="ml-auto flex h-7 w-7 items-center justify-center rounded-lg text-faint hover:bg-card2 hover:text-txt"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
        <div className="p-5">
          <div className="mb-2 text-[11.5px] font-semibold uppercase tracking-wider text-faint">
            {t("impPasteLabel")}
          </div>
          <textarea
            className="field font-mono text-xs"
            rows={9}
            spellCheck={false}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={'{\n  "mcpServers": { ... }\n}'}
          />
          <div className="mt-2.5">
            <button
              type="button"
              onClick={detect}
              className="inline-flex items-center gap-1.5 rounded-lg border border-line2 px-3 py-1.5 text-[13px] font-medium text-txt transition-colors hover:bg-card2"
            >
              {t("impDetect")}
            </button>
          </div>

          {parsed && (
            <div className="mt-2.5">
              <p className="text-[11.5px] leading-relaxed text-faint">
                {t("impDetected", { count: parsed.length })}
              </p>
              <div className="mt-1.5 space-y-1.5">
                {parsed.map((p) => {
                  const tr = transportOf(p.entry as McpServerEntry);
                  const envCount = Object.keys((p.entry.env ?? {}) as Record<string, unknown>).length;
                  return (
                    <label
                      key={p.name}
                      className="flex cursor-pointer items-center gap-2.5 rounded-lg border border-line px-2.5 py-2 text-[13px] hover:border-line2"
                    >
                      <input
                        type="checkbox"
                        checked={picked.has(p.name)}
                        onChange={(e) =>
                          setPicked((s) => {
                            const n = new Set(s);
                            if (e.target.checked) n.add(p.name);
                            else n.delete(p.name);
                            return n;
                          })
                        }
                      />
                      <b className="text-txt">{p.name}</b>
                      <span
                        className={`rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase leading-none tracking-wider ${transportBadgeClass(tr)}`}
                      >
                        {tr}
                      </span>
                      <span className="ml-auto min-w-0 truncate font-mono text-[11.5px] text-faint">
                        {serverLine(p.entry as McpServerEntry)}
                        {envCount > 0 ? ` · ${t("impEnvVars", { count: envCount })}` : ""}
                      </span>
                    </label>
                  );
                })}
              </div>
            </div>
          )}

          <div className="mt-4 flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg px-3 py-1.5 text-[13px] text-muted transition-colors hover:text-txt"
            >
              {t("impCancel")}
            </button>
            <button
              type="button"
              disabled={!parsed || picked.size === 0 || busy}
              onClick={() => void go()}
              className="rounded-lg bg-indigo px-3 py-1.5 text-[13px] font-medium text-white transition-colors hover:bg-indigo2 disabled:opacity-45"
            >
              {t("impGo", { count: picked.size })}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
