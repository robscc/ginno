"use client";

import { useEffect, useState } from "react";
import * as api from "@/lib/runtime";
import { Brain, Sparkles } from "lucide-react";

export function MemoryPanel() {
  const [content, setContent] = useState("");
  const [poolCount, setPoolCount] = useState(0);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  const load = async () => {
    try {
      const r = await api.getMemory();
      if (r.ok) {
        setContent(r.content);
        setPoolCount(r.pool_count);
      }
    } catch {
      /* ignore */
    }
  };

  useEffect(() => {
    load();
  }, []);

  async function onSummarize() {
    setBusy(true);
    setMsg("");
    try {
      const r = await api.summarizeMemory();
      if (r.ok) {
        setMsg(r.message || `已总结 ${r.pool_entries} 条 (${r.summarized_chars} 字)`);
        await load();
      } else {
        setMsg(r.error || "总结失败");
      }
    } catch (e) {
      // try/finally had no catch — a rejected summarize (sidecar down / network)
      // was an unhandled Promise rejection with zero user feedback.
      setMsg(e instanceof Error ? `总结失败：${e.message}` : "总结失败：无法连接运行时");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center px-4 pb-2 pt-4">
        <Brain className="mr-2 h-4 w-4 text-muted" />
        <span className="text-sm font-semibold text-txt">全局记忆</span>
        {poolCount > 0 && (
          <span className="ml-2 rounded-full bg-violet/20 px-2 py-0.5 text-[11px] text-violet">
            pool: {poolCount}
          </span>
        )}
        <button
          onClick={onSummarize}
          disabled={busy || poolCount === 0}
          className="ml-auto flex items-center gap-1 text-xs text-muted hover:text-txt disabled:opacity-50"
        >
          <Sparkles className={`h-3.5 w-3.5 ${busy ? "animate-pulse" : ""}`} />
          总结
        </button>
      </div>
      {msg && <div className="px-4 pb-2 text-xs text-violet">{msg}</div>}
      <div className="flex-1 overflow-y-auto px-4">
        {content ? (
          <pre className="whitespace-pre-wrap text-xs leading-relaxed text-muted">{content}</pre>
        ) : (
          <div className="py-6 text-center text-xs text-faint">
            尚无全局记忆。对话会自动累积到 pool，点「总结」提炼。
          </div>
        )}
      </div>
    </div>
  );
}
