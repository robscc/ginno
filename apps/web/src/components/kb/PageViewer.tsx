"use client";

import { useEffect, useRef, useState } from "react";
import { Eye, Pencil, Save, X, Link2, FilePlus } from "lucide-react";
import * as api from "@/lib/runtime";
import type { WikiPageDoc } from "@/lib/types";
import { Markdown } from "@/components/chat/Markdown";

export interface ViewTarget {
  path?: string;
  title?: string;
}

const sanitize = (s: string) =>
  (s || "untitled")
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 80) || "untitled";

/**
 * Obsidian-like page pane: a reading view (markdown with clickable wikilinks),
 * an editing view (raw markdown + an "insert [[link]]" toolbar) and a create
 * view for dangling wikilinks (a note that doesn't exist yet).
 */
export function PageViewer({
  target,
  onNavigate,
  onSaved,
  onClose,
}: {
  target: ViewTarget | null;
  onNavigate: (t: string) => void;
  onSaved: () => void;
  onClose: () => void;
}) {
  const [doc, setDoc] = useState<WikiPageDoc | null>(null);
  const [loading, setLoading] = useState(false);
  const [mode, setMode] = useState<"read" | "edit" | "create">("read");
  const [raw, setRaw] = useState("");
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    if (!target) {
      setDoc(null);
      return;
    }
    let alive = true;
    setLoading(true);
    setMsg("");
    api
      .kbWikiPage(target.path || "", target.title || "")
      .then((d) => {
        if (!alive) return;
        setDoc(d);
        setRaw(d.raw || "");
        if (d.exists) {
          setPath(d.path);
          setMode("read");
        } else {
          // dangling link → offer to create; default path = sanitized title at root
          setPath(target.path && target.path.endsWith(".md") ? target.path : `${sanitize(target.title || target.path || "")}.md`);
          setRaw(`---\ntitle: ${target.title || target.path || ""}\ntags: []\n---\n\n# ${target.title || target.path || ""}\n\n`);
          setMode("create");
        }
      })
      .catch(() => alive && setMsg("加载失败：运行时未连接"))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // onNavigate/onSaved/onClose are intentionally omitted (parent passes fresh
    // identities each render; depending on them would reload the doc every render).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.path, target?.title]);

  const insertLink = () => {
    const ta = taRef.current;
    if (!ta) return;
    const s = ta.selectionStart, e = ta.selectionEnd;
    const sel = raw.slice(s, e);
    const ins = `[[${sel}]]`;
    const next = raw.slice(0, s) + ins + raw.slice(e);
    setRaw(next);
    requestAnimationFrame(() => {
      ta.focus();
      const pos = s + 2 + sel.length;
      ta.setSelectionRange(sel ? pos : s + 2, sel ? pos : s + 2 + 0);
    });
  };

  const save = async () => {
    setBusy(true);
    setMsg("");
    try {
      const r =
        mode === "create"
          ? await api.kbWikiCreatePage(path.trim(), raw)
          : await api.kbWikiPutPage(doc?.path || path.trim(), raw);
      if (r.ok) {
        setMsg(mode === "create" ? "已创建" : "已保存");
        onSaved();
        if (mode === "create") onNavigate(path.trim()); // reopen as existing
        else setMode("read");
      } else {
        setMsg(r.error || "保存失败");
      }
    } catch {
      setMsg("保存失败：运行时未连接");
    } finally {
      setBusy(false);
    }
  };

  if (!target) {
    return (
      <div className="flex h-full min-h-[320px] flex-col items-center justify-center rounded-xl border border-dashed border-line p-6 text-center text-sm text-faint">
        <FilePlus className="mb-2 h-5 w-5 text-faint" />
        从左侧选一篇，或点正文里的 <span className="text-violet">[[wikilink]]</span> 预览 / 跳转。
      </div>
    );
  }
  if (loading) return <div className="p-6 text-sm text-faint">加载中…</div>;
  if (!doc) return <div className="p-6 text-sm text-red">{msg || "无法加载"}</div>;

  const title = doc.title || target.title || path;

  // Reading view hides the YAML frontmatter (it's surfaced as the pane header +
  // tag pills, Obsidian-style) and a leading H1 that merely repeats the title.
  let readBody = doc.raw;
  if (/^---[ \t]*\n/.test(readBody)) readBody = readBody.replace(/^---[\s\S]*?\n---[ \t]*\n?/, "");
  readBody = readBody.replace(/^\n+/, "");
  const nl = readBody.indexOf("\n");
  const head = nl === -1 ? readBody : readBody.slice(0, nl);
  if (/^#[ \t]+/.test(head) && head.replace(/^#[ \t]+/, "").trim() === title.trim()) {
    readBody = nl === -1 ? "" : readBody.slice(nl + 1);
  }

  return (
    <div className="flex h-full min-h-[320px] flex-col overflow-hidden rounded-xl border border-line bg-card">
      <div className="flex items-center gap-2 border-b border-line px-4 py-2.5">
        <span className="truncate text-sm font-semibold text-txt" title={doc.path || path}>
          {title}
        </span>
        {mode === "create" && (
          <span className="rounded-full bg-violet/20 px-2 py-0.5 text-[10px] text-violet">新建</span>
        )}
        <div className="ml-auto flex items-center gap-1">
          {mode === "read" ? (
            <button
              onClick={() => setMode("edit")}
              className="flex items-center gap-1 rounded-lg border border-line px-2 py-1 text-xs text-muted hover:text-txt"
            >
              <Pencil className="h-3.5 w-3.5" /> 编辑
            </button>
          ) : (
            <button
              onClick={() => {
                setMode(doc.exists ? "read" : "create");
                setRaw(doc.raw || raw);
              }}
              className="flex items-center gap-1 rounded-lg border border-line px-2 py-1 text-xs text-muted hover:text-txt"
            >
              <Eye className="h-3.5 w-3.5" /> 预览
            </button>
          )}
          <button onClick={onClose} aria-label="关闭" className="rounded-lg p-1 text-faint hover:text-txt">
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      {mode === "read" ? (
        <div className="flex-1 overflow-y-auto px-5 py-4">
          {!!doc.tags?.length && (
            <div className="mb-3 flex flex-wrap gap-1">
              {doc.tags.map((t) => (
                <span key={t} className="rounded border border-line2 px-1.5 py-0.5 text-[10px] text-faint">
                  #{t}
                </span>
              ))}
            </div>
          )}
          <div className="max-w-none text-txt">
            <Markdown text={readBody} onWikilink={onNavigate} />
          </div>
        </div>
      ) : (
        <div className="flex flex-1 flex-col overflow-hidden">
          {mode === "create" && (
            <div className="flex items-center gap-2 px-4 pt-3">
              <label className="text-[11px] text-faint">保存路径</label>
              <input
                className="field flex-1 font-mono text-xs"
                value={path}
                onChange={(e) => setPath(e.target.value)}
                placeholder="文件夹/笔记名.md"
              />
            </div>
          )}
          <div className="flex items-center gap-2 px-4 py-2">
            <button
              onClick={insertLink}
              className="flex items-center gap-1 rounded-lg border border-line2 px-2 py-1 text-xs text-muted hover:text-txt"
            >
              <Link2 className="h-3.5 w-3.5" /> 插入 [[链接]]
            </button>
            <span className="text-[11px] text-faint">选中文字再点，可生成 [[选中]] </span>
          </div>
          <textarea
            ref={taRef}
            value={raw}
            onChange={(e) => setRaw(e.target.value)}
            spellCheck={false}
            className="m-4 mt-0 flex-1 resize-none rounded-lg border border-line bg-base/40 p-3 font-mono text-xs leading-relaxed text-txt outline-none focus:border-line2"
          />
          <div className="flex items-center gap-2 border-t border-line px-4 py-2.5">
            <button
              onClick={save}
              disabled={busy || !path.trim()}
              className="flex items-center gap-1 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
            >
              <Save className="h-3.5 w-3.5" /> {mode === "create" ? "创建" : "保存"}
            </button>
            {msg && <span className={`text-xs ${msg.startsWith("已") ? "text-green" : "text-red"}`}>{msg}</span>}
          </div>
        </div>
      )}
    </div>
  );
}
