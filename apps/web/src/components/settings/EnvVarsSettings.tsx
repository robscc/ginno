"use client";

import { useCallback, useEffect, useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";

/**
 * 环境变量注入设置（settings.json 的 "env" 键，tools/env_inject.py 消费）：
 * bash 工具（含 skill 命令）起子进程时合并注入——GUI 启动不继承代理等
 * 场景。PATH 由 runtime 端做追加语义，这里不特殊处理。
 */
type Row = { key: string; value: string };

const normEnv = (raw: unknown): Row[] =>
  raw && typeof raw === "object" && !Array.isArray(raw)
    ? Object.entries(raw as Record<string, unknown>).map(([key, v]) => ({
        key,
        value: typeof v === "string" ? v : JSON.stringify(v),
      }))
    : [];

export function EnvVarsSettings() {
  const t = useTranslations("settings.env");
  const [rows, setRows] = useState<Row[]>([]);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setMsg("");
    api
      .getSettings()
      .then((s) => setRows(normEnv((s as Record<string, unknown>).env)))
      .catch(() => setMsg(t("loadFailed")));
  }, [t]);
  useEffect(load, [load]);

  const update = (i: number, patch: Partial<Row>) =>
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const remove = (i: number) => setRows((rs) => rs.filter((_, j) => j !== i));

  async function save() {
    setBusy(true);
    setMsg("");
    try {
      const s = (await api.getSettings()) as Record<string, unknown>;
      const env: Record<string, string> = {};
      for (const r of rows) {
        const k = r.key.trim();
        if (!k) continue; // 空名行静默丢弃，与 runtime 端过滤一致
        env[k] = r.value;
      }
      s.env = env;
      const r = await api.putSettings(s);
      setMsg(r.ok ? t("saved") : t("saveFailed"));
      if (r.ok) setRows(normEnv(env));
    } catch {
      setMsg(t("saveFailedConn"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">{t("title")}</h2>
      <p className="mt-1 max-w-2xl text-sm text-muted">{t("description")}</p>
      <div className="mt-5 max-w-2xl space-y-1.5">
        {rows.map((r, i) => (
          <div key={i} className="flex items-center gap-2">
            <input
              value={r.key}
              onChange={(e) => update(i, { key: e.target.value })}
              placeholder={t("namePlaceholder")}
              className="w-48 rounded border border-line2 bg-card2 px-2 py-1 font-mono text-sm text-txt"
            />
            <input
              value={r.value}
              onChange={(e) => update(i, { value: e.target.value })}
              placeholder={t("valuePlaceholder")}
              className="flex-1 rounded border border-line2 bg-card2 px-2 py-1 font-mono text-sm text-txt"
            />
            <button
              onClick={() => remove(i)}
              title={t("remove")}
              className="rounded p-1.5 text-faint hover:text-red"
            >
              <Trash2 size={14} />
            </button>
          </div>
        ))}
        <button
          onClick={() => setRows((rs) => [...rs, { key: "", value: "" }])}
          className="flex items-center gap-1 rounded border border-line2 px-2 py-1 text-xs text-muted hover:text-txt"
        >
          <Plus size={12} /> {t("add")}
        </button>
        <p className="text-xs text-faint">{t("pathNote")}</p>
        <div className="flex items-center gap-3 pt-1">
          <button
            onClick={() => void save()}
            disabled={busy}
            className="rounded bg-accent px-3 py-1.5 text-sm text-white disabled:opacity-50"
          >
            {t("save")}
          </button>
          {msg && <span className="text-xs text-muted">{msg}</span>}
        </div>
      </div>
    </div>
  );
}
