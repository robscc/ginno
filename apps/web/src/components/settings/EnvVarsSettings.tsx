"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Plus, RotateCw, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import {
  canonRows,
  envFromRows,
  rowsChanged,
  rowsFromEnv,
  type Row,
} from "@/lib/envVarsRows";

/**
 * 环境变量注入设置（settings.json 的 "env" 键，tools/env_inject.py 消费）：
 * bash 工具（含 skill 命令）起子进程时合并注入——GUI 启动不继承代理等
 * 场景。PATH 由 runtime 端做追加语义，这里不特殊处理。
 *
 * 状态机（防误写改造）：
 *   loading → ready（可编辑/保存） | loadFailed（行锁定，仅可手动重试）
 *   ready 下 dirty（草稿 ≠ 基线）才允许保存；保存失败保留草稿可直接重试；
 *   saving 期间全部控件锁定，ref 双保险防重复提交。
 */
type Phase = "loading" | "ready" | "loadFailed";
type Status = "idle" | "saving" | "saved" | "saveFailed" | "saveFailedConn";

export function EnvVarsSettings() {
  const t = useTranslations("settings.env");
  const [phase, setPhase] = useState<Phase>("loading");
  const [rows, setRows] = useState<Row[]>([]);
  const [status, setStatus] = useState<Status>("idle");
  const [saving, setSaving] = useState(false);
  const baseline = useRef<Row[]>([]); // 最近一次成功加载/保存的规范形（脏判定基准）
  const rawEnv = useRef<Record<string, unknown>>({}); // 服务端原始 env，未触碰行按原类型写回
  const loadSeq = useRef(0); // 过期响应守卫：慢响应不得覆盖新状态
  const savingRef = useRef(false); // 同一 tick 双击的防重复提交兜底

  const load = useCallback(() => {
    const seq = ++loadSeq.current;
    setPhase("loading");
    setStatus("idle");
    api
      .getSettings()
      .then((s) => {
        if (seq !== loadSeq.current) return;
        const raw = (s as Record<string, unknown>).env;
        rawEnv.current =
          raw && typeof raw === "object" && !Array.isArray(raw)
            ? (raw as Record<string, unknown>)
            : {};
        const r = rowsFromEnv(raw);
        baseline.current = canonRows(r);
        setRows(r);
        setPhase("ready");
      })
      .catch(() => {
        if (seq !== loadSeq.current) return;
        setPhase("loadFailed"); // 行保持锁定，仅提供手动重试
      });
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const dirty = rowsChanged(rows, baseline.current);
  const canSave = phase === "ready" && !saving && dirty;
  const locked = phase !== "ready" || saving;

  const update = (i: number, patch: Partial<Row>) =>
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const remove = (i: number) => setRows((rs) => rs.filter((_, j) => j !== i));
  const add = () => setRows((rs) => [...rs, { key: "", value: "" }]);

  async function save() {
    // 加载未完成 / 保存中 / 无修改 一律拒绝提交（按钮 disabled 之外的第二道闸）
    if (savingRef.current || phase !== "ready" || !dirty) return;
    savingRef.current = true;
    setSaving(true);
    setStatus("saving");
    try {
      // 每次保存前重拉全量设置、只覆写 env 键——保护 provider keys 等其他设置
      const s = (await api.getSettings()) as Record<string, unknown>;
      const env = envFromRows(rows, rawEnv.current);
      s.env = env;
      const r = await api.putSettings(s);
      if (r.ok) {
        rawEnv.current = env;
        baseline.current = canonRows(rows);
        setRows(canonRows(rows)); // 回填规范形（trim 后的 key）
        setStatus("saved");
      } else {
        setStatus("saveFailed"); // 草稿原样保留，可直接再保存
      }
    } catch {
      setStatus("saveFailedConn"); // 同上，草稿不动
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }

  const statusError =
    phase === "loadFailed" ||
    status === "saveFailed" ||
    status === "saveFailedConn";
  const statusText =
    phase === "loading"
      ? t("loading")
      : phase === "loadFailed"
        ? t("loadFailed")
        : status === "idle"
          ? ""
          : t(status);

  return (
    <div className="px-8 py-7" data-testid="env-vars">
      <h2 className="text-lg font-semibold text-txt">{t("title")}</h2>
      <p className="mt-1 max-w-2xl text-sm text-muted">{t("description")}</p>
      <div className="mt-5 max-w-2xl space-y-1.5">
        {rows.map((r, i) => (
          <div key={i} className="flex items-center gap-2">
            <input
              data-testid={`env-key-${i}`}
              value={r.key}
              disabled={locked}
              onChange={(e) => update(i, { key: e.target.value })}
              placeholder={t("namePlaceholder")}
              className="w-48 rounded border border-line2 bg-card2 px-2 py-1 font-mono text-sm text-txt disabled:opacity-50"
            />
            <input
              data-testid={`env-value-${i}`}
              value={r.value}
              disabled={locked}
              onChange={(e) => update(i, { value: e.target.value })}
              placeholder={t("valuePlaceholder")}
              className="flex-1 rounded border border-line2 bg-card2 px-2 py-1 font-mono text-sm text-txt disabled:opacity-50"
            />
            <button
              data-testid={`env-remove-${i}`}
              onClick={() => remove(i)}
              disabled={locked}
              title={t("remove")}
              className="rounded p-1.5 text-faint hover:text-red disabled:opacity-50"
            >
              <Trash2 size={14} />
            </button>
          </div>
        ))}
        <button
          data-testid="env-add"
          onClick={add}
          disabled={locked}
          className="flex items-center gap-1 rounded border border-line2 px-2 py-1 text-xs text-muted hover:text-txt disabled:opacity-50"
        >
          <Plus size={12} /> {t("add")}
        </button>
        <p className="text-xs text-faint">{t("pathNote")}</p>
        <div className="flex items-center gap-3 pt-1">
          <button
            data-testid="env-save"
            onClick={() => void save()}
            disabled={!canSave}
            title={phase === "ready" && !dirty ? t("noChanges") : undefined}
            className="rounded bg-accent px-3 py-1.5 text-sm text-white disabled:opacity-50"
          >
            {t("save")}
          </button>
          {phase === "loadFailed" && (
            <button
              data-testid="env-retry"
              onClick={load}
              className="flex items-center gap-1 rounded border border-line2 px-2 py-1 text-xs text-muted hover:text-txt"
            >
              <RotateCw size={12} /> {t("retry")}
            </button>
          )}
          <p
            data-testid="env-status"
            role="status"
            className={`text-xs ${statusError ? "text-red" : "text-muted"}`}
          >
            {statusText}
          </p>
        </div>
      </div>
    </div>
  );
}
