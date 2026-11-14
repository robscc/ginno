"use client";

// 团队记忆编辑弹窗（agent-team-design.md §5.4）：TEAM.md（团队共享）与
// notes.md（实例私有）共用一个壳——加载即读、保存即写，治理模型同
// GINNO.md（用户直编；Lead 提议由用户粘贴确认，不在本组件范围）。
//
// backlog P1-15（数据丢失，最高优先）：加载失败绝不能当「空文件」——
// 旧实现 catch 也 setLoaded(true) + 空文本，save 直写会在 sidecar 抖动时
// 把 TEAM.md/notes.md 覆盖成空。现在加载失败进入错误态：禁用保存与编辑、
// 给出重试；只有成功加载后才允许写回。

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { useEscClose } from "./useEscClose";

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
  const [loadFailed, setLoadFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 重试计数：+1 触发重新加载（load 闭包每次渲染都是新的，不进 deps）。
  const [attempt, setAttempt] = useState(0);

  useEscClose(onClose);

  useEffect(() => {
    let alive = true;
    setLoaded(false);
    setLoadFailed(false);
    load()
      .then((t) => {
        if (alive) {
          setText(t);
          setLoaded(true);
        }
      })
      .catch(() => {
        // P1-15：失败 ≠ 空文件。不置 loaded、不清 text，进错误态禁保存。
        if (alive) setLoadFailed(true);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempt]);

  const onSave = async () => {
    // 双保险：按钮已 disabled，这里再挡一次——任何路径都不得在未成功
    // 加载时写回（否则空文本覆盖真实文件）。
    if (!loaded || loadFailed || busy) return;
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
          className="mt-3 min-h-[300px] flex-1 resize-none rounded-lg border border-line bg-base/40 p-2.5 font-mono text-xs leading-relaxed text-txt outline-none focus:border-violet/50 disabled:opacity-60"
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={!loaded || loadFailed}
          spellCheck={false}
        />
        {loadFailed && (
          <div className="mt-2 flex items-center gap-2 rounded-lg border border-red/40 bg-red/10 px-2.5 py-1.5 text-xs text-red">
            <span className="min-w-0 flex-1 leading-relaxed">{tc("team.memLoadFailed")}</span>
            <button
              onClick={() => setAttempt((n) => n + 1)}
              className="shrink-0 rounded border border-red/40 px-2 py-0.5 transition-colors hover:bg-red/10"
            >
              {tc("team.memRetry")}
            </button>
          </div>
        )}
        {error && <div className="mt-2 text-xs text-red">{error}</div>}
        <div className="mt-3 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:text-txt"
          >
            {tc("team.memCancel")}
          </button>
          <button
            disabled={busy || !loaded || loadFailed}
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
