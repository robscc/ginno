"use client";

import { useCallback, useEffect, useState } from "react";
import * as api from "@/lib/runtime";
import { BookUp, Brain, Loader2, Sparkles } from "lucide-react";
import { useGinno } from "@/lib/store";
import type { MemoryDraft } from "@/lib/types";
import { MemoryDraftModal } from "./MemoryDraftModal";
import { PromoteModal } from "./PromoteModal";

/** Memory sections: MEMORY.md is grouped by `## 主题`; each group is a
 * promotion unit (Gate 3). Content before the first heading forms an
 * unlabeled group. */
type Section = { heading: string; lines: string[] };

function splitSections(content: string): Section[] {
  const out: Section[] = [];
  let heading = "";
  let lines: string[] = [];
  const flush = () => {
    if (lines.some((l) => l.trim())) out.push({ heading, lines });
    lines = [];
  };
  for (const line of content.split("\n")) {
    if (line.startsWith("## ")) {
      flush();
      heading = line;
      lines = [line];
    } else {
      lines.push(line);
    }
  }
  flush();
  return out;
}

export function MemoryPanel() {
  const g = useGinno();
  const [content, setContent] = useState("");
  const [poolCount, setPoolCount] = useState(0);
  const [kbUsable, setKbUsable] = useState(false);
  const [draft, setDraft] = useState<MemoryDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [draftOpen, setDraftOpen] = useState(false);
  const [promoteSection, setPromoteSection] = useState<Section | null>(null);

  const load = useCallback(async () => {
    try {
      const [r, d] = await Promise.all([api.getMemory(), api.getMemoryDraft()]);
      if (r.ok) {
        setContent(r.content);
        setPoolCount(r.pool_count);
        setKbUsable(r.kb_usable);
      }
      setDraft(d.ok && d.draft ? d : null);
      g.reloadMemoryBadge();
    } catch {
      /* sidecar down */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function onDistill() {
    setBusy(true);
    setMsg("");
    try {
      const r = await api.summarizeMemory();
      if (r.ok) {
        if (r.message) {
          setMsg(r.message); // pool empty
        } else {
          await load();
          setDraftOpen(true); // straight into review
        }
      } else {
        setMsg(r.error || "起草失败");
      }
    } catch (e) {
      setMsg(e instanceof Error ? `起草失败：${e.message}` : "起草失败：无法连接运行时");
    } finally {
      setBusy(false);
    }
  }

  const sections = splitSections(content);

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
          onClick={onDistill}
          disabled={busy || poolCount === 0}
          title="用 LLM 把对话池提炼成草稿，审核后才写入记忆"
          className="ml-auto flex items-center gap-1 text-xs text-muted hover:text-txt disabled:opacity-50"
        >
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
          提炼草稿
        </button>
      </div>

      {draft && (
        <button
          onClick={() => setDraftOpen(true)}
          className="mx-4 mb-2 flex items-center gap-2 rounded-lg border border-violet/40 bg-violet/10 px-3 py-2 text-left text-xs text-txt hover:bg-violet/20"
        >
          <span className="inline-block h-2 w-2 shrink-0 animate-pulse rounded-full bg-violet" />
          有 {draft.pool_entries ?? 0} 条对话的蒸馏草稿待审核
          <span className="ml-auto text-violet">审核</span>
        </button>
      )}

      {msg && <div className="px-4 pb-2 text-xs text-violet">{msg}</div>}

      <div className="flex-1 overflow-y-auto px-4 pb-4">
        {sections.length ? (
          <div className="space-y-2">
            {sections.map((s, i) => (
              <div
                key={i}
                className="group rounded-lg border border-line bg-base/30 px-3 py-2"
              >
                {kbUsable && (
                  <button
                    onClick={() => setPromoteSection(s)}
                    title="沉淀到知识库"
                    className="float-right ml-2 hidden rounded p-1 text-muted hover:bg-card2 hover:text-violet group-hover:block"
                  >
                    <BookUp className="h-3.5 w-3.5" />
                  </button>
                )}
                <pre className="whitespace-pre-wrap text-xs leading-relaxed text-muted">
                  {s.lines.join("\n")}
                </pre>
              </div>
            ))}
          </div>
        ) : (
          <div className="py-6 text-center text-xs text-faint">
            尚无全局记忆。对话会自动累积到 pool，点「提炼草稿」审核后写入。
          </div>
        )}
        {kbUsable ? (
          <div className="mt-3 text-[11px] text-faint">
            悬停段落可「沉淀到知识库」——记忆成为可检索的知识页。
          </div>
        ) : (
          <div className="mt-3 text-[11px] text-faint">
            在设置中配置知识库（vault）后，可把记忆段落沉淀为可检索的知识页。
          </div>
        )}
      </div>

      {draftOpen && draft && (
        <MemoryDraftModal
          draft={draft}
          onResolved={load}
          onClose={() => setDraftOpen(false)}
        />
      )}
      {promoteSection && (
        <PromoteModal
          text={promoteSection.lines.join("\n")}
          sectionLines={promoteSection.lines}
          onDone={(note) => {
            setMsg(note);
            load();
          }}
          onClose={() => setPromoteSection(null)}
        />
      )}
    </div>
  );
}
