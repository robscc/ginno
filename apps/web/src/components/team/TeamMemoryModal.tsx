"use client";

// 团队记忆编辑弹窗（agent-team-design.md §5.4）：TEAM.md（团队共享）与
// notes.md（实例私有）共用一个壳——加载即读、保存即写，治理模型同
// GINNO.md（用户直编；Lead 提议由用户粘贴确认，不在本组件范围）。

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

export function TeamMemoryModal({
  title,
  hint,
  load,
  save,
  onClose,
}: {
  title: string;
  hint: string;
  load: () => Promise<string>;
  save: (text: string) => Promise<boolean>;
  onClose: () => void;
}) {
  const tc = useTranslations("chat");
  const [text, setText] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    load()
      .then((t) => {
        if (alive) {
          setText(t);
          setLoaded(true);
        }
      })
      .catch(() => alive && setLoaded(true));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onSave = async () => {
    setBusy(true);
    setError(null);
    try {
      const ok = await save(text);
      if (ok) onClose();
      else setError(tc("team.memSaveFailed"));
    } catch {
      setError(tc("team.memSaveFailed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onMouseDown={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className="flex max-h-[85vh] w-full max-w-xl flex-col rounded-xl border border-line bg-card p-4 shadow-2xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="text-sm font-semibold text-txt">{title}</div>
        <div className="mt-1 text-[11px] leading-relaxed text-faint">{hint}</div>
        <textarea
          className="mt-3 min-h-[300px] flex-1 resize-none rounded-lg border border-line bg-base/40 p-2.5 font-mono text-xs leading-relaxed text-txt outline-none focus:border-violet/50"
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={!loaded}
          spellCheck={false}
        />
        {error && <div className="mt-2 text-xs text-red">{error}</div>}
        <div className="mt-3 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:text-txt"
          >
            {tc("team.memCancel")}
          </button>
          <button
            disabled={busy || !loaded}
            onClick={() => void onSave()}
            className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
          >
            {busy ? tc("team.memSaving") : tc("team.memSave")}
          </button>
        </div>
      </div>
    </div>
  );
}
